"""Tests for the sync client, resource handle, hooks and transports."""

import dataclasses
from collections.abc import Callable, Sequence
from typing import cast

import httpx2
import pytest

from craftainer_crud_sdk import (
    EXTENSION_KEY,
    BindsTransport,
    CapabilityError,
    ConnectionFailed,
    CrudClient,
    HookedTransport,
    InvalidQueryError,
    ManifestError,
    NotFound,
    PreconditionFailed,
    RequestContext,
    SendRaw,
    ServerError,
    SyncSendRaw,
    TokenProvider,
    TransportHook,
    UnknownResourceError,
    ValidationFailed,
)

from ._support import (
    BASE,
    CATALOG,
    HEROES_V1,
    HEROES_V2,
    MANIFEST_PATH,
    load_doc,
    make_transport,
)

JSON = {"id": 1, "name": "Ada"}


class Recorder:
    """A sync hook that logs its calls into a shared list."""

    def __init__(self, name: str, log: list[str], short: httpx2.Response | None = None) -> None:
        """Set up the double."""
        self.name = name
        self.log = log
        self.short = short

    def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        """On request."""
        if request.extensions.get(EXTENSION_KEY, {}).get("op") == "manifest":
            return None
        self.log.append(f"req:{self.name}")
        return self.short

    def on_response(self, request: httpx2.Request, response: httpx2.Response) -> httpx2.Response:
        """On response."""
        if request.extensions.get(EXTENSION_KEY, {}).get("op") != "manifest":
            self.log.append(f"resp:{self.name}")
        return response


class Binder(Recorder):
    """A hook that implements BindsTransport."""

    send: SyncSendRaw | SendRaw | None = None

    def bind(self, send: SyncSendRaw | SendRaw) -> None:
        """Bind."""
        self.send = send


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response] | None = None,
    *,
    hooks: Sequence[TransportHook] = (),
    seen: list[httpx2.Request] | None = None,
    token_provider: TokenProvider | None = None,
    manifest_url: str | None = None,
) -> CrudClient:
    transport = make_transport(handler or (lambda r: httpx2.Response(200, json=JSON)), seen=seen)
    return CrudClient(
        BASE,
        transport=transport,
        hooks=hooks,
        token_provider=token_provider,
        manifest_url=manifest_url,
    )


def _ctx(request: httpx2.Request) -> RequestContext:
    context: RequestContext = request.extensions[EXTENSION_KEY]
    return context


def _last(seen: list[httpx2.Request]) -> httpx2.Request:
    return seen[-1]


def test_resource_lookup_and_unknown() -> None:
    """Test resource lookup and unknown."""
    client = _client()
    assert client.resource("heroes").resource.api_version == "v2"
    assert client.resource("heroes", "v1").resource.api_version == "v1"
    with pytest.raises(UnknownResourceError, match="heroes' v9"):
        client.resource("heroes", "v9")
    with pytest.raises(UnknownResourceError, match="ghost"):
        client.resource("ghost")


def test_manifest_loaded_once_and_refreshable() -> None:
    """Test manifest loaded once and refreshable."""
    seen: list[httpx2.Request] = []
    client = _client(seen=seen)
    first = client.manifest()
    assert client.manifest() is first
    client.resource("heroes")
    assert [r.url.path for r in seen] == ["/.well-known/api-catalog", MANIFEST_PATH]
    client.manifest(refresh=True)
    assert len(seen) == 4


def test_direct_manifest_url_skips_discovery() -> None:
    """Test direct manifest url skips discovery."""
    seen: list[httpx2.Request] = []
    client = _client(seen=seen, manifest_url=f"{BASE}{MANIFEST_PATH}")
    client.manifest()
    assert [r.url.path for r in seen] == [MANIFEST_PATH]


def test_reads() -> None:
    """Test reads."""
    seen: list[httpx2.Request] = []
    page = [{"id": 1}]
    client = _client(lambda r: httpx2.Response(200, json=page), seen=seen)
    heroes = client.resource("heroes")
    assert heroes.list({"name__icontains": "a b"}, "-name", skip=5, limit=10) == page
    request = _last(seen)
    assert request.method == "GET"
    assert request.url.raw_path.decode() == (
        f"{HEROES_V2}?name__icontains=a%20b&sort=-name&skip=5&limit=10"
    )
    assert _ctx(request) == {
        "resource": "heroes", "op": "list", "record_id": None,
        "api_version": "v2", "scope": "anon",
    }  # fmt: skip
    assert request.headers["accept"] == "application/json"

    heroes.get(4)
    assert _last(seen).url.raw_path.decode() == f"{HEROES_V2}?id=4"
    assert _ctx(_last(seen))["op"] == "record"
    assert _ctx(_last(seen))["record_id"] == 4

    heroes.revisions(4)
    assert _last(seen).url.path == f"{HEROES_V2}/revisions"
    assert _last(seen).url.query == b"id=4"
    assert _ctx(_last(seen))["op"] == "revisions"


