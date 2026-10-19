from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.catalog import Catalog, is_family_alias, load_catalog, newest_in_family, parse_catalog
from core.profile import ResolvedModel, load_profile, resolve_entry, resolve_profile

PROVIDERS = Path(__file__).resolve().parent.parent / "providers"
SHIPPED = PROVIDERS / "anthropic-default.yaml"
CATALOG = PROVIDERS / "catalog.yaml"
FAMILIES = {"cheap": "haiku", "standard": "sonnet", "deep": "opus"}


def _row(model_id: str, aliases: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": model_id,
        "aliases": aliases or [],
        "provider": "anthropic",
        "price": {"input": 1, "output": 2, "cache_write": 1, "cache_read": 1},
        "effort": ["low"],
        "classes": ["reason"],
        "context_window": None,
    }


def _catalog(*rows: dict[str, Any]) -> Catalog:
    cat = parse_catalog({"models": list(rows)})
    assert isinstance(cat, Catalog)
    return cat


def test_an_alias_picks_the_newest_including_a_two_digit_minor() -> None:
    cat = _catalog(_row("claude-opus-5-9"), _row("claude-opus-5-10"), _row("claude-sonnet-5-5"))
    assert resolve_entry("opus", cat) == ResolvedModel("claude-opus-5-10", "opus")


def test_a_family_alias_ignores_a_catalog_alias_pinned_to_an_older_row() -> None:
    cat = _catalog(_row("claude-sonnet-5-5", ["sonnet"]), _row("claude-sonnet-5-10"))
    assert resolve_entry("sonnet", cat) == ResolvedModel("claude-sonnet-5-10", "sonnet")


def test_a_latest_alias_resolves_to_its_family() -> None:
    cat = _catalog(_row("claude-sonnet-5"), _row("claude-sonnet-5-5"))
    assert resolve_entry("sonnet-latest", cat) == ResolvedModel("claude-sonnet-5-5", "sonnet-latest")


def test_an_exact_id_resolves_exactly() -> None:
    cat = _catalog(_row("claude-opus-5-9"), _row("claude-opus-5-10"))
    assert resolve_entry("claude-opus-5-9", cat) == ResolvedModel("claude-opus-5-9", "claude-opus-5-9")


def test_a_pinned_catalog_alias_resolves_to_its_row_id() -> None:
    cat = _catalog(_row("claude-haiku-4-5-20251001", ["claude-haiku-4-5"]))
    assert resolve_entry("claude-haiku-4-5", cat) == ResolvedModel("claude-haiku-4-5-20251001", "claude-haiku-4-5")


def test_a_name_missing_from_the_catalog_passes_through_unchanged() -> None:
    cat = _catalog(_row("claude-sonnet-5-5"))
    assert resolve_entry("local/qwen2.5-7b-instruct", cat) == ResolvedModel("local/qwen2.5-7b-instruct", "local/qwen2.5-7b-instruct")


def test_an_alias_with_no_family_row_is_an_error_naming_alias_and_family() -> None:
    got = resolve_profile({"tiers": {"deep": "opus-latest"}}, _catalog(_row("claude-sonnet-5-5")))
    assert isinstance(got, list) and "'opus-latest'" in got[0] and "'opus'" in got[0]


def test_the_shipped_profile_requests_family_aliases_and_resolves_each_to_its_newest_row() -> None:
    catalog = load_catalog(CATALOG)
    got = load_profile(SHIPPED, CATALOG)
    assert not isinstance(catalog, list) and not isinstance(got, list)
    ids = [e.id for e in catalog.entries]
    assert {t: r.requested for t, r in got.tiers.items()} == FAMILIES
    assert {t: r.model for t, r in got.tiers.items()} == {t: newest_in_family(ids, fam) for t, fam in FAMILIES.items()}
    assert all(not is_family_alias(r.model) for r in got.tiers.values())


def test_the_shipped_profile_with_alias_tiers_picks_a_newer_row_without_a_yaml_edit(tmp_path: Path) -> None:
    raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.safe_dump({**raw, "tiers": FAMILIES}))
    cat_raw = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(yaml.safe_dump({**cat_raw, "models": [*cat_raw["models"], _row("claude-sonnet-5-10")]}))
    got = load_profile(profile, catalog)
    assert not isinstance(got, list)
    ids = [m["id"] for m in cat_raw["models"]]
    assert {t: r.requested for t, r in got.tiers.items()} == FAMILIES
    assert {t: r.model for t, r in got.tiers.items()} == {
        "cheap": newest_in_family(ids, "haiku"),
        "standard": "claude-sonnet-5-10",
        "deep": newest_in_family(ids, "opus"),
    }
    assert got.raw["classes"] == raw["classes"]
