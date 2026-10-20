"""Tests for the example public API."""

import template_python_package
from template_python_package.core import add


def test_add() -> None:
    """add sums two ints."""
    assert add(2, 3) == 5


def test_total() -> None:
    """total sums an iterable and is 0 when empty."""
    assert template_python_package.total([1, 2, 3]) == 6
    assert template_python_package.total([]) == 0