def test_writes() -> None:
    """Test writes."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        if request.method == "DELETE" and b"id=" in request.url.query:
            return httpx2.Response(204)
        return httpx2.Response(200, json={"matched": 1, "ids": [1]})

    client = _client(handler, seen=seen)
    heroes = client.resource("heroes")

    heroes.create({"name": "A"})
    request = _last(seen)
    assert (request.method, request.url.path, _ctx(request)["op"]) == ("POST", HEROES_V2, "create")
    assert request.content == b'{"name":"A"}'

    heroes.update(3, {"name": "B"}, if_match='"e"')
    request = _last(seen)
    assert (request.method, request.url.query, request.headers["if-match"]) == (
        "PATCH", b"id=3", '"e"',
    )  # fmt: skip
    assert _ctx(request)["record_id"] == 3
    heroes.update(3, {"name": "B"})
    assert "if-match" not in _last(seen).headers

    assert heroes.update_many({"power_level__min": 1}, {"name": "C"}) == {"matched": 1, "ids": [1]}
    assert (_last(seen).method, _last(seen).url.query) == ("PATCH", b"power_level__min=1")
    assert _ctx(_last(seen))["op"] == "bulk_update"

    heroes.delete(3, if_match="*")
    assert (_last(seen).method, _last(seen).headers["if-match"]) == ("DELETE", "*")
    assert _ctx(_last(seen))["op"] == "delete"

    assert heroes.delete_many({"name": "x"})["matched"] == 1
    assert _ctx(_last(seen))["op"] == "bulk_delete"

    heroes.restore(9)
    assert (_last(seen).method, _last(seen).url.path, _last(seen).url.query) == (
        "POST", f"{HEROES_V2}/restore", b"id=9",
    )  # fmt: skip
    assert _ctx(_last(seen))["record_id"] == 9
    heroes.restore(filters={"is_locked": False})
    assert _last(seen).url.query == b"is_locked=false"


def test_stats_predict_events() -> None:
    """Test stats predict events."""
    seen: list[httpx2.Request] = []
    client = _client(seen=seen)
    heroes = client.resource("heroes")
    heroes.stats()
    assert (_last(seen).url.path, _last(seen).url.query) == (f"{HEROES_V2}/stats", b"")
    heroes.stats("week", include_archived=True, include_unpublished=False)
    assert _last(seen).url.query == b"bucket=week&include_archived=true&include_unpublished=false"
    assert _ctx(_last(seen))["op"] == "stats"
    heroes.predict()
    assert _last(seen).url.query == b""
    heroes.predict("power_level", 3, "day")
    assert _last(seen).url.path == f"{HEROES_V2}/predict"
    assert _last(seen).url.query == b"field=power_level&periods=3&bucket=day"
    assert _ctx(_last(seen))["op"] == "predict"
    assert heroes.events_url() == f"{BASE}{HEROES_V2}/events"


def test_iter_all_pages_until_short_page() -> None:
    """Test iter all pages until short page."""
    seen: list[httpx2.Request] = []
    sizes = {0: 2, 2: 2, 4: 1}

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        skip = int(request.url.params.get("skip", "0"))
        return httpx2.Response(200, json=[{"id": skip + i} for i in range(sizes[skip])])

    heroes = _client(handler, seen=seen).resource("heroes")
    assert [r["id"] for r in heroes.iter_all(limit=2, sort="name")] == [0, 1, 2, 3, 4]
    queries = [r.url.query.decode() for r in seen[2:]]
    assert queries == [
        "sort=name&skip=0&limit=2",
        "sort=name&skip=2&limit=2",
        "sort=name&skip=4&limit=2",
    ]


def test_iter_all_stops_after_an_empty_page_and_uses_default_limit() -> None:
    """Test iter all stops after an empty page and uses default limit."""
    seen: list[httpx2.Request] = []
    heroes = _client(
        lambda r: (
            httpx2.Response(200, json=[{"id": i} for i in range(100)])
            if r.url.params["skip"] == "0"
            else httpx2.Response(200, json=[])
        ),
        seen=seen,
    ).resource("heroes")
    assert len(list(heroes.iter_all())) == 100
    assert seen[-1].url.query == b"skip=100&limit=100"


def test_capability_gating() -> None:
    """Test capability gating."""
    client = _client()
    v1 = client.resource("heroes", "v1")
    quests = client.resource("quests")
    for call in (
        v1.stats,
        v1.predict,
        lambda: v1.revisions(1),
        lambda: v1.restore(1),
        quests.events_url,
    ):
        with pytest.raises(CapabilityError, match="does not support"):
            call()
    no_bulk = dataclasses.replace(
        quests.resource,
        capabilities=dataclasses.replace(
            quests.resource.capabilities, bulk_update=False, bulk_delete=False
        ),
    )
    handle = type(quests)(client, no_bulk)
    with pytest.raises(CapabilityError, match="bulk_update"):
        handle.update_many({"title": "x"}, {})
    with pytest.raises(CapabilityError, match="bulk_delete"):
        handle.delete_many({"title": "x"})
    # capability on but the path missing from the manifest
    no_path = dataclasses.replace(
        client.resource("heroes").resource, paths={"filters": "/f", "clone": "/c"}
    )
    with pytest.raises(CapabilityError):
        type(quests)(client, no_path).stats()


def test_invalid_arguments_raise_before_sending() -> None:
    """Test invalid arguments raise before sending."""
    seen: list[httpx2.Request] = []
    heroes = _client(seen=seen).resource("heroes")
    n = len(seen)
    calls: list[Callable[[], object]] = [
        lambda: heroes.list({"nope": 1}),
        lambda: heroes.list(limit=1001),
        lambda: heroes.get(-1),
        lambda: heroes.update_many({}, {"name": "x"}),
        lambda: heroes.delete_many({}),
        heroes.restore,
        lambda: heroes.restore(1, {"name": "x"}),
        lambda: heroes.restore(filters={}),
        lambda: heroes.restore(1, {}),
        lambda: heroes.predict("name"),
        lambda: heroes.predict("nope"),
        lambda: heroes.predict(periods=0),
        lambda: heroes.predict(periods=True),
    ]
    for call in calls:
        with pytest.raises(InvalidQueryError):
            call()
    assert len(seen) == n


def test_server_errors_raise_problem_classes() -> None:
    """Test server errors raise problem classes."""
    status = {"code": 404, "body": {"title": "Missing", "detail": "no hero"}}

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        return httpx2.Response(status["code"], json=status["body"])  # type: ignore[arg-type]

    heroes = _client(handler).resource("heroes")
    with pytest.raises(NotFound, match="no hero") as info:
        heroes.get(1)
    assert info.value.response is not None
    status.update(code=412, body={})
    with pytest.raises(PreconditionFailed):
        heroes.update(1, {}, if_match='"x"')
    status.update(code=422, body={"detail": [{"loc": ["body"], "msg": "m", "type": "t"}]})
    with pytest.raises(ValidationFailed):
        heroes.create({})
    status.update(code=500, body={})
    with pytest.raises(ServerError):
        heroes.list()


def test_transport_failure_is_connection_failed() -> None:
    """Test transport failure is connection failed."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        raise httpx2.ConnectError("refused", request=request)

    heroes = _client(handler).resource("heroes")
    with pytest.raises(ConnectionFailed, match="refused") as info:
        heroes.list()
    assert isinstance(info.value.__cause__, httpx2.ConnectError)


