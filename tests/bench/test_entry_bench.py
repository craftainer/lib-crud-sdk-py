"""Microbenchmark of the I2 value types (pytest-benchmark / CodSpeed)."""

from pytest_benchmark.fixture import BenchmarkFixture

from craftainer_crud_sdk import CacheEntry


def test_cache_entry_bench(benchmark: BenchmarkFixture) -> None:
    """Time building a CacheEntry."""
    entry = benchmark(CacheEntry, 200, (("etag", '"a"'),), b"{}", 0.0, 1.0, 2.0)
    assert entry.status == 200
