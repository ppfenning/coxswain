"""Lane database names on literals."""

from __future__ import annotations

import re

from harness.lane_database import database_name, lane_database_url, lane_env

TEMPLATE = "postgresql://u@h:5432/{lane}_test"


def test_database_name_is_last_segment() -> None:
    assert database_name("postgresql://u@h/db_lane_test") == "db_lane_test"


def test_two_tasks_of_one_run_differ() -> None:
    a = lane_database_url(TEMPLATE, "run1", "p1", "t1")
    b = lane_database_url(TEMPLATE, "run1", "p1", "t2")
    assert database_name(a) != database_name(b)


def test_same_inputs_give_same_name() -> None:
    assert lane_database_url(TEMPLATE, "run1", "p1", "t1") == lane_database_url(TEMPLATE, "run1", "p1", "t1")


def test_name_carries_task_and_suffix() -> None:
    name = database_name(lane_database_url(TEMPLATE, "run1", "p1", "t1"))
    assert re.fullmatch(r"t1_[0-9a-f]{8}_test", name)


def test_suffix_survives_very_long_task_id() -> None:
    url = lane_database_url(TEMPLATE, "run1", "p1", "x" * 200)
    name = database_name(url)
    assert name.endswith("_test")
    assert len(name) == 63
    assert url.startswith("postgresql://u@h:5432/")


def test_slug_is_safe_for_odd_characters() -> None:
    name = database_name(lane_database_url(TEMPLATE, "run/1", "p 1", "P1/Unit-3 é!"))
    assert re.fullmatch(r"[a-z0-9_]+", name)
    assert name.startswith("p1_unit_3_")


def test_none_setting_gives_empty_mapping() -> None:
    assert lane_env(None, "run1", "p1", "t1") == {}


def test_setting_gives_one_env_entry() -> None:
    env = lane_env({"env": "DATABASE_URL", "template": TEMPLATE}, "run1", "p1", "t1")
    assert env == {"DATABASE_URL": lane_database_url(TEMPLATE, "run1", "p1", "t1")}
