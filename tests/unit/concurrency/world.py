"""A deterministic world for interleaving the async cache layer with SSE subscribers.

The world owns manual fakes: a clock, an API transport whose responses are released one at a time,
an event source per subscriber that emits frames on demand, and a store whose writes can be held.
A *step* does one thing (start a read, release a response, deliver a frame, ...), lets the event
loop settle, and then checks the invariants of the spec. Nothing sleeps and no thread races.
"""

import asyncio
import contextvars
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Self

import httpx2

from craftainer_crud_sdk.cache import AsyncCacheLayer, CachePolicy, MemoryStore
from craftainer_crud_sdk.events import AsyncEventSubscriber, invalidate_on_events
from craftainer_crud_sdk.interfaces import EXTENSION_KEY, CacheEntry
from tests.unit.cache.harness import Clock, make_request

RESOURCE = "heroes"
LABEL = "hero"
EVENTS = {
    "transport": "sse",
    "path": "/api/heroes/events",
    "resource": LABEL,
    "resume_param": "subscriber_id",
    "replay": "none",
    "keepalive_seconds": 10,
}
POLICY = CachePolicy(max_age=60, stats_ttl=30, stale_while_revalidate=120)
SETTLE_TURNS = 12
STATS_KINDS = frozenset({"stats", "predict"})
_REAL_SLEEP = asyncio.sleep
_CURRENT: contextvars.ContextVar[str] = contextvars.ContextVar("world_label", default="")

type Step = tuple[Any, ...]


@dataclass(slots=True)
class Landed:
    """An invalidation that has finished clearing the store."""

    version: int  # the server version it reflects
    action: str  # create | update | delete | restore | reset
    record_id: int | None


@dataclass(slots=True)
class Conn:
    """One SSE connection, as the server sees it."""

    wiring: int
    queue: asyncio.Queue[bytes | Exception | None] = field(default_factory=asyncio.Queue)
    up: bool = False  # the server sent the handshake, so it fans events out to it
    alive: bool = True  # the client side has not closed or lost it
    outbox: list[tuple[int, str, int]] = field(default_factory=list)  # not yet delivered


class QueueStream(httpx2.AsyncByteStream):
    """Reads frames the world pushes; marks its connection dead however it ends."""

    def __init__(self, conn: Conn) -> None:
        """Read from `conn`."""
        self.conn = conn

    def __aiter__(self) -> Self:
        """Iterate."""
        return self

    async def __anext__(self) -> bytes:
        """Next frame; ends on `None`, raises a pushed exception."""
        try:
            item = await self.conn.queue.get()
        except BaseException:
            self.conn.alive = False
            raise
        if item is None:
            self.conn.alive = False
            raise StopAsyncIteration
        if isinstance(item, Exception):
            self.conn.alive = False
            raise item
        return item

    async def aclose(self) -> None:
        """The client closed the response."""
        self.conn.alive = False


@dataclass(slots=True)
class Pending:
    """A request that reached the API server and waits for its response to be released."""

    label: str
    request: httpx2.Request
    response: httpx2.Response
    gate: asyncio.Future[None]


class ApiTransport(httpx2.AsyncBaseTransport):
    """The server for GET/PATCH/...: decides at arrival, answers when released."""

    def __init__(self, world: World) -> None:
        """Serve `world`."""
        self.world = world
        self.pending: list[Pending] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Snapshot the server now, deliver the response when the world releases it."""
        world = self.world
        if request.method == "GET":
            version = world.version
            etag = f'"{version}"'
            if request.headers.get("if-none-match") == etag:
                response = httpx2.Response(304, headers={"ETag": etag})
            else:
                response = httpx2.Response(200, json={"version": version}, headers={"ETag": etag})
        else:
            ctx = request.extensions[EXTENSION_KEY]
            version = world.mutate(ctx["op"], ctx["record_id"])
            response = httpx2.Response(
                200, json={"version": version}, headers={"ETag": f'"{version}"'}
            )
        gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        pending = Pending(_CURRENT.get(), request, response, gate)
        self.pending.append(pending)
        try:
            await gate
        finally:
            self.pending.remove(pending)
        return response


class SseTransport(httpx2.MockTransport):
    """Opens a connection per request (or refuses once when told to)."""

    def __init__(self, world: World, wiring: int) -> None:
        """Serve wiring `wiring`."""
        self.world = world
        self.wiring = wiring
        super().__init__(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one connection attempt."""
        if self.wiring in self.world.refuse:
            self.world.refuse.discard(self.wiring)
            return httpx2.Response(503, json={"title": "down"})
        conn = Conn(self.wiring)
        self.world.conns.append(conn)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=QueueStream(conn)
        )


