from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.cartridge import apply_overlay, integration_checks_setting, load, overlay_errors
from core.cartridge import test_database_setting as database_setting  # alias: pytest would collect the `test_` name
from core.skills import index_from_roots

PACKAGE = Path(__file__).resolve().parent.parent
OVERLAY = Path(__file__).resolve().parents[3] / ".agent" / "cartridge.yaml"


def _overlay() -> dict:
    assert OVERLAY.is_file(), f"repository overlay missing at {OVERLAY}"
    return yaml.safe_load(OVERLAY.read_text())


def test_repo_overlay_has_no_errors() -> None:
    assert overlay_errors(_overlay()) == []


def test_repo_overlay_sets_land_checks_ci_and_leaves_land_at_to_the_team() -> None:
    # The land_at level stop-gap is gone: ticket granularity unblocks dependent phases on a stored merge.
    landing = _overlay()["landing_areas"]
    assert landing["land_checks"] == "ci"
    assert "land_at" not in landing


def test_local_team_without_overlay_leaves_land_checks_unset() -> None:
    resolved = load("local", PACKAGE / "cartridges", skill_index=index_from_roots([PACKAGE / "skills-plugins"]))
    assert "land_checks" not in resolved.get("landing_areas", {})


_VALID = {"env": "DATABASE_URL", "template": "postgresql://localhost/app_{lane}_test", "drop": "dropdb {name}"}


def _with_test_database(value: object) -> dict:
    return {"landing_areas": {"test_database": value}}


def test_valid_test_database_merges_next_to_target_branch() -> None:
    overlay = {"landing_areas": {"target_branch": "main", "test_database": _VALID}}
    assert overlay_errors(overlay) == []
    merged = apply_overlay({"landing_areas": {"land_at": "ticket"}}, overlay)
    assert merged["landing_areas"] == {"land_at": "ticket", "target_branch": "main", "test_database": _VALID}
    assert database_setting(merged) == _VALID


def test_test_database_without_drop_is_valid() -> None:
    assert overlay_errors(_with_test_database({"env": "DB_URL", "template": "sqlite:///{lane}.db"})) == []


def test_overlay_without_test_database_leaves_cartridge_unchanged() -> None:
    resolved = {"description": "x", "landing_areas": {"land_at": "ticket"}}
    overlay = {"landing_areas": {"land_checks": "ci"}}
    assert apply_overlay(resolved, overlay) == {**resolved, "landing_areas": {"land_at": "ticket", "land_checks": "ci"}}
    assert apply_overlay(resolved, {"description": "y"})["landing_areas"] == resolved["landing_areas"]
    assert database_setting(resolved) is None
    assert database_setting({}) is None


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://localhost/app_{lane}_test",
        {**_VALID, "extra": "x"},
        {"template": _VALID["template"]},
        {"env": "DATABASE_URL"},
        {**_VALID, "env": "1BAD"},
        {**_VALID, "env": "A-B"},
        {**_VALID, "env": 5},
        {**_VALID, "template": "postgresql://localhost/app_test"},
        {**_VALID, "template": "postgresql://localhost/{lane}_{lane}"},
        {**_VALID, "template": "postgresql://localhost/app_{lane}"},
        {**_VALID, "template": 7},
        {**_VALID, "drop": "dropdb app"},
        {**_VALID, "drop": ["dropdb", "{name}"]},
    ],
)
def test_invalid_test_database_is_refused_by_name(value: object) -> None:
    problems = overlay_errors(_with_test_database(value))
    assert problems
    assert all("landing_areas.test_database" in problem for problem in problems)


_CHECKS = [{"name": "api", "cmd": "pytest {packages}"}, {"name": "web", "cmd": "npm test"}]
_WITH_SCHEMA = {**_VALID, "schema": "db/schema.sql", "create": "createdb {name} < {schema}"}


def _landing(**keys: object) -> dict:
    return {"landing_areas": keys}


def test_integration_checks_schema_and_create_are_accepted_and_merged() -> None:
    overlay = _landing(integration_checks=_CHECKS, test_database=_WITH_SCHEMA)
    assert overlay_errors(overlay) == []
    merged = apply_overlay({"landing_areas": {"land_at": "ticket"}}, overlay)
    assert merged["landing_areas"] == {"land_at": "ticket", "integration_checks": _CHECKS, "test_database": _WITH_SCHEMA}
    assert integration_checks_setting(merged) == _CHECKS
    assert database_setting(merged) == _WITH_SCHEMA


def test_schema_without_create_is_valid() -> None:
    assert overlay_errors(_with_test_database({**_VALID, "schema": "db/schema.sql"})) == []


def test_dotted_file_name_is_not_a_dotdot_segment() -> None:
    assert overlay_errors(_with_test_database({**_WITH_SCHEMA, "schema": "db/..v2/schema.sql"})) == []


@pytest.mark.parametrize(
    ("overlay", "expected"),
    [
        (_landing(integration_checks=[]), "landing_areas.integration_checks: list must not be empty"),
        (_landing(integration_checks=[_CHECKS[0], _CHECKS[0]]), "integration_checks entry 'api': duplicate name"),
        (_landing(integration_checks=[{"name": "api"}]), "integration_checks entry"),
        (_landing(test_database={**_VALID, "create": "createdb {name}"}), "landing_areas.test_database: create requires schema"),
        (_landing(test_database={**_WITH_SCHEMA, "create": "createdb app"}), "landing_areas.test_database: create must"),
        (_landing(test_database={**_WITH_SCHEMA, "schema": "/db/schema.sql"}), "landing_areas.test_database: schema must"),
        (_landing(test_database={**_WITH_SCHEMA, "schema": "db/../schema.sql"}), "landing_areas.test_database: schema must"),
        (_landing(test_database={**_WITH_SCHEMA, "schema": ".."}), "landing_areas.test_database: schema must"),
        (_landing(test_database={**_WITH_SCHEMA, "schema": 3}), "landing_areas.test_database: schema must"),
    ],
)
def test_invalid_integration_settings_are_refused_by_name(overlay: dict, expected: str) -> None:
    problems = overlay_errors(overlay)
    assert len(problems) == 1
    assert expected in problems[0]
    assert "integration_checks" in problems[0] or "landing_areas.test_database" in problems[0]


def test_overlay_without_integration_keys_behaves_as_before() -> None:
    resolved = {"landing_areas": {"land_at": "ticket"}}
    merged = apply_overlay(resolved, _with_test_database(_VALID))
    assert merged["landing_areas"] == {"land_at": "ticket", "test_database": _VALID}
    assert "integration_checks" not in apply_overlay(resolved, _landing(land_checks="ci"))["landing_areas"]
    assert integration_checks_setting(resolved) == []
    assert integration_checks_setting({}) == []
