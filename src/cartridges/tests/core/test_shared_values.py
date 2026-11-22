"""The small values moved into `core` so the harness and the tools share them without importing each other (F3)."""

from __future__ import annotations

import pytest

from core.frontmatter import split
from core.redaction import display, redact
from core.task_lease import parse_task_lease_name, task_lease_name


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("---\ntitle: a\n---\nbody", ({"title": "a"}, "body")),
        ("no block", ({}, "no block")),
        ("---\ntitle: a\nnever closed", ({}, "---\ntitle: a\nnever closed")),
        ("---\n- a list\n---\nbody", ({}, "body")),
        ("---\n: : bad\n---\nbody", ({}, "body")),
    ],
)
def test_split_takes_a_mapping_block_off_and_never_refuses(text, expected) -> None:
    assert split(text) == expected


def test_a_task_lease_name_reverses_and_other_lease_names_name_no_task() -> None:
    assert parse_task_lease_name(task_lease_name("i-1", "t_1")) == ("i-1", "t_1")
    assert [parse_task_lease_name(n) for n in ("land:t1", "land:task:i1/", "land:task:/t1", "repo:x")] == [None] * 4


def test_redact_replaces_the_token_and_its_repr_escaped_forms() -> None:
    assert redact("a tok\\x00en b tok\x00en", "tok\x00en") == "a *** b ***"
    assert redact("unchanged", "") == "unchanged"


def test_display_hides_the_userinfo_and_a_query_password() -> None:
    url = "postgresql://cox:hunter2@db:5432/store?sslpassword=s3&application_name=cox"
    assert display(url) == "postgresql://cox:***@db:5432/store?sslpassword=***&application_name=cox"
