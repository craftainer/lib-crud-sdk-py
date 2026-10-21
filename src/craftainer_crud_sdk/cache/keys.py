"""Cache key construction and the invalidation prefixes.

Keys are slash-separated:
``v1/{resource}/{api_version}/{kind}/{subject}/{scope}/{canonicalQuery}``. Versions of one
resource never share an entry, but they share one event stream, so event and write invalidation
clears ``v1/{resource}/`` across every version (and every caller) at once.
"""

__all__ = [
    "KINDS",
    "STATS_KINDS",
    "UNVERSIONED",
    "build_key",
    "canonical_query",
    "kind_prefix",
    "manifest_key",
    "record_prefix",
    "resource_prefix",
    "subject_for",
]

KINDS = frozenset({"list", "record", "stats", "predict", "revisions", "filters"})
STATS_KINDS = frozenset({"stats", "predict"})
UNVERSIONED = "_"
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
    return f"v1/{resource}/{api_version}/{kind}/{subject}/{scope}/{canonical_query(query)}"


def manifest_key(scope: str) -> str:
    """Key of the manifest document for one caller scope."""
    return f"v1/_manifest/{scope}"


def resource_prefix(resource: str) -> str:
    """Prefix of everything cached for ``resource``, in every API version."""
    return f"v1/{resource}/"


def kind_prefix(resource: str, api_version: str, kind: str) -> str:
    """Prefix of all entries of one kind of one API version (every scope and subject)."""
    return f"v1/{resource}/{api_version}/{kind}/"


def record_prefix(resource: str, api_version: str, record_id: int) -> str:
    """Prefix of the ``record`` entries of one record of one API version."""
    return f"v1/{resource}/{api_version}/record/{record_id}/"
