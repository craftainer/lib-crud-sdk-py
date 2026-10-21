"""Scripted and seeded interleavings of the cache layer and the SSE subscribers.

Every script is a race that a review round found (or the spec forbids); the invariants of
`world.World.check` run after each step, so a regression names the step that broke it.
"""

import asyncio
import random

import httpx2
import pytest

from .world import Landed, Step, Violation, World

pytestmark = pytest.mark.asyncio

UP: list[Step] = [("subscribe", 0), ("hello", 0)]

SCRIPTS: dict[str, tuple[list[Step], bool]] = {
    # An event lands while the read it makes stale is in flight: never stored fresh.
    "event-lands-while-read-in-flight": (
        [
            *UP,
            ("read", "r1", "list"),
            ("mutate", "update", 1),
            ("deliver", 0),
            ("release", "r1"),
            ("read", "r2", "list"),
            ("release", "r2"),
        ],
        False,
    ),
    # The event lands between the response and the store write: the entry is deleted again.
    "event-lands-while-storing": (
        [
            *UP,
            ("hold", "set"),
            ("read", "r1", "record", 1),
            ("release", "r1"),
            ("mutate", "update", 1),
            ("deliver", 0),
            ("release_store",),
            ("read", "r2", "record", 1),
        ],
        False,
    ),
    # A 304 priced before an invalidation must not become fresh afterwards.
    "revalidation-priced-before-an-event": (
        [
            *UP,
            ("read", "r1", "list"),
            ("release", "r1"),
            ("advance", 200),
            ("read", "r2", "list"),
            ("mutate", "update", 1),
            ("deliver", 0),
            ("release", "r2"),
            ("read", "r3", "list"),
        ],
        False,
    ),
    # A write in flight bumps the generation for reads that were sent before it.
    "write-while-read-in-flight": (
        [
            *UP,
            ("read", "r1", "list"),
            ("write", "w1", "update", 1),
            ("release", "w1"),
            ("deliver", 0),
            ("release", "r1"),
            ("read", "r2", "list"),
        ],
        False,
    ),
    "write-through-then-delete-event": (
        [
            *UP,
            ("write", "w1", "update", 1),
            ("release", "w1"),
            ("deliver", 0),
            ("mutate", "delete", 1),
            ("deliver", 0),
            ("read", "r1", "record", 1),
        ],
        False,
    ),
    "create-event-keeps-records-fresh-and-clears-lists": (
        [
            *UP,
            ("read", "r1", "record", 1),
            ("release", "r1"),
            ("read", "r2", "list"),
            ("release", "r2"),
            ("mutate", "create", 3),
            ("deliver", 0),
            ("read", "r3", "record", 1),
            ("read", "r4", "list"),
            ("release", "r4"),
        ],
        False,
    ),
    # The first handshake clears what a persistent store kept from before the process.
    "first-handshake-clears-preloaded-entries": (
        [("subscribe", 0), ("read", "r0", "list"), ("hello", 0), ("read", "r1", "list")],
        True,
    ),
    # A reconnect covers the events lost in the gap.
    "reconnect-clears-the-gap": (
        [
            *UP,
            ("read", "r1", "list"),
            ("release", "r1"),
            ("mutate", "update", 1),
            ("drop", 0),
            ("advance", 1),
            ("hello", 0),
            ("read", "r2", "list"),
        ],
        False,
    ),
    "refused-connection-backs-off-then-connects": (
        [("refuse", 0), ("subscribe", 0), ("advance", 1), ("hello", 0), ("advance", 5)],
        False,
    ),
    "clean-eof-reconnects": (
        [*UP, ("eof", 0), ("read", "r1", "list"), ("release", "r1"), ("advance", 1), ("hello", 0)],
        False,
    ),
    # The gate is open iff at least one stream is connected.
    "gate-stays-open-while-one-of-two-streams-is-up": (
        [
            ("subscribe", 0),
            ("subscribe", 1),
            ("hello", 0),
            ("hello", 1),
            ("drop", 0),
            ("read", "r1", "list"),
            ("release", "r1"),
            ("read", "r2", "list"),
            ("drop", 1),
            ("read", "r3", "list"),
        ],
        False,
    ),
    "a-second-listener-on-a-connected-stream-sees-it-up": (
        [
            *UP,
            ("subscribe", 0),
            ("unsubscribe", 0),
            ("read", "r1", "list"),
            ("release", "r1"),
            ("unsubscribe", 0),
            ("read", "r2", "list"),
        ],
        False,
    ),
    # No fresh or SWR hit without the stream (stats keep their ttl).
    "swr-ends-when-the-stream-drops": (
        [
            *UP,
            ("read", "r1", "list"),
            ("release", "r1"),
            ("advance", 70),
            ("read", "r2", "list"),
            ("drop", 0),
            ("read", "r3", "list"),
            ("release", "r3"),
            ("release", "r2"),
        ],
        False,
    ),
    "swr-of-stats-needs-the-stream": (
        [
            *UP,
            ("read", "r1", "stats"),
            ("release", "r1"),
            ("advance", 40),
            ("drop", 0),
            ("read", "r2", "stats"),
            ("release", "r2"),
        ],
        False,
    ),
    "stats-ttl-without-a-stream": (
        [
            ("read", "r1", "stats"),
            ("release", "r1"),
            ("advance", 10),
            ("read", "r2", "stats"),
            ("advance", 40),
            ("read", "r3", "stats"),
            ("release", "r3"),
        ],
        False,
    ),
    # After close nothing is scheduled.
    "close-with-a-revalidation-in-flight": (
        [
            *UP,
            ("read", "r1", "list"),
            ("release", "r1"),
            ("advance", 70),
            ("read", "r2", "list"),
            ("close",),
            ("read", "r3", "list"),
            ("release", "r2"),
        ],
        False,
    ),
    "close-while-connecting-and-reading": (
        [("subscribe", 0), ("read", "r1", "list"), ("close",), ("hello", 0), ("release", "r1")],
        False,
    ),
    # Unsubscribe / subscribe in any order leaves a live reader iff there is a subscriber.
    "unsubscribe-before-the-handshake": (
        [("subscribe", 0), ("unsubscribe", 0), ("hello", 0), ("subscribe", 0), ("hello", 0)],
        False,
    ),
    "resubscribe-in-the-same-loop-turn": (
        [
            *UP,
            ("batch", ("unsubscribe", 0), ("subscribe", 0)),
            ("hello", 0),
            ("batch", ("subscribe", 0), ("unsubscribe", 0), ("unsubscribe", 0)),
        ],
        False,
    ),
    "unsubscribe-everything-then-subscribe-again": (
        [
            ("subscribe", 0),
            ("subscribe", 0),
            ("hello", 0),
            ("unsubscribe", 0),
            ("unsubscribe", 0),
            ("unsubscribe", 0),
            ("subscribe", 0),
            ("hello", 0),
        ],
        False,
    ),
}


