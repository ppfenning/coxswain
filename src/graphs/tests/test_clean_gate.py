import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from graphs.delivery.clean_gate import _MAX_HITS, _PLACEHOLDERS, clean_checks, clean_finding, clean_refusal
from harness.checks import check_feedback, repo_checks
from harness.epic_build import _gate_checks, _lane_checks

ROOT = Path(__file__).resolve().parents[3]
CLEAN = "python .github/check_clean.py . --require-names"
PYTEST = "cd src/graphs && pytest -q -n auto"
HOME = "/" + "home" + "/" + "someone"
# Made up per run, so no real machine name is ever written down: the test supplies the names rule its own pattern.
MACHINE = f"host{uuid4().hex[:10]}"


def test_clean_checks_picks_only_the_clean_entry() -> None:
    checks = [{"name": PYTEST, "cmd": PYTEST}, {"name": CLEAN, "cmd": CLEAN}]
    assert clean_checks(checks) == ({"name": CLEAN, "cmd": CLEAN},)


def test_clean_checks_without_a_clean_entry_is_empty() -> None:
    no_flag = "python .github/check_clean.py ."
    assert clean_checks([{"name": PYTEST, "cmd": PYTEST}, {"name": no_flag, "cmd": no_flag}]) == ()


def test_exit_zero_is_no_finding() -> None:
    assert clean_finding(0, "clean: no personal reference or vendor named") is None


def test_exit_one_quotes_the_hit_and_states_the_placeholder() -> None:
    hit = f"src/graphs/tests/test_x.py:12: home-path: '{HOME}'"
    finding = clean_finding(1, f"names rule skipped\n{hit}\n")
    assert finding is not None
    assert hit in finding
    assert "names rule skipped" not in finding
    assert "tmp_path" in finding and "`/home/user`" in finding


def test_exit_two_is_a_gate_that_did_not_run() -> None:
    assert clean_finding(2, "COXSWAIN_CLEAN_DENY is not set and --require-names was given") is None


def test_a_names_hit_names_the_machine_placeholders() -> None:
    refusal = clean_refusal(f"tests/test_x.py:3: names: '{MACHINE}'\n")
    assert refusal is not None
    assert f"tests/test_x.py:3: names: '{MACHINE}'" in refusal
    assert "`lane-host` or `lander-host`" in refusal


def test_output_with_no_hit_line_is_no_refusal() -> None:
    assert clean_refusal("clean: no personal reference or vendor named") is None


def test_check_feedback_adds_the_refusal_only_to_a_failed_clean_line() -> None:
    hit = f"tests/test_x.py:3: names: '{MACHINE}'"
    clean = {"name": CLEAN, "cmd": CLEAN, "passed": False, "exit_code": 1, "output_tail": hit}
    pytest_line = {"name": PYTEST, "cmd": PYTEST, "passed": False, "exit_code": 1, "output_tail": hit}
    assert "lander-host" in check_feedback([clean])
    assert "lander-host" not in check_feedback([pytest_line])


def test_an_unparsed_failure_says_no_hit_was_named_rather_than_replace_a_match() -> None:
    finding = clean_finding(1, "something went wrong")
    assert finding is not None
    assert "something went wrong" in finding
    assert "named no file and line" in finding
    assert "Replace each matched text" not in finding


def test_many_hits_give_a_bounded_refusal_and_no_repeat_of_the_tail() -> None:
    hits = "\n".join(f"tests/test_x.py:{n}: names: '{MACHINE}'" for n in range(1, 501))
    refusal = clean_refusal(hits)
    assert refusal is not None
    assert refusal.count(MACHINE) == _MAX_HITS
    assert f"+{500 - _MAX_HITS} more" in refusal
    assert len(refusal) < 3000
    feedback = check_feedback([{"name": CLEAN, "cmd": CLEAN, "passed": False, "exit_code": 1, "output_tail": hits}])
    assert feedback.count(MACHINE) == _MAX_HITS


def test_every_suggested_placeholder_passes_the_real_script(tmp_path: Path) -> None:
    literals = [p for _lead, placeholders in _PLACEHOLDERS.values() for p in placeholders]
    (tmp_path / "fixture.txt").write_text("\n".join(f"value = {p}/data" for p in literals) + "\n", encoding="utf-8")
    env = {**os.environ, "COXSWAIN_CLEAN_DENY": re.escape(MACHINE)}
    ran = subprocess.run(
        [sys.executable, str(ROOT / ".github" / "check_clean.py"), str(tmp_path), "--require-names"],
        env=env, capture_output=True, text=True,
    )
    assert (ran.returncode, ran.stdout.strip()) == (0, "clean: no personal reference or vendor named")


def _git(repo: Path, *args: str) -> None:
    """Edge. One git command in `repo`, with a neutral identity."""
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def test_the_lane_gate_runs_the_clean_line_and_hands_the_builder_the_placeholder(tmp_path: Path) -> None:
    """The repo's own `.agent-checks`, scoped and run by the lane gate against a real tree with the real script."""
    repo = tmp_path / "repo"
    (repo / ".github").mkdir(parents=True)
    shutil.copy(ROOT / ".github" / "check_clean.py", repo / ".github" / "check_clean.py")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_fetch.py").write_text(f'HOST = "ok"\nRUN_HOST = "{MACHINE}"\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a task that leaks a machine name")

    checks = repo_checks((ROOT / ".agent-checks").read_text(encoding="utf-8-sig"))
    scoped, _selection = _lane_checks(repo, "main", checks)
    clean = list(clean_checks(scoped))
    assert [c["cmd"] for c in clean] == [CLEAN]

    env = {**os.environ, "COXSWAIN_CLEAN_DENY": re.escape(MACHINE)}
    results = _gate_checks(repo, clean, env=env)
    assert [(r["passed"], r["exit_code"]) for r in results] == [(False, 1)]
    feedback = check_feedback(results)
    assert f"tests/test_fetch.py:2: names: '{MACHINE}'" in feedback
    assert "`lane-host` or `lander-host`" in feedback