class SpyStore:
    """An async store over a `MemoryStore` whose set/clear can be held until released."""

    def __init__(self, world: World, store: MemoryStore) -> None:
        """Wrap `store`."""
        self.world = world
        self.store = store

    async def _maybe_hold(self, op: str) -> None:
        world = self.world
        if world.holds.get(op, 0) > 0:
            world.holds[op] -= 1
            gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            world.held.append(gate)
            try:
                await gate
            finally:
                world.held.remove(gate)  # released, or the waiting task was cancelled

    async def get(self, key: str) -> CacheEntry | None:
        """Never held: a hit is decided from what is stored right now."""
        return self.store.get(key)

    async def set(self, key: str, entry: CacheEntry, ttl: float | None = None) -> None:
        """Store, after the hold."""
        await self._maybe_hold("set")
        self.store.set(key, entry, ttl)

    async def delete(self, key: str) -> None:
        """Delete, after the hold."""
        await self._maybe_hold("set")
        self.store.delete(key)

    async def clear(self, prefix: str | None = None) -> None:
        """Clear, after the hold."""
        await self._maybe_hold("clear")
        self.store.clear(prefix)

    async def clear_many(self, prefixes: list[str]) -> None:
        """Clear, after the hold."""
        await self._maybe_hold("clear")
        self.store.clear_many(prefixes)


class SpyLayer(AsyncCacheLayer):
    """Tells the world when an invalidation has finished landing."""

    world: World

    async def invalidate_event(self, resource: str, action: str, record_id: int | None) -> None:
        """Land an event."""
        await super().invalidate_event(resource, action, record_id)
        self.world.land_event(action, record_id)

    async def clear_resource(self, resource: str) -> None:
        """Land a reset."""
        await super().clear_resource(resource)
        world = self.world
        world.landed.append(Landed(world.version, "reset", None))
        world.awaiting_reset = [c for c in world.awaiting_reset if c.alive][1:]


@dataclass(slots=True)
class Wiring:
    """A subscriber (and its client) plus the listeners currently wired to it."""

    subscriber: AsyncEventSubscriber
    client: httpx2.AsyncClient
    stops: list[Callable[[], None]] = field(default_factory=list)


class Violation(AssertionError):  # noqa: N818 - an assertion about the spec, not an error type
    """An invariant failed; the message carries the step trace."""


