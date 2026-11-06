from __future__ import annotations

import dataclasses

import pytest

from harness.ticket_claims import Claim, check_claims, parse_claims

DEF = ("src/tools/agent_tools/land.py", 10, "def phase_land_deferred(task):")
CALL = ("src/tools/agent_tools/cli.py", 812, "    phase_land_deferred(task)")


def _search(*hits):
    return lambda word: list(hits)


@pytest.mark.parametrize(
    "body",
    [
        "There is no caller of `X`.",
        "X has no callers.",
        "Nothing calls X.",
        "No caller uses `X`.",
    ],
)
def test_no_caller_phrasings(body):
    assert parse_claims(body) == (Claim("no_caller", "X"),)


@pytest.mark.parametrize(
    ("body", "claim"),
    [
        ("Only `cli.py` uses `helper`.", Claim("only_user", "helper", ("cli.py",))),
        ("helper is used only by src/a/cli.py.", Claim("only_user", "helper", ("src/a/cli.py",))),
        ("Nothing imports pkg.mod.", Claim("no_import", "pkg.mod")),
        ("No module imports `Z`", Claim("no_import", "Z")),
        ("Z is imported by nothing.", Claim("no_import", "Z")),
    ],
)
def test_only_user_and_no_import_phrasings(body, claim):
    assert parse_claims(body) == (claim,)


def test_claims_come_back_in_body_order():
    body = "Z is imported by nothing. Later, nothing calls X, and only a.py uses Y."
    assert [c.subject for c in parse_claims(body)] == ["Z", "X", "Y"]


def test_body_without_a_claim_gives_nothing():
    assert parse_claims("Add a flag to the parser.") == ()
    assert check_claims({"body": "Add a flag."}, _search(CALL)) == ()
    assert check_claims({}, _search(CALL)) == ()


def test_claim_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Claim("no_caller", "X").subject = "Y"  # type: ignore[misc]


def test_false_no_caller_lists_hits_and_skips_the_definition():
    task = {"body": "No caller of phase_land_deferred remains."}
    assert check_claims(task, _search(DEF, CALL)) == (
        "claim 'no caller of phase_land_deferred' is false, 1 hit: src/tools/agent_tools/cli.py:812: phase_land_deferred(task)",
    )


def test_true_no_caller_gives_nothing():
    assert check_claims({"body": "no caller of phase_land_deferred"}, _search(DEF)) == ()


def test_listing_stops_at_five_hits():
    hits = [("a.py", n, "x()") for n in range(1, 8)]
    (text,) = check_claims({"body": "nothing calls x"}, _search(*hits))
    assert text.startswith("claim 'no caller of x' is false, 7 hits: a.py:1: x()")
    assert "a.py:5: x()" in text
    assert "a.py:6" not in text
    assert text.endswith("; and 2 more")


def test_false_only_user_names_the_stray_file():
    task = {"body": "Only cli.py uses helper."}
    hits = [
        ("src/a/impl.py", 3, "def helper():"),
        ("src/a/impl.py", 9, "helper()"),
        ("src/a/cli.py", 4, "helper()"),
        ("src/a/other.py", 7, "helper()"),
        ("src/a/xcli.py", 2, "helper()"),
    ]
    (text,) = check_claims(task, _search(*hits))
    assert text.startswith("claim 'only cli.py uses helper' is false, 2 hits: ")
    assert "src/a/other.py:7: helper()" in text
    assert "src/a/xcli.py:2: helper()" in text
    assert "cli.py:4" not in text
    assert "impl.py" not in text


def test_no_import_counts_only_import_lines():
    hits = [
        ("a.py", 1, "# mentions Z in a comment"),
        ("b.py", 2, "x = Z.run()"),
        ("c.py", 3, "from pkg import Z"),
    ]
    (text,) = check_claims({"body": "nothing imports Z"}, _search(*hits))
    assert text == "claim 'nothing imports Z' is false, 1 hit: c.py:3: from pkg import Z"
    assert check_claims({"body": "nothing imports Z"}, _search(*hits[:2])) == ()


def test_surface_hit_carries_the_marker():
    task = {"body": "Nothing calls f.", "surfaces": ["src/a/cli.py"]}
    hits = [("src/a/cli.py", 4, "f()"), ("src/a/other.py", 5, "f()")]
    (text,) = check_claims(task, _search(*hits))
    assert "src/a/cli.py:4: f() (a surface of this task);" in text
    assert text.endswith("src/a/other.py:5: f()")
