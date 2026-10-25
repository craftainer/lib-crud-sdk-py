"""Cache key construction and the invalidation prefixes.

Keys are slash-separated:
``v1/{resource}/{kind}/{subject}/{api_version}/{scope}/{canonicalQuery}``. Versions of one
resource never share an entry, but they share one event stream; because the version comes after
kind and subject, one prefix such as ``v1/{resource}/record/{id}/`` clears an entry in every
version and for every caller at once.
"""

__all__ = [
    "KINDS",
    "STATS_KINDS",
    "UNVERSIONED",
    "build_key",
    "canonical_query",
    "event_prefixes",
    "kind_prefix",
    "manifest_key",
    "prefix_parts",
    "record_prefix",
    "resource_prefix",
    "resource_prefixes",
    "revisions_prefix",
    "subject_for",
]

KINDS = frozenset({"list", "record", "stats", "predict", "revisions", "filters"})
STATS_KINDS = frozenset({"stats", "predict"})
UNVERSIONED = "_"
_COLLECTION_KINDS = ("list", "stats", "predict")
_PER_RECORD_KINDS = ("record", "revisions")


def canonical_query(query: str | bytes) -> str:
    """Sort the query pairs and drop ``id`` (it is the subject).

    The client already emits canonical, percent-encoded values, so values are kept verbatim:
    only the order is normalised.
    """
    text = query.decode("ascii") if isinstance(query, bytes) else query
    pairs = [part for part in text.split("&") if part and part.partition("=")[0] != "id"]
    pairs.sort(key=lambda part: part.partition("="))
    return "&".join(pairs)


def subject_for(kind: str, record_id: int | None) -> str:
    """The subject segment: the record id for per-record kinds, ``_`` otherwise."""
    if kind in _PER_RECORD_KINDS and record_id is not None:
        return str(record_id)
    return "_"


def build_key(  # noqa: PLR0913  # a key is these independent facts
    resource: str,
    kind: str,
    record_id: int | None,
    scope: str,
    query: str | bytes = "",
    *,
    api_version: str = UNVERSIONED,
) -> str:
    """Build the key for one cached response."""
    subject = subject_for(kind, record_id)
    return f"v1/{resource}/{kind}/{subject}/{api_version}/{scope}/{canonical_query(query)}"


def manifest_key(scope: str) -> str:
    """Key of the manifest document for one caller scope."""
    return f"v1/_manifest/{scope}"


def prefix_parts(prefix: str | None) -> tuple[str, str | None] | None:
    """The ``(resource, kind)`` a key prefix names (``kind`` is ``None`` for a whole resource).

    ``None`` if the prefix does not name a resource (empty, foreign or ``manifest``-level).
    """
    parts = (prefix or "").split("/")
    if len(parts) < 2 or parts[0] != "v1" or not parts[1]:  # noqa: PLR2004
        return None
    return parts[1], (parts[2] or None) if len(parts) > 2 else None  # noqa: PLR2004


def resource_prefix(resource: str) -> str:
    """Prefix of everything cached for ``resource``, in every API version."""
    return f"v1/{resource}/"


def kind_prefix(resource: str, kind: str) -> str:
    """Prefix of all entries of one kind for ``resource`` (every scope and subject)."""
    return f"v1/{resource}/{kind}/"


def record_prefix(resource: str, record_id: int) -> str:
    """Prefix of the ``record`` entries of one record (every scope and query)."""
    return f"v1/{resource}/record/{record_id}/"


def revisions_prefix(resource: str, record_id: int) -> str:
    """Prefix of the ``revisions`` entries of one record."""
    return f"v1/{resource}/revisions/{record_id}/"


def resource_prefixes(resource: str) -> list[str]:
    """Everything cached for ``resource`` except ``filters`` (a reconnect or unknown change)."""
    return [kind_prefix(resource, kind) for kind in (*_COLLECTION_KINDS, *_PER_RECORD_KINDS)]


def event_prefixes(resource: str, action: str, record_id: int | None) -> list[str]:
    """Prefixes an SSE event clears, per the spec's table.

    ``create`` clears the collection kinds; ``update``/``delete``/``restore`` also clear the
    record and its revisions; an unknown action or a missing record id clears the resource.
    """
    collections = [kind_prefix(resource, kind) for kind in _COLLECTION_KINDS]
    if action == "create":
        return collections
    if action in {"update", "delete", "restore"} and record_id is not None:
        return [
            *collections,
            record_prefix(resource, record_id),
            revisions_prefix(resource, record_id),
        ]
    return resource_prefixes(resource)
