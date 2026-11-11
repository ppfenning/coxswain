"""The isolation guard's comparison is pure; one literal call per rule."""

from pathlib import Path

from conftest import GUARDED_MODULES, Snapshot, isolation_verdict

BEFORE = Snapshot(frozenset({(1, "MainThread")}), "/work", {"A": "1", "B": "2"})


def test_clean_run_has_no_findings():
    assert isolation_verdict(BEFORE, BEFORE) == ()


def test_leaked_thread_is_reported_by_name():
    after = BEFORE._replace(threads=BEFORE.threads | {(2, "lane-worker")})
    assert isolation_verdict(BEFORE, after) == ("thread 'lane-worker' is still alive",)


def test_thread_that_ended_is_not_a_finding():
    after = BEFORE._replace(threads=frozenset())
    assert isolation_verdict(BEFORE, after) == ()


def test_leaked_env_var_is_reported_by_key():
    after = BEFORE._replace(env={**BEFORE.env, "C": "3"})
    assert isolation_verdict(BEFORE, after) == ("env C changed from None to '3'",)


def test_removed_and_changed_env_vars_are_reported():
    after = BEFORE._replace(env={"B": "9"})
    assert isolation_verdict(BEFORE, after) == (
        "env A changed from '1' to None",
        "env B changed from '2' to '9'",
    )


def test_changed_cwd_is_reported():
    after = BEFORE._replace(cwd="/elsewhere")
    assert isolation_verdict(BEFORE, after) == ("cwd changed from '/work' to '/elsewhere'",)


def test_every_guarded_module_exists_beside_this_file():
    here = Path(__file__).parent
    assert [name for name in GUARDED_MODULES if not (here / f"{name}.py").is_file()] == []
    assert len(set(GUARDED_MODULES)) == len(GUARDED_MODULES) == 11
