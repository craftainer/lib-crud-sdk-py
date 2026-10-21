"""Tests for the query builder: encoding table, validation and parameter order."""

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

from craftainer_crud_sdk import FieldSpec, InvalidQueryError, build_query
from craftainer_crud_sdk.query import encode_params, format_scalar

from ._support import resource

HEROES = resource("heroes", "v2").fields
QUESTS = resource("quests", "v1").fields


def _field(name: str, value_type: str, ops: tuple[str, ...], *, sortable: bool = True) -> FieldSpec:
    return FieldSpec(name, value_type, ops, sortable, None, False, False, False, False)


EXTRA = (
    _field("ratio", "number", ("eq", "min", "max", "in")),
    _field("tag", "string", ("eq", "in", "regex")),
    _field("hidden", "string", ("eq",), sortable=False),
    _field("id", "integer", ("eq", "in")),
    _field("future", "mystery", ("eq",)),
)
ALL = (*HEROES, *EXTRA)

PLUS2 = timezone(timedelta(hours=2))

TABLE: list[tuple[dict[str, Any], str]] = [
    ({}, ""),
    ({"filters": {"name": "Ada Lovelace"}}, "name=Ada%20Lovelace"),
    ({"filters": {"name__eq": "x"}}, "name=x"),
    ({"filters": {"name": "a+b&c=d/é"}}, "name=a%2Bb%26c%3Dd%2F%C3%A9"),
    ({"filters": {"name__icontains": "AZaz09-._~"}}, "name__icontains=AZaz09-._~"),
    ({"filters": {"name__regex": "^a.*$"}}, "name__regex=%5Ea.%2A%24"),
    ({"filters": {"power_level__in": [3, 1, 2]}}, "power_level__in=3,1,2"),
    ({"filters": {"is_draft__in": (True, False)}}, "is_draft__in=true,false"),
    ({"filters": {"tag__in": ["a b", "c/d"]}}, "tag__in=a%20b,c%2Fd"),
    ({"filters": {"is_draft": True}}, "is_draft=true"),
    ({"filters": {"is_locked": False}}, "is_locked=false"),
    ({"filters": {"power_level__min": -5}}, "power_level__min=-5"),
    ({"filters": {"ratio__max": 1.5}}, "ratio__max=1.5"),
    ({"filters": {"ratio": 2}}, "ratio=2"),
    ({"filters": {"future": "anything"}}, "future=anything"),
    (
        {"filters": {"created_at__min": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)}},
        "created_at__min=2026-01-02T03%3A04%3A05Z",
    ),
    (
        {"filters": {"created_at__min": datetime(2026, 1, 2, 3, 4, 5)}},
        "created_at__min=2026-01-02T03%3A04%3A05Z",
    ),
    (
        {"filters": {"created_at__max": datetime(2026, 1, 2, 3, 4, 5, tzinfo=PLUS2)}},
        "created_at__max=2026-01-02T01%3A04%3A05Z",
    ),
    (
        {"filters": {"created_at": datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)}},
        "created_at=2026-01-02T03%3A04%3A05.123Z",
    ),
    (
        {"filters": {"created_at": datetime(2026, 1, 2, 3, 4, 5, 500000, tzinfo=UTC)}},
        "created_at=2026-01-02T03%3A04%3A05.5Z",
    ),
    (
        {"filters": {"created_at": datetime(2026, 1, 2, 3, 4, 5, 999, tzinfo=UTC)}},
        "created_at=2026-01-02T03%3A04%3A05Z",
    ),
    (
        {"filters": {"created_at__in": [datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2)]}},
        "created_at__in=2026-01-01T00%3A00%3A00Z,2026-01-02T00%3A00%3A00Z",
    ),
    ({"record_id": 7}, "id=7"),
    ({"sort": "name,-power_level"}, "sort=name,-power_level"),
    ({"sort": ["-created_at", "name"]}, "sort=-created_at,name"),
    ({"skip": 0, "limit": 100}, "skip=0&limit=100"),
    (
        {"include_archived": True, "include_unpublished": False},
        "include_archived=true&include_unpublished=false",
    ),
    (
        {
            "include_unpublished": True,
            "include_archived": False,
            "limit": 10,
            "skip": 20,
            "sort": "name",
            "filters": {
                "power_level__min": 1,
                "name__icontains": "a",
                "power_level__max": 9,
                "name": "n",
                "created_at__min": datetime(2026, 1, 1, tzinfo=UTC),
            },
            "record_id": 3,
        },
        "id=3&created_at__min=2026-01-01T00%3A00%3A00Z&name=n&name__icontains=a"
        "&power_level__max=9&power_level__min=1&sort=name&skip=20&limit=10"
        "&include_archived=false&include_unpublished=true",
    ),
]