class World:
    """The fakes, the step interpreter and the invariants."""

    def __init__(self, *, preloaded: bool = False) -> None:
        """Create a world with a fresh layer, store, server and no subscribers."""
        self.clock = Clock()
        self.timers: list[tuple[float, asyncio.Future[None]]] = []
        self.version = 1
        self.refuse: set[int] = set()
        self.conns: list[Conn] = []
        self.holds: dict[str, int] = {}
        self.held: list[asyncio.Future[None]] = []
        self.landed: list[Landed] = []
        self.delivered: list[Landed] = []  # events in a stream, not yet landed
        self.trace: list[Step] = []
        self.violations: list[str] = []
        self.closed = False
        self.awaiting_reset: list[Conn] = []  # live connections whose handshake owes a reset
        self.wirings: dict[int, Wiring] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.api = ApiTransport(self)
        self.memory = MemoryStore(clock=self.clock)
        self.layer = SpyLayer(SpyStore(self, self.memory), {RESOURCE: POLICY}, clock=self.clock)
        self.layer.world = self
        self.layer.bind(self.api.handle_async_request)
        self.sub_ids = 0
        if preloaded:
            self.preload()

    # -- the fake clock -----------------------------------------------------------------------

    async def fake_sleep(self, delay: float, result: object = None) -> object:
        """`asyncio.sleep` on the manual clock (zero still yields to the loop)."""
        if delay <= 0:
            await _REAL_SLEEP(0)
            return result
        gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.timers.append((self.clock.now + delay, gate))
        await gate
        return result

    def advance(self, seconds: float) -> None:
        """Move the clock; wake the timers that are due."""
        self.clock.now += seconds
        due = [t for t in self.timers if t[0] <= self.clock.now]
        self.timers = [t for t in self.timers if t[0] > self.clock.now]
        for _, gate in due:
            if not gate.done():
                gate.set_result(None)

    # -- the server ---------------------------------------------------------------------------

    def mutate(self, action: str, record_id: int | None) -> int:
        """A change on the server: bump the version and queue the event for every live stream."""
        self.version += 1
        if record_id is not None:
            for conn in self.conns:
                if conn.up and conn.alive:
                    conn.outbox.append((self.version, action, record_id))
        return self.version

    def land_event(self, action: str, record_id: int | None) -> None:
        """An event listener finished clearing: match it to the oldest delivered event."""
        for item in self.delivered:
            if item.action == action and item.record_id == record_id:
                self.delivered.remove(item)
                self.landed.append(item)
                return

    def preload(self) -> None:
        """Entries a persistent store kept from a previous process (version 0, long fresh)."""
        from craftainer_crud_sdk.cache import build_key  # noqa: PLC0415

        for kind, record_id in (("list", None), ("record", 1), ("stats", None)):
            key = build_key(RESOURCE, kind, record_id, "anon")
            entry = CacheEntry(
                200,
                (("content-type", "application/json"),),
                b'{"version": 0}',
                self.clock.now,
                self.clock.now + 1000,
                self.clock.now + 2000,
                '"0"',
            )
            self.memory.set(key, entry)

    # -- steps ---------------------------------------------------------------------------------

    async def settle(self) -> None:
        """Let every runnable task run until the loop is quiet (a fixed number of turns)."""
        for _ in range(SETTLE_TURNS):
            await _REAL_SLEEP(0)

    async def step(self, step: Step) -> None:
        """Do one thing, settle, check the invariants."""
        self.trace.append(step)
        name, *args = step
        await getattr(self, f"do_{name}")(*args)
        await self.settle()
        self.check()

    async def run(self, steps: list[Step]) -> None:
        """Run `steps`, then finish (release everything, close, check)."""
        for step in steps:
            await self.step(step)
        await self.finish()

    async def finish(self) -> None:
        """Release what is pending, close everything and check the closed-world invariants."""
        for pending in list(self.api.pending):
            await self.step(("release", pending.label))
        if not self.closed:
            await self.step(("close",))
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for wiring in self.wirings.values():
            await wiring.client.aclose()

    def wiring(self, index: int) -> Wiring:
        """The wiring `index`, created on first use."""
        if index not in self.wirings:
            client = httpx2.AsyncClient(transport=SseTransport(self, index), base_url="http://t")
            subscriber = AsyncEventSubscriber(client, EVENTS, jitter=lambda: 0.0)
            self.wirings[index] = Wiring(subscriber, client)
        return self.wirings[index]

    def latest(self, index: int) -> Conn | None:
        """The newest live connection of wiring `index`."""
        live = [c for c in self.conns if c.wiring == index and c.alive]
        return live[-1] if live else None

    async def do_subscribe(self, index: int) -> None:
        """Wire another listener to the wiring's subscriber."""
        wiring = self.wiring(index)
        wiring.stops.append(invalidate_on_events(self.layer, wiring.subscriber, RESOURCE))

    async def do_unsubscribe(self, index: int, which: int = 0) -> None:
        """Withdraw one of the wiring's listeners (no-op without any)."""
        stops = self.wiring(index).stops
        if stops:
            stops.pop(which % len(stops))()

    async def do_hello(self, index: int) -> None:
        """The server accepts the newest open connection and sends the handshake."""
        conn = self.latest(index)
        if conn is not None and not conn.up:
            conn.up = True
            self.awaiting_reset.append(conn)
            self.sub_ids += 1
            frame = {"subscriber_id": f"sub-{self.sub_ids}"}
            conn.queue.put_nowait(f"id: 0\ndata: {json.dumps(frame)}\n\n".encode())

    async def do_deliver(self, index: int, count: int = 1) -> None:
        """Move up to `count` queued events of the connection into its stream."""
        conn = self.latest(index)
        if conn is None or not conn.up:
            return
        for _ in range(count):
            if not conn.outbox:
                return
            version, action, record_id = conn.outbox.pop(0)
            data = {
                "resource": LABEL,
                "record_id": record_id,
                "action": action,
                "snapshot": {},
                "timestamp": str(version),
            }
            self.delivered.append(Landed(version, action, record_id))
            conn.queue.put_nowait(f"id: {version}\ndata: {json.dumps(data)}\n\n".encode())

    async def do_drop(self, index: int) -> None:
        """The newest connection fails mid-stream."""
        conn = self.latest(index)
        if conn is not None:
            conn.queue.put_nowait(httpx2.ReadError("dropped"))

    async def do_eof(self, index: int) -> None:
        """The newest connection ends cleanly."""
        conn = self.latest(index)
        if conn is not None:
            conn.queue.put_nowait(None)

    async def do_refuse(self, index: int) -> None:
        """The next connection attempt of the wiring gets a 503."""
        self.refuse.add(index)

    async def do_advance(self, seconds: float) -> None:
        """Move the clock."""
        self.advance(seconds)

    async def do_mutate(self, action: str, record_id: int) -> None:
        """Another client changes a record."""
        self.mutate(action, record_id)

    async def do_hold(self, op: str) -> None:
        """Hold the next store `set` or `clear` until `release_store`."""
        self.holds[op] = self.holds.get(op, 0) + 1

    async def do_release_store(self) -> None:
        """Let the oldest held store operation run."""
        if self.held:
            self.held[0].set_result(None)

    async def do_read(self, label: str, op: str, record_id: int | None = None) -> None:
        """Start a read through the layer; it may block on the server."""
        self.tasks[label] = asyncio.get_running_loop().create_task(
            self.drive(label, op, "GET", record_id)
        )

    async def do_write(self, label: str, op: str, record_id: int | None = None) -> None:
        """Start a write (`update`, `delete`, `create`) through the layer."""
        method = {"update": "PATCH", "delete": "DELETE", "create": "POST"}[op]
        self.tasks[label] = asyncio.get_running_loop().create_task(
            self.drive(label, op, method, record_id)
        )

    async def do_release(self, label: str) -> None:
        """Deliver the response of the oldest pending request of `label`."""
        for pending in self.api.pending:
            if pending.label == label and not pending.gate.done():
                pending.gate.set_result(None)
                return

    async def do_batch(self, *steps: Step) -> None:
        """Several steps within one loop turn, with no settling between them (a tight race)."""
        for name, *args in steps:
            await getattr(self, f"do_{name}")(*args)

    async def do_close(self) -> None:
        """Close the layer and every subscriber, like closing the client."""
        self.closed = True
        await self.layer.aclose()
        for wiring in self.wirings.values():
            while wiring.stops:
                wiring.stops.pop()()
            await wiring.subscriber.aclose()

    async def drive(self, label: str, op: str, method: str, record_id: int | None) -> None:
        """What the client does for one request: the hooks around the transport."""
        _CURRENT.set(label)
        request = make_request(op, method=method, record_id=record_id)  # type: ignore[arg-type]
        short = await self.layer.on_request(request)
        if short is not None:
            self.note_read(op, short)
            return
        raw = await self.api.handle_async_request(request)
        response = await self.layer.on_response(request, raw)
        if method == "GET":
            self.note_read(op, response)
        else:
            self.landed.append(Landed(self.version_of(response), op, record_id))

    @staticmethod
    def version_of(response: httpx2.Response) -> int:
        """The server version in a response body."""
        return int(json.loads(response.content)["version"])

    def note_read(self, op: str, response: httpx2.Response) -> None:
        """I3: a fresh or stale answer needs the stream (stats/predict hits need only the ttl)."""
        how = response.extensions.get("craftainer_cache")
        if how == "stale" and not self.connected():
            self.violations.append(f"stale {op} served without a stream")
        if how == "hit" and op not in STATS_KINDS and not self.connected():
            self.violations.append(f"fresh {op} hit served without a stream")

    # -- invariants ---------------------------------------------------------------------------

    def connected(self) -> bool:
        """Oracle: some stream of the resource is up and alive."""
        return any(c.up and c.alive for c in self.conns)

    def entries(self) -> Iterator[tuple[str, str, CacheEntry]]:
        """The stored entries as (kind, subject, entry)."""
        for key, (_, entry) in list(self.memory._data.items()):
            _, _, kind, subject, *_ = key.split("/")
            yield kind, subject, entry

    @staticmethod
    def covers(landed: Landed, kind: str, subject: str) -> bool:
        """The spec's table: does this invalidation clear an entry of `kind`/`subject`?"""
        if landed.action == "reset":
            return kind != "filters"
        if kind in {"list", "stats", "predict"}:
            return True
        return landed.action != "create" and subject == str(landed.record_id)

    def check(self) -> None:
        """Assert every invariant; raise `Violation` with the trace otherwise."""
        problems = list(self.violations)
        now = self.clock.now
        for kind, subject, entry in self.entries():
            version = int(json.loads(entry.body)["version"])
            if entry.stale_until <= now:
                continue  # not served without a request
            for item in self.landed:
                if item.version > version and self.covers(item, kind, subject):
                    problems.append(
                        f"{kind}/{subject} (v{version}) is servable after {item.action} "
                        f"{item.record_id} (v{item.version}) landed"
                    )
        gate, connected = self.layer.events_active(RESOURCE), self.connected()
        # A held store clear delays a handshake's reset, and the gate opens only after it: while
        # one is held the gate may lag, but it must never be open without a stream.
        if gate != connected and not (self.held and connected and not gate):
            problems.append(f"gate {gate} but connected {connected}")
        self.awaiting_reset = [c for c in self.awaiting_reset if c.alive]
        if self.awaiting_reset and not self.held:
            problems.append("a handshake was processed without clearing the resource")
        if self.closed:
            problems.extend(self.leftover_work())
        else:
            for index, wiring in self.wirings.items():
                state = wiring.subscriber._state
                alive = state.task is not None and not state.task.done()
                if alive != bool(wiring.stops):
                    problems.append(
                        f"wiring {index}: reader alive={alive}, subscribers={wiring.stops}"
                    )
        if problems:
            trace = "\n  ".join(map(repr, self.trace))
            raise Violation("; ".join(problems) + f"\ntrace:\n  {trace}")

    def leftover_work(self) -> list[str]:
        """I4: after close nothing is scheduled."""
        out: list[str] = []
        if self.layer._tasks or self.layer._inflight:
            out.append("layer still has revalidation work after close")
        for index, wiring in self.wirings.items():
            state = wiring.subscriber._state
            if state.tasks or state.task is not None:
                out.append(f"wiring {index} still has reader tasks after close")
        return out
