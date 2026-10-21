"""`enable_cache` builds a policy per (resource name, api_version)."""

from craftainer_crud_sdk.cache import CachePolicy
from craftainer_crud_sdk.wiring import _policies

from ._support import load


def test_a_policy_per_version_and_an_override_applies_to_every_version() -> None:
    """Each manifest entry gets its own policy; an override by name wins in all versions."""
    manifest = load()
    versions = sorted(r.api_version for r in manifest.resources if r.name == "heroes")
    assert len(versions) > 1
    selected = [r for r in manifest.resources if r.name == "heroes"][:1]
    plain = _policies(manifest, selected, None)
    assert sorted(plain) == [("heroes", v) for v in versions]
    override = CachePolicy(max_age=5)
    assert set(_policies(manifest, selected, {"heroes": override}).values()) == {override}