@pytest.mark.parametrize(("kwargs", "expected"), TABLE)
def test_encoding_table(kwargs: dict[str, Any], expected: str) -> None:
    """Test encoding table."""
    assert build_query(ALL, max_limit=1000, **kwargs) == expected


def test_quests_enum_and_date() -> None:
    """Test quests enum and date."""
    query = build_query(
        QUESTS,
        filters={"difficulty__in": ["easy", "hard"], "due_on__min": date(2026, 5, 1), "title": "t"},
    )
    assert query == "difficulty__in=easy,hard&due_on__min=2026-05-01&title=t"


ERRORS: list[tuple[dict[str, Any], str]] = [
    ({"filters": {"nope": 1}}, "unknown filter field"),
    ({"filters": {"nope__min": 1}}, "unknown filter field"),
    ({"filters": {"name__min": "a"}}, "does not support"),
    ({"filters": {"name__bogus": "a"}}, "does not support"),
    ({"filters": {"id": 1}}, "record_id"),
    ({"filters": {"name": "a", "name__eq": "b"}}, "twice"),
    ({"filters": {"power_level": "1"}}, "cannot take"),
    ({"filters": {"power_level": True}}, "cannot take"),
    ({"filters": {"power_level": 1.5}}, "cannot take"),
    ({"filters": {"ratio": float("nan")}}, "cannot take"),
    ({"filters": {"ratio": "1"}}, "cannot take"),
    ({"filters": {"ratio": True}}, "cannot take"),
    ({"filters": {"name": 5}}, "cannot take"),
    ({"filters": {"is_draft": "true"}}, "cannot take"),
    ({"filters": {"created_at": date(2026, 1, 1)}}, "cannot take"),
    ({"filters": {"created_at": "2026-01-01"}}, "cannot take"),
    ({"filters": {"power_level__in": 5}}, "non-empty list"),
    ({"filters": {"power_level__in": "1,2"}}, "non-empty list"),
    ({"filters": {"power_level__in": []}}, "non-empty list"),
    ({"filters": {"tag__in": ["a,b"]}}, "comma"),
    ({"filters": {"tag__regex": "x" * 201}}, "200 characters"),
    ({"filters": {"power_level__min": [1]}}, "cannot take"),
    ({"sort": "nope"}, "cannot sort"),
    ({"sort": "-hidden"}, "cannot sort"),
    ({"sort": []}, "sort is empty"),
    ({"sort": ""}, "cannot sort"),
    ({"skip": -1}, "skip"),
    ({"skip": True}, "skip"),
    ({"limit": 0}, "limit"),
    ({"limit": 1001}, "exceeds"),
    ({"record_id": -1}, "record_id"),
    ({"record_id": "1"}, "record_id"),
]


@pytest.mark.parametrize(("kwargs", "message"), ERRORS)
def test_invalid_queries_raise_before_sending(kwargs: dict[str, Any], message: str) -> None:
    """Test invalid queries raise before sending."""
    with pytest.raises(InvalidQueryError, match=message):
        build_query(ALL, max_limit=1000, **kwargs)


def test_enum_choice_is_checked() -> None:
    """Test enum choice is checked."""
    with pytest.raises(InvalidQueryError, match="accepts only"):
        build_query(QUESTS, filters={"difficulty": "impossible"})


def test_limit_without_server_maximum() -> None:
    """Test limit without server maximum."""
    assert build_query(ALL, limit=5000) == "limit=5000"


def test_encode_params_and_format_scalar() -> None:
    """Test encode params and format scalar."""
    assert encode_params([("a b", "c~d"), ("e", "f/g")]) == "a%20b=c~d&e=f%2Fg"
    assert format_scalar(_field("n", "integer", ("eq",)), 3) == "3"
