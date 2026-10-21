"""Per-resource cache policy, defaulted from the manifest ``cache`` hints."""

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["CachePolicy"]


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """How one resource is cached. Times are seconds.

    ``max_age`` and ``stale_while_revalidate`` only take effect while the resource's SSE stream
    is running; without it every read revalidates. ``stats_ttl`` bounds staleness of
    ``stats``/``predict`` on its own, as the server caches those bodies for the same time.
    """

    enabled: bool = True
    max_age: float = 0.0
    stats_ttl: float = 0.0
    stale_while_revalidate: float = 0.0
    etag: bool = True
    revalidate_only: bool = False

    @classmethod
    def from_manifest(  # noqa: PLR0913  # one keyword override per policy field
        cls,
        resource: Mapping[str, object],
        *,
        enabled: bool = True,
        max_age: float | None = None,
        stats_ttl: float | None = None,
        stale_while_revalidate: float | None = None,
        revalidate_only: bool | None = None,
    ) -> CachePolicy:
        """Build a policy from a manifest resource dict (I1); keyword arguments override hints."""
        hints = resource.get("cache")
        hints = hints if isinstance(hints, Mapping) else {}
        return cls(
            enabled=enabled,
            max_age=max_age if max_age is not None else _seconds(hints.get("max_age")),
            stats_ttl=stats_ttl if stats_ttl is not None else _seconds(hints.get("stats_ttl")),
            stale_while_revalidate=(
                stale_while_revalidate
                if stale_while_revalidate is not None
                else _seconds(hints.get("stale_while_revalidate"))
            ),
            etag=hints.get("etag") is not False,
            revalidate_only=(
                revalidate_only
                if revalidate_only is not None
                else hints.get("revalidate_only") is True
            ),
        )


def _seconds(value: object) -> float:
    """A non-negative number of seconds from a hint; anything else (``null``) is 0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return max(float(value), 0.0)
