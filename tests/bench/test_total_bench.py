"""Microbenchmark of the example API (pytest-benchmark / CodSpeed)."""

from pytest_benchmark.fixture import BenchmarkFixture

from template_python_package import total


def test_total_bench(benchmark: BenchmarkFixture) -> None:
    """Time total() over a fixed range."""
    values = list(range(1_000))
    assert benchmark(total, values) == sum(values)
