from __future__ import annotations

from typing import Any

from core.catalog import Catalog, parse_catalog
from core.model_freshness import freshness_warnings
from core.profile import Profile, resolve_profile


def _row(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "aliases": [],
        "provider": "anthropic",
        "price": {"input": 1, "output": 2, "cache_write": 1, "cache_read": 1},
        "effort": ["low"],
        "classes": ["reason"],
        "context_window": None,
    }


def _catalog(*ids: str) -> Catalog:
    cat = parse_catalog({"models": [_row(i) for i in ids]})
    assert isinstance(cat, Catalog)
    return cat


def _profile(catalog: Catalog, **tiers: str) -> Profile:
    prof = resolve_profile({"profile": "t", "tiers": tiers}, catalog)
    assert isinstance(prof, Profile)
    return prof


CAT = _catalog("claude-sonnet-4-5", "claude-sonnet-4-6", "claude-haiku-4-5")


def test_a_fresh_profile_has_no_warnings() -> None:
    prof = _profile(CAT, standard="sonnet", pinned="claude-sonnet-4-6", cheap="claude-haiku-4-5")
    assert freshness_warnings(prof, CAT) == []


def test_a_pinned_stale_tier_names_tier_family_current_and_newer_id() -> None:
    prof = _profile(CAT, standard="claude-sonnet-4-5")
    assert freshness_warnings(prof, CAT) == [
        "tier 'standard' (family sonnet): resolves to claude-sonnet-4-5, catalog has newer claude-sonnet-4-6"
    ]


def test_a_vendor_id_newer_than_the_catalog_warns_only_when_supplied() -> None:
    prof = _profile(CAT, standard="sonnet")
    assert freshness_warnings(prof, CAT, ["claude-sonnet-4-6", "claude-sonnet-4-7"]) == [
        "family sonnet: vendor lists claude-sonnet-4-7, newer than catalog claude-sonnet-4-6; catalog needs a row"
    ]
    assert freshness_warnings(prof, CAT) == []
