from pathlib import Path

import pytest
import yaml

from core.catalog import (
    CLASSES,
    Catalog,
    ModelEntry,
    Price,
    entries_for_class,
    entry_by_alias,
    entry_by_id,
    load_catalog,
    parse_catalog,
)

PROVIDERS = Path(__file__).resolve().parents[2] / "providers"


def _model(id_, aliases=(), classes=("extract",), price=None, effort=("low",)):
    return {
        "id": id_,
        "aliases": list(aliases),
        "provider": "anthropic",
        "price": price or {"input": 1, "output": 5, "cache_write": 1.25, "cache_read": 0.1},
        "effort": list(effort),
        "classes": list(classes),
        "context_window": 200000,
    }


def _catalog(*models):
    return {"models": list(models)}


def test_a_valid_catalog_parses_to_frozen_records():
    got = parse_catalog(_catalog(_model("m-a", aliases=("a",), classes=("extract", "reason"))))
    assert got == Catalog(
        entries=(
            ModelEntry(
                id="m-a",
                aliases=("a",),
                provider="anthropic",
                price=Price(1.0, 5.0, 1.25, 0.1),
                effort=("low",),
                classes=("extract", "reason"),
                context_window=200000,
            ),
        )
    )


def test_a_null_context_window_means_unknown():
    raw = _catalog({**_model("m-a"), "context_window": None})
    assert parse_catalog(raw).entries[0].context_window is None


def test_duplicate_ids_fail():
    got = parse_catalog(_catalog(_model("m-a"), _model("m-a", aliases=("b",))))
    assert got == ["duplicate id 'm-a'"]


def test_duplicate_aliases_fail():
    got = parse_catalog(_catalog(_model("m-a", aliases=("x",)), _model("m-b", aliases=("x",))))
    assert got == ["duplicate alias 'x'"]


def test_an_alias_equal_to_another_id_fails():
    got = parse_catalog(_catalog(_model("m-a"), _model("m-b", aliases=("m-a",))))
    assert got == ["alias 'm-a' collides with a model id"]


def test_unknown_capability_class_fails():
    got = parse_catalog(_catalog(_model("m-a", classes=("extract", "vibes"))))
    assert got == ["models[0] (m-a): unknown capability class 'vibes'"]


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_price_fails(bad):
    price = {"input": bad, "output": 5, "cache_write": 1, "cache_read": 1}
    got = parse_catalog(_catalog(_model("m-a", price=price)))
    assert got == ["models[0] (m-a): price.input must be a positive number"]


def test_empty_effort_list_fails():
    got = parse_catalog(_catalog(_model("m-a", effort=())))
    assert got == ["models[0] (m-a): effort list is empty"]


def test_every_error_is_reported_at_once():
    got = parse_catalog(_catalog(_model("m-a", classes=("vibes",), effort=()), _model("m-a")))
    assert len(got) == 3


def test_a_non_mapping_or_empty_catalog_fails_as_a_value():
    assert parse_catalog(None) == ["catalog must have a non-empty 'models' list"]
    assert parse_catalog({"models": []}) == ["catalog must have a non-empty 'models' list"]


def _three():
    return parse_catalog(
        _catalog(
            _model("m-mid", aliases=("mid",), classes=("extract", "reason"),
                   price={"input": 2, "output": 10, "cache_write": 1, "cache_read": 1}),
            _model("m-low", aliases=("low",), classes=("extract",)),
            _model("m-top", classes=("frontier",),
                   price={"input": 10, "output": 50, "cache_write": 1, "cache_read": 1}),
        )
    )


def test_entries_for_class_orders_cheapest_first():
    assert [e.id for e in entries_for_class(_three(), "extract")] == ["m-low", "m-mid"]
    assert entries_for_class(_three(), "judge") == ()


def test_entry_by_id_and_alias_hit_and_miss():
    cat = _three()
    assert entry_by_id(cat, "m-mid").id == "m-mid"
    assert entry_by_id(cat, "mid") is None
    assert entry_by_alias(cat, "mid").id == "m-mid"
    assert entry_by_alias(cat, "m-mid") is None


def test_load_catalog_reads_the_shipped_catalog():
    got = load_catalog(PROVIDERS / "catalog.yaml")
    assert isinstance(got, Catalog)
    assert [e.id for e in got.entries] == [
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    ]
    assert [e.id for e in entries_for_class(got, "extract")] == [
        "claude-haiku-4-5-20251001",
        "claude-sonnet-5",
    ]


def test_load_catalog_returns_errors_for_a_missing_file(tmp_path):
    got = load_catalog(tmp_path / "nope.yaml")
    assert isinstance(got, list) and "cannot read" in got[0]


# local-oss.yaml is excluded: its tier values (local/qwen2.5-7b-instruct,
# anthropic/claude-standard, anthropic/claude-deep) name an OpenAI-compatible
# endpoint, not catalog models.
_NOT_CATALOG_MODELS = {"local-oss.yaml", "catalog.yaml"}
_PROFILES = sorted(p for p in PROVIDERS.glob("*.yaml") if p.name not in _NOT_CATALOG_MODELS)


@pytest.mark.parametrize("profile", _PROFILES, ids=lambda p: p.name)
def test_every_model_a_profile_names_is_a_catalog_id_or_alias(profile):
    cat = load_catalog(PROVIDERS / "catalog.yaml")
    tiers = yaml.safe_load(profile.read_text(encoding="utf-8"))["tiers"]
    unknown = {
        tier: model
        for tier, model in tiers.items()
        if entry_by_id(cat, model) is None and entry_by_alias(cat, model) is None
    }
    assert unknown == {}


def _resolve(cat, name):
    return entry_by_id(cat, name) or entry_by_alias(cat, name)


@pytest.mark.parametrize("profile", _PROFILES, ids=lambda p: p.name)
def test_every_profile_class_list_is_non_empty_and_names_models_carrying_that_class(profile):
    cat = load_catalog(PROVIDERS / "catalog.yaml")
    classes = yaml.safe_load(profile.read_text(encoding="utf-8"))["classes"]
    assert set(classes) <= CLASSES
    assert {k for k, v in classes.items() if not v} == set()
    unresolved = {(k, m) for k, v in classes.items() for m in v if _resolve(cat, m) is None}
    assert unresolved == set()
    lacking = {
        (k, m) for k, v in classes.items() for m in v if k not in _resolve(cat, m).classes
    }
    assert lacking == set()


@pytest.mark.parametrize("profile", _PROFILES, ids=lambda p: p.name)
def test_every_profile_defines_all_four_classes(profile):
    classes = yaml.safe_load(profile.read_text(encoding="utf-8"))["classes"]
    assert set(classes) == CLASSES


def test_the_profile_glob_found_the_profiles_it_is_meant_to_check():
    assert [p.name for p in _PROFILES] == ["anthropic-default.yaml", "claude-code.yaml"]