async def play(steps: list[Step], *, preloaded: bool, monkeypatch: pytest.MonkeyPatch) -> World:
    """Run one interleaving on a fresh world."""
    world = World(preloaded=preloaded)
    monkeypatch.setattr(asyncio, "sleep", world.fake_sleep)
    await world.run(steps)
    return world


@pytest.mark.parametrize("name", sorted(SCRIPTS))
async def test_scripted_interleaving(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each scripted race keeps every invariant at every step."""
    steps, preloaded = SCRIPTS[name]
    world = await play(steps, preloaded=preloaded, monkeypatch=monkeypatch)
    assert world.trace[: len(steps)] == steps


async def test_the_harness_detects_a_broken_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate that disagrees with the connections is reported with the step trace."""
    world = World()
    monkeypatch.setattr(asyncio, "sleep", world.fake_sleep)
    await world.step(("subscribe", 0))
    await world.step(("hello", 0))
    world.layer.set_events_active("heroes", False)  # sabotage
    with pytest.raises(Violation, match="gate False but connected True"):
        world.check()
    world.layer.set_events_active("heroes", True)
    await world.finish()


async def test_the_harness_detects_a_fresh_entry_after_an_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry older than a landed, covering invalidation must not be servable."""
    world = World(preloaded=True)
    monkeypatch.setattr(asyncio, "sleep", world.fake_sleep)
    world.landed.append(Landed(world.version, "update", 1))
    with pytest.raises(Violation, match=r"record/1 .* is servable after update 1"):
        world.check()
    world.landed[0] = Landed(world.version, "update", 2)  # another record: only list/stats
    with pytest.raises(Violation, match="list/_"):
        world.check()
    world.landed.clear()
    await world.finish()


async def test_the_harness_detects_a_stale_hit_without_a_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I3 is checked on every read result."""
    world = World()
    monkeypatch.setattr(asyncio, "sleep", world.fake_sleep)
    response = httpx2.Response(200, extensions={"craftainer_cache": "stale"})
    world.note_read("list", response)
    response = httpx2.Response(200, extensions={"craftainer_cache": "hit"})
    world.note_read("list", response)
    world.note_read("stats", response)  # stats keep their ttl
    with pytest.raises(Violation, match=r"stale list served.*fresh list hit"):
        world.check()
    world.violations.clear()
    await world.finish()


SEEDS = range(200)
STEPS_PER_RUN = 28
OPS = ("list", "record", "stats", "predict")
MUTATIONS = ("create", "update", "delete")


class Explorer:
    """Draws the next step of a seeded random interleaving from the world's current state."""

    def __init__(self, seed: int, world: World) -> None:
        """Seed the draw."""
        self.rng = random.Random(seed)  # noqa: S311  # fixed seeds for reproducible runs, not cryptography
        self.world = world
        self.labels = 0

    def label(self, prefix: str) -> str:
        """A fresh label."""
        self.labels += 1
        return f"{prefix}{self.labels}"

    def next(self) -> Step:
        """One step; always valid for the current state."""
        rng, world = self.rng, self.world
        wiring = rng.randrange(2)
        pending = sorted({p.label for p in world.api.pending})
        choices: list[Step] = [
            (
                "read",
                self.label("r"),
                (op := rng.choice(OPS)),
                rng.choice([1, 2]) if op == "record" else None,
            ),
            (
                "write",
                self.label("w"),
                (m := rng.choice(MUTATIONS)),
                None if m == "create" else rng.choice([1, 2]),
            ),
            ("mutate", rng.choice(MUTATIONS), rng.choice([1, 2])),
            ("deliver", wiring, rng.choice([1, 2, 5])),
            ("advance", rng.choice([1, 5, 35, 70, 200])),
            ("drop", wiring),
            ("eof", wiring),
            ("refuse", wiring),
            ("hold", rng.choice(["set", "clear"])),
            ("release_store",),
            ("unsubscribe", wiring, rng.randrange(3)),
        ]
        if not world.closed:
            choices += [
                ("batch", ("unsubscribe", wiring), ("subscribe", wiring)),
                ("batch", ("subscribe", wiring), ("unsubscribe", wiring, 1)),
            ]
            choices += [("subscribe", wiring), ("hello", wiring), ("hello", wiring)]
            choices += [("close",)] * (1 if len(world.trace) > STEPS_PER_RUN // 2 else 0)
        if pending:
            choices += [("release", rng.choice(pending))] * 4
        return rng.choice(choices)


async def test_seeded_random_interleavings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixed seeds: random but reproducible interleavings, every invariant at every step."""
    steps = 0
    for seed in SEEDS:
        world = World(preloaded=seed % 3 == 0)
        monkeypatch.setattr(asyncio, "sleep", world.fake_sleep)
        explorer = Explorer(seed, world)
        try:
            for _ in range(STEPS_PER_RUN):
                await world.step(explorer.next())
            await world.finish()
        except Violation as exc:
            raise AssertionError(f"seed {seed}: {exc}") from exc
        steps += len(world.trace)
    assert steps > len(SEEDS) * STEPS_PER_RUN
