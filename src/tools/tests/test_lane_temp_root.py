"""The suite passes under a lane's temp root: one named `coxswain-lane-...`, and long.

An epic lane runs this suite with TMPDIR at a scratch directory of its own. Its name carries `coxswain-`, and on macOS
it sits under `/private/var/folders/.../T/`, so pytest's temp paths there run past 200 characters. Tests that matched
`coxswain-` in output naming temp paths, made a folder named for a whole temp path, or matched the tail of an error
cut at 200 characters failed in every lane and passed by hand. Each test below stood for one of those."""

import os
import subprocess
import sys
from pathlib import Path

SUITE = Path(__file__).resolve().parents[1]
REPRESENTATIVES = (
    "tests/test_chair_resolve.py::test_bare_cox_resumes_a_crashed_lease_in_its_cwd_and_the_session_line_says_resumed",
    "tests/test_chair_activity_cursor.py::test_first_call_equals_uncached",
    "tests/test_dash_feed_operator_sessions.py::test_both_sessions_appear_in_the_windows_their_lines_fall_in",
    "tests/test_dash_feed_idle.py::test_a_cached_activity_equals_an_uncached_one",
    "tests/test_land_manual.py::test_an_unreachable_origin_names_the_fetch_error",
    "tests/test_setup_migrate_cli.py::test_apply_makes_the_umbrella_the_monorepo_and_retires_the_components",
)


def lane_temp_root(base: Path, length: int) -> Path:
    """A lane-shaped TMPDIR under `base`, at least `length` characters long whatever `base` is."""
    root = base / "coxswain-lane-epic-with-a-long-run-name-0123456789-abcdef012345" / "pytest-tmp"
    pad = max(length - len(str(root)) - len("/p1--"), 1)
    return root / f"p1--{('a-task-with-a-long-name-' * 20)[:pad]}"


def test_representative_tests_pass_under_a_long_coxswain_lane_temp_root(tmp_path):
    scratch = lane_temp_root(tmp_path, 220)
    scratch.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COV_"))}
    env["TMPDIR"] = str(scratch)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", *REPRESENTATIVES],
        cwd=SUITE, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert f"{len(REPRESENTATIVES)} passed" in proc.stdout
