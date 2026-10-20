"""Public surface of the package; builds on ``core``."""

from collections.abc import Iterable

from template_python_package.core import add


def total(values: Iterable[int]) -> int:
    """Return the sum of ``values`` (0 when empty)."""
    result = 0
    for value in values:
        result = add(result, value)
    return result