def test_manifest_failures() -> None:
    """Test manifest failures."""

    def failing(path: str, response: httpx2.Response | Exception) -> CrudClient:
        """Failing."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            """Handler."""
            if request.url.path == path:
                if isinstance(response, Exception):
                    raise response
                return response
            return httpx2.Response(200, json={"linkset": [{"describedby": [
                {"href": MANIFEST_PATH, "profile": "https://craftainer.github.io/schemas/crud-manifest/v1"}
            ]}]})  # fmt: skip

        return CrudClient(BASE, transport=httpx2.MockTransport(handler))

    with pytest.raises(ServerError):
        failing(MANIFEST_PATH, httpx2.Response(500, json={"title": "x"})).manifest()
    with pytest.raises(ManifestError, match="manifest_version"):
        failing(MANIFEST_PATH, httpx2.Response(200, json={"manifest_version": "9"})).manifest()
    with pytest.raises(ManifestError):
        failing(MANIFEST_PATH, httpx2.Response(200, text="not json")).manifest()
    with pytest.raises(ConnectionFailed):
        failing(MANIFEST_PATH, httpx2.ConnectError("down")).manifest()
    with pytest.raises(ManifestError, match="describedby"):
        failing("/.well-known/api-catalog", httpx2.Response(200, json={})).manifest()


def test_token_provider_adds_bearer_scope_and_refreshes_once() -> None:
    """Test token provider adds bearer scope and refreshes once."""
    headers: list[str] = []
    scopes: list[str] = []

    def provider(*, refresh: bool = False) -> str | None:
        """Provider."""
        return "t2" if refresh else "t1"

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        headers.append(request.headers["authorization"])
        scopes.append(_ctx(request)["scope"])
        return httpx2.Response(401 if len(headers) == 1 else 200, json=[])

    client = _client(handler, token_provider=provider)
    assert client.resource("heroes").list() == []
    assert headers == ["Bearer t1", "Bearer t2"]
    assert scopes[0] != "anon"
    assert scopes[0] != scopes[1]


def test_hook_order_short_circuit_and_reverse_responses() -> None:
    """Test hook order short circuit and reverse responses."""
    log: list[str] = []
    cached = httpx2.Response(200, json=[{"id": 9}])
    hooks = (
        Recorder("a", log),
        Recorder("b", log, short=cached),
        Recorder("c", log),
    )
    heroes = _client(hooks=hooks).resource("heroes")
    log.clear()
    assert heroes.list() == [{"id": 9}]
    assert log == ["req:a", "req:b", "resp:a"]


def test_hooks_see_network_responses_in_reverse() -> None:
    """Test hooks see network responses in reverse."""
    log: list[str] = []
    heroes = _client(hooks=(Recorder("a", log), Recorder("b", log))).resource("heroes")
    log.clear()
    heroes.get(1)
    assert log == ["req:a", "req:b", "resp:b", "resp:a"]


def test_hook_may_replace_the_response() -> None:
    """Test hook may replace the response."""

    class Replacer(Recorder):
        """Replacer."""

        def on_response(
            self, request: httpx2.Request, response: httpx2.Response
        ) -> httpx2.Response:
            """On response."""
            if request.extensions.get(EXTENSION_KEY, {}).get("op") == "manifest":
                return response
            return httpx2.Response(200, json=[{"id": "replaced"}])

    heroes = _client(hooks=(Replacer("r", []),)).resource("heroes", "v1")
    assert heroes.list() == [{"id": "replaced"}]


def test_bind_sends_past_the_hooks() -> None:
    """Test bind sends past the hooks."""
    log: list[str] = []
    binder = Binder("a", log)
    assert isinstance(binder, BindsTransport)
    seen: list[httpx2.Request] = []
    client = _client(hooks=(binder, Recorder("b", log)), seen=seen)
    client.manifest()
    log.clear()
    assert binder.send is not None
    response = cast("SyncSendRaw", binder.send)(httpx2.Request("GET", f"{BASE}{HEROES_V1}"))
    assert response.status_code == 200
    assert log == []  # no hook ran for the raw send
    assert seen[-1].url.path == HEROES_V1


def test_hooked_transport_directly_and_close() -> None:
    """Test hooked transport directly and close."""
    closed: list[bool] = []

    class Inner(httpx2.BaseTransport):
        """Inner."""

        def handle_request(self, request: httpx2.Request) -> httpx2.Response:
            """Handle request."""
            return httpx2.Response(200)

        def close(self) -> None:
            """Close."""
            closed.append(True)

    transport = HookedTransport(Inner(), [])
    assert transport.handle_request(httpx2.Request("GET", BASE)).status_code == 200
    transport.close()
    assert closed == [True]


def test_timeout_and_headers_passthrough() -> None:
    """Test timeout and headers passthrough."""
    seen: list[httpx2.Request] = []
    transport = make_transport(lambda r: httpx2.Response(200, json=[]), seen=seen)
    client = CrudClient(BASE, transport=transport, timeout=3.0, headers={"X-App": "t"})
    client.resource("heroes").list()
    assert seen[-1].headers["x-app"] == "t"
    assert seen[-1].extensions["timeout"]["read"] == 3.0


def test_close_and_context_manager() -> None:
    """Test close and context manager."""
    with CrudClient(BASE, transport=make_transport(lambda r: httpx2.Response(200))) as client:
        assert client.base_url == BASE
    assert client._http.is_closed

    default = CrudClient(BASE)
    default.close()
    assert default._http.is_closed


def test_clone_draft_publish_filters() -> None:
    """Test clone draft publish filters."""
    seen: list[httpx2.Request] = []
    heroes = _client(seen=seen).resource("heroes")
    heroes.clone(4)
    assert (_last(seen).method, _last(seen).url.path, _last(seen).url.query) == (
        "POST", f"{HEROES_V2}/clone", b"id=4",
    )  # fmt: skip
    assert _ctx(_last(seen))["op"] == "clone"
    assert _ctx(_last(seen))["record_id"] == 4
    heroes.create_draft({"name": "D"})
    assert (_last(seen).method, _last(seen).url.path) == ("POST", f"{HEROES_V2}/draft")
    assert _last(seen).content == b'{"name":"D"}'
    assert _ctx(_last(seen))["op"] == "draft"
    heroes.publish(4)
    assert (_last(seen).url.path, _last(seen).url.query) == (f"{HEROES_V2}/publish", b"id=4")
    assert _ctx(_last(seen))["op"] == "publish"
    heroes.filters()
    assert (_last(seen).method, _last(seen).url.path) == ("GET", f"{HEROES_V2}/filters")
    assert _ctx(_last(seen))["op"] == "filters"


def test_clone_draft_publish_gating() -> None:
    """Test clone draft publish gating."""
    client = _client()
    v1 = client.resource("heroes", "v1")
    with pytest.raises(CapabilityError, match="draft"):
        v1.create_draft({})
    with pytest.raises(CapabilityError, match="publish"):
        v1.publish(1)
    caps = dataclasses.replace(v1.resource.capabilities, clone=False)
    no_clone = type(v1)(client, dataclasses.replace(v1.resource, capabilities=caps))
    with pytest.raises(CapabilityError, match="clone"):
        no_clone.clone(1)


def test_restore_needs_exactly_one_of_id_and_filters() -> None:
    """Both given and neither given are rejected before sending."""
    seen: list[httpx2.Request] = []
    heroes = _client(seen=seen).resource("heroes")
    n = len(seen)
    with pytest.raises(InvalidQueryError, match="exactly one"):
        heroes.restore(1, {"is_locked": False})
    with pytest.raises(InvalidQueryError, match="exactly one"):
        heroes.restore()
    assert len(seen) == n


def test_paging_parameter_names_come_from_the_manifest() -> None:
    """A manifest with offset/page_size/order is honoured by list and iter_all."""
    doc = load_doc()
    for res in doc["resources"]:
        res["pagination"].update(skip_param="offset", limit_param="page_size", sort_param="order")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Serve the catalog, the doctored manifest, and short pages."""
        if request.url.path == "/.well-known/api-catalog":
            return httpx2.Response(200, json=CATALOG)
        if request.url.path == MANIFEST_PATH:
            return httpx2.Response(200, json=doc)
        seen.append(request)
        offset = int(request.url.params.get("offset", "0"))
        return httpx2.Response(
            200, json=[{"id": offset + i} for i in range(2 if offset < 2 else 1)]
        )

    client = CrudClient(BASE, transport=httpx2.MockTransport(handler))
    heroes = client.resource("heroes")
    assert [r["id"] for r in heroes.iter_all(limit=2, sort="name")] == [0, 1, 2]
    queries = [r.url.query.decode() for r in seen]
    assert queries == [
        "order=name&offset=0&page_size=2",
        "order=name&offset=2&page_size=2",
    ]
    seen.clear()
    heroes.list(sort="name", skip=3, limit=4)
    assert seen[0].url.query == b"order=name&offset=3&page_size=4"


def test_delete_ignores_an_empty_or_non_json_success_body() -> None:
    """A successful delete is not an error because its body is empty or not JSON."""
    for response in (httpx2.Response(200), httpx2.Response(202, text="accepted")):
        heroes = _client(lambda r, response=response: response).resource("heroes")  # type: ignore[misc]
        heroes.delete(3)


def test_close_still_closes_http_and_joins_when_a_closer_raises() -> None:
    """A failing closer does not leak the HTTP client or skip the joins."""
    client = _client()
    joined: list[bool] = []

    def boom() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    client.add_closer(boom, lambda: joined.append(True))
    with pytest.raises(RuntimeError, match="boom"):
        client.close()
    assert client._http.is_closed
    assert joined == [True]


def test_iter_all_rejects_a_zero_limit() -> None:
    """`limit=0` is an error, not the default limit."""
    heroes = _client().resource("heroes")
    with pytest.raises(InvalidQueryError):
        next(iter(heroes.iter_all(limit=0)))
