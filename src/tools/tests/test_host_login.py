"""`cox host login` builds the store CLI's `host login --login-json` argv, with the keys the chair's login check writes.

What the store then does with it, and that `beat_at` stays, is proven against the real store by
smoke/test_contract_host_cli.py. The `FakeStore` that stood in for the store here took an argv the store CLI never
accepted (`--login-ok`, `--failures`, `--by`), so every real `cox host login` failed while these tests passed.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_tools import cli


def _login(argv: list[str]) -> dict:
    assert argv[:3] == ["host", "login", "host-a"] and argv[3] == "--login-json" and len(argv) == 5
    return json.loads(argv[4])


def test_a_failed_login_carries_ok_reason_and_the_failure_count_under_the_chairs_keys():
    assert _login(cli._host_login_argv("host-a", False, "expired", 3)) == {
        "login_ok": False, "reason": "expired", "login_failures": 3,
    }


def test_a_login_of_none_and_no_reason_are_null_so_the_store_clears_an_earlier_reason():
    assert _login(cli._host_login_argv("host-a", None, None, 0)) == {"login_ok": None, "reason": None, "login_failures": 0}


def test_the_argv_never_names_beat():
    assert "beat" not in cli._host_login_argv("host-a", True, None, 0)


@pytest.mark.parametrize("text, expected", [("true", True), ("False", False), ("none", None)])
def test_login_ok_reads_true_false_and_none(text, expected):
    assert cli._parse_login_ok(text) is expected
    ns = cli.build_parser().parse_args(["host", "login", "host-a", text])
    assert ns.login_ok is expected


def test_login_ok_rejects_anything_else():
    with pytest.raises(argparse.ArgumentTypeError):
        cli._parse_login_ok("maybe")
