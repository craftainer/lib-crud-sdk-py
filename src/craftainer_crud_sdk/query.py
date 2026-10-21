"""Query-string builder with `crud_query` semantics, validated against the manifest fields."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Protocol
from urllib.parse import quote

from craftainer_crud_sdk.errors import InvalidQueryError

__all__ = ["FieldLike", "build_query", "encode_params", "format_scalar"]

_MAX_REGEX_LENGTH = 200
_UNRESERVED_SAFE = "-._~"


class FieldLike(Protocol):
    """The part of a manifest field the builder needs (`manifest.FieldSpec` satisfies it)."""

    @property
    def name(self) -> str:
        """Property name; also the query key."""
        ...

    @property
    def value_type(self) -> str:
        """`integer`, `number`, `date`, `datetime`, `string`, `boolean` or `enum`."""
        ...

    @property
    def filter_ops(self) -> Sequence[str]:
        """Operators the server accepts."""
        ...

    @property
    def sortable(self) -> bool:
        """Whether `sort=` accepts the field."""
        ...

    @property
    def choices(self) -> Sequence[str] | None:
        """Allowed values of an enum field."""
        ...


def _quote(text: str) -> str:
    return quote(text, safe=_UNRESERVED_SAFE)


def encode_params(pairs: Sequence[tuple[str, str]]) -> str:
    """Join already-stringified pairs: everything but RFC 3986 unreserved is percent-encoded."""
    return "&".join(f"{_quote(k)}={_quote(v)}" for k, v in pairs)


def _format_datetime(value: datetime) -> str:
    moment = value.astimezone(UTC) if value.tzinfo is not None else value
    text = (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
    )
    millis = f"{moment.microsecond // 1000:03d}".rstrip("0")
    return f"{text}.{millis}Z" if millis else f"{text}Z"


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def format_scalar(field: FieldLike, value: object) -> str:
    """Wire form of one value for `field`; raises `InvalidQueryError` on a type mismatch."""
    kind = field.value_type
    text: str | None = None
    if kind == "integer":
        text = str(value) if isinstance(value, int) and not isinstance(value, bool) else None
    elif kind == "number":
        ok = _is_number(value) and math.isfinite(float(value))  # type: ignore[arg-type]
        text = str(value) if ok else None
    elif kind == "boolean":
        text = ("true" if value else "false") if isinstance(value, bool) else None
    elif kind == "date":
        text = (
            value.isoformat()
            if isinstance(value, date) and not isinstance(value, datetime)
            else None
        )
    elif kind == "datetime":
        text = _format_datetime(value) if isinstance(value, datetime) else None
    elif isinstance(value, str):
        text = value
    if text is None:
        raise InvalidQueryError(f"field {field.name!r} ({kind}) cannot take {value!r}")
    if kind == "enum" and field.choices is not None and text not in field.choices:
        raise InvalidQueryError(
            f"field {field.name!r} accepts only {list(field.choices)}, not {text!r}"
        )
    return text


def _filter_pair(field: FieldLike, op: str, value: object) -> tuple[str, str]:
    key = field.name if op == "eq" else f"{field.name}__{op}"
    if op == "in":
        if not isinstance(value, list | tuple) or not value:
            raise InvalidQueryError(f"{key!r} takes a non-empty list or tuple")
        parts = [format_scalar(field, item) for item in value]
        if any("," in part for part in parts):
            raise InvalidQueryError(f"{key!r}: a value contains a comma, which separates the list")
        return key, ",".join(_quote(part) for part in parts)
    text = format_scalar(field, value)
    if op == "regex" and len(text) > _MAX_REGEX_LENGTH:
        raise InvalidQueryError(f"{key!r}: a regex is limited to {_MAX_REGEX_LENGTH} characters")
    return key, _quote(text)


def _filter_pairs(
    fields: Mapping[str, FieldLike], filters: Mapping[str, object]
) -> list[tuple[str, str]]:
    parsed: dict[tuple[str, str], tuple[str, str]] = {}
    for raw_key, value in filters.items():
        name, sep, suffix = raw_key.rpartition("__")
        if not sep:
            name, suffix = raw_key, "eq"
        field = fields.get(name)
        if field is None:
            raise InvalidQueryError(f"unknown filter field {name!r}")
        if suffix not in field.filter_ops:
            raise InvalidQueryError(f"field {name!r} does not support the {suffix!r} operator")
        if name == "id" and suffix == "eq":
            raise InvalidQueryError("filter on id with record_id, not an equality filter")
        if (name, suffix) in parsed:
            raise InvalidQueryError(f"filter {name!r} {suffix!r} given twice")
        parsed[name, suffix] = _filter_pair(field, suffix, value)
    return [parsed[k] for k in sorted(parsed)]


def _sort_value(fields: Mapping[str, FieldLike], sort: str | Sequence[str]) -> str:
    parts = sort.split(",") if isinstance(sort, str) else list(sort)
    out: list[str] = []
    for part in parts:
        name = part.removeprefix("-")
        field = fields.get(name)
        if field is None or not field.sortable:
            raise InvalidQueryError(f"cannot sort by {name!r}")
        out.append(_quote(part))
    if not out:
        raise InvalidQueryError("sort is empty")
    return ",".join(out)


def _int_arg(name: str, value: int, minimum: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidQueryError(f"{name} must be an integer >= {minimum}")
    return str(value)


def build_query(  # noqa: PLR0913 - one keyword per documented query parameter
    fields: Sequence[FieldLike],
    *,
    record_id: int | None = None,
    filters: Mapping[str, object] | None = None,
    sort: str | Sequence[str] | None = None,
    skip: int | None = None,
    limit: int | None = None,
    max_limit: int | None = None,
    skip_param: str = "skip",
    limit_param: str = "limit",
    sort_param: str = "sort",
    include_archived: bool | None = None,
    include_unpublished: bool | None = None,
) -> str:
    """The query string (no leading `?`) for a list, get or bulk call.

    `filters` keys are `field` (equality, emitted bare) or `field__op`. Order of parameters:
    `id`, filters sorted by (field, operator), sort, skip, limit, `include_archived`,
    `include_unpublished`. The sort/skip/limit names are `sort_param`/`skip_param`/`limit_param`
    (the manifest's `pagination`); the `id` parameter is always `id`. Booleans are emitted
    whenever they are not `None`.
    """
    by_name = {f.name: f for f in fields}
    parts: list[str] = []
    if record_id is not None:
        parts.append(f"id={_int_arg('record_id', record_id, 0)}")
    # Filter values arrive already percent-encoded (the `in` list keeps its literal commas).
    parts.extend(f"{_quote(key)}={value}" for key, value in _filter_pairs(by_name, filters or {}))
    if sort is not None:
        parts.append(f"{_quote(sort_param)}={_sort_value(by_name, sort)}")
    if skip is not None:
        parts.append(f"{_quote(skip_param)}={_int_arg('skip', skip, 0)}")
    if limit is not None:
        text = _int_arg("limit", limit, 1)
        if max_limit is not None and limit > max_limit:
            raise InvalidQueryError(f"limit {limit} exceeds the server maximum {max_limit}")
        parts.append(f"{_quote(limit_param)}={text}")
    if include_archived is not None:
        parts.append(f"include_archived={'true' if include_archived else 'false'}")
    if include_unpublished is not None:
        parts.append(f"include_unpublished={'true' if include_unpublished else 'false'}")
    return "&".join(parts)
