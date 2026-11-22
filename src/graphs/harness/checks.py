"""Run the project's configured checks, in the worktree the harness owns.

`_change_facts` in `graphs/delivery/lifecycle_propose.py` counts a diff's shape
from the patch itself rather than asking the build node to report it, because a
node describing its own diff is describing a recollection. Checks extend the
same argument one step further: whether the tests pass is not something a
review node gets to assert either. This module runs the cartridge's configured
commands against the applied patch and turns their exit codes and output into
evidence rows — measured, never self-reported, and attached to the proposal
before the gate sees it. `repo_checks` parses a repository's own root
`.agent-checks` file into the same `{name, cmd}` shape `run_checks` expects.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphs.delivery.clean_gate import clean_checks, clean_refusal
from harness.check_scope import TOUCHED_ONLY
from harness.lane_workers import XDIST_VAR
from harness.worktree import linked_venv_bin, shared_venv_env

__all__ = [
    "HARNESS_FAULT_PREFIX",
    "LEVELS",
    "NEW_MODEL_ONLY",
    "TOUCHED_ONLY",
    "all_passed",
    "check_feedback",
    "check_outcome",
    "check_timeout",
    "checks_evidence",
    "collected_ids",
    "coverage_floor_holds",
    "failed_test_files",
    "failed_test_ids",
    "failures_outside",
    "fixable_checks",
    "flaky_note",
    "for_level",
    "is_harness_fault",
    "old_model_checks",
    "output_excerpt",
    "outside_surface_files",
    "quarantine_reason",
    "refix_route",
    "repo_checks",
    "run_checks",
    "serial_rerun_cmd",
]

HARNESS_FAULT_PREFIX = "harness fault:"
_OUTSIDE_SURFACES_PREFIX = "configured check failed outside surfaces: "
_FAILED_LINE_RE = re.compile(r"^FAILED (\S+?)::", re.MULTILINE)
_SUMMARY_HEADER = "short test summary info"

# Tokens like "12 passed", "2 failed", "1 error"/"errors", "3 skipped". Generic
# on purpose: it reads whatever a test runner prints rather than special-casing
# pytest, go test, jest, and every other framework's own vocabulary.
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped)\b", re.IGNORECASE)

_TAIL_CHARS = 2000
_REAP_SECONDS = 5  # how long a killed check's pipes get to drain before they are abandoned
_TAIL_LINES = 20
_EXCERPT_LINES = 40
_PYTEST_FAILURE_RE = re.compile(r"^(FAILED|ERROR) ", re.MULTILINE)
_PYTEST_NODE_RE = re.compile(r"^(FAILED|ERROR) \S+::", re.MULTILINE)
# A pytest progress line: dots and letters with an optional file name and percentage, or the percentage alone.
_PROGRESS_RE = re.compile(r"^(\S+\.py\s+)?[.FEsxXR]+\s*(\[\s*\d+%\])?$|^\[\s*\d+%\]$")
_TRUNCATION_MARKER = f"... [truncated to last {_TAIL_LINES} lines]"
_LINT_NAMES = frozenset({"lint", "ruff"})

# pytest's own exit code for "the command ran, but collected zero tests" —
# distinct from a real failure, which exits 1, and from a collection error,
# which exits 2 or 4.
_PYTEST_NO_TESTS_EXIT_CODE = 5
_SKIPPED_NO_PYTHON_TESTS = "skipped: no Python tests"
_TEST_FILE_GLOBS = ("test_*.py", "*_test.py")
_EXCLUDED_DIR_NAMES = frozenset({".venv", "target", "node_modules"})


def _is_pytest_cmd(cmd: str) -> bool:
    """Whether `cmd`'s own program is pytest, not merely a command that mentions it.

    Matches `pytest -q` and any absolute or relative path ending `/pytest`;
    a command that happens to print the word "pytest" does not count.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    return bool(tokens) and Path(tokens[0]).name == "pytest"


def _has_python_tests(worktree: Path) -> bool:
    """True iff `worktree` holds a `test_*.py` or `*_test.py` file, at any depth.

    A match under a `.venv`, `target`, or `node_modules` directory component
    does not count — those are vendored or built trees, not the repository's
    own tests.
    """
    for pattern in _TEST_FILE_GLOBS:
        for path in worktree.rglob(pattern):
            if not path.is_file():
                continue
            parent_parts = path.relative_to(worktree).parts[:-1]
            if _EXCLUDED_DIR_NAMES.isdisjoint(parent_parts):
                return True
    return False


def _parse_counts(output: str) -> dict[str, int]:
    """Mechanical extraction only. No match, no key — never invented."""
    counts: dict[str, int] = {}
    for number, word in _COUNT_RE.findall(output):
        key = "error" if word.lower() == "errors" else word.lower()
        counts[key] = counts.get(key, 0) + int(number)
    return counts


def tail_lines(text: str, n: int = _TAIL_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join([_TRUNCATION_MARKER, *lines[-n:]])


def _is_pytest_output(text: str) -> bool:
    """pytest's own summary header, or a FAILED/ERROR line naming a test node (`file::test`)."""
    return _SUMMARY_HEADER in text or bool(_PYTEST_NODE_RE.search(text))


def output_excerpt(output: str, n: int = _EXCERPT_LINES) -> str:
    """pytest output with failures: the short test summary, else the FAILED/ERROR lines. Else the last `n` lines."""
    lines = output.splitlines()
    if not (_is_pytest_output(output) and _PYTEST_FAILURE_RE.search(output)):
        return "\n".join(lines[-n:])
    start = next((i for i, ln in enumerate(lines) if _SUMMARY_HEADER in ln), None)
    kept = lines[start:] if start is not None else [ln for ln in lines if _PYTEST_FAILURE_RE.match(ln)]
    return "\n".join(kept[-n:])


def collected_ids(output: str) -> set[str]:
    """Node ids from `pytest --collect-only -q` stdout, pure.

    A node id line carries `::`; the trailing summary line ("N tests
    collected", "no tests ran") does not, so it is excluded by the same test.
    """
    return {line.strip() for line in output.splitlines() if "::" in line}


def coverage_floor_holds(before: set[str], after: set[str]) -> bool:
    """True iff every id collected `before` a trim is still collected `after`."""
    return before <= after


def check_outcome(returncode: int | None, error: str | None) -> str:
    if error is not None:
        return "unrunnable"
    return "passed" if returncode == 0 else "failed"


LEVELS: tuple[str, ...] = ("task", "phase", "initiative")
_LEVEL_MARK = re.compile(r"^#\s*level:\s*(\S+)\s*$")
# A trailing `# new model only` on a command line: the check runs at its level on the phase model alone, never in the
# old model's flat list (`old_model_checks`). The entry carries `NEW_MODEL_ONLY`; bash reads the mark as a comment.
_NEW_MODEL_MARK = re.compile(r"\s+#\s*new model only\s*$")
NEW_MODEL_ONLY = "new_model_only"
# A trailing `# touched only` on a task line `cd <dir> && pytest ...`: on the phase model the task gate runs it on the
# test files the task touched alone (`check_scope.select_checks`), and not at all when there are none, never the whole
# component: the whole suite is a wider level's. The entry carries `TOUCHED_ONLY` and `NEW_MODEL_ONLY` both, since the
# old model has no wider level to leave a suite to: its flat list takes the suite from the widest section instead.
_TOUCHED_MARK = re.compile(r"\s+#\s*touched only\s*$")
# A trailing `# timeout: <seconds>` on a command line: the check's own `timeout` (`check_timeout`), which `run_checks`
# and the touched-only scoping (`check_scope.TOUCHED_TIMEOUT_S`) honour over their defaults; a value that is not a
# positive whole number of seconds becomes a failing task check that names the line.
_TIMEOUT_MARK = re.compile(r"\s+#\s*timeout:\s*(\S*)\s*$")
# A `# fix: <command>` on a command line: the fix the build runs before the gate; it takes the rest of the line.
_FIX_MARK = re.compile(r"\s+#\s*fix:\s*(\S.*)$")
# The marks `_marks` peels off a line's end, in any order (and `# fix:`, which takes the rest of what is left).
_END_MARKS = ((NEW_MODEL_ONLY, _NEW_MODEL_MARK), (TOUCHED_ONLY, _TOUCHED_MARK), ("timeout", _TIMEOUT_MARK))
# A mark anywhere in a line: one still inside a command or a fix once the peel stops is a misplaced mark.
_ANY_MARK = re.compile(r"\s#\s*(?:new model only|touched only|timeout:|fix:)")


@dataclass(frozen=True)
class _Marks:
    """A `.agent-checks` command line with its trailing marks peeled (`_marks`): the command bash runs, the check's
    name (the command and its `# fix:`, as written), the fix command, each other mark found (its value, "" when it
    has none), and why the line cannot be read ("" when it can)."""

    cmd: str
    name: str
    fix: str
    found: dict[str, str]
    bad: str


def _marks(line: str) -> _Marks:
    """Pure. Peel `line`'s trailing marks in any order: `# new model only`, `# touched only` and `# timeout: N` off the
    end, and `# fix: <command>`, which runs to the end of what is left. A mark given twice, or a mark still inside the
    command or the fix once the peel stops, is a bad line, never a silent change to what the check does."""
    text, fix_span, found = line, "", dict[str, str]()
    while True:
        hit = next(((key, m) for key, mark in _END_MARKS if (m := mark.search(text))), None)
        if hit is None and not fix_span and (m := _FIX_MARK.search(text)):
            fix_span, text = text[m.start():], text[: m.start()].rstrip()
            continue
        if hit is None:
            break
        key, m = hit
        if key in found:
            return _Marks(text, text, "", found, f"the mark {m.group(0).strip()!r} is given twice")
        found[key], text = (m.group(1) if m.groups() else ""), text[: m.start()]
    fix = _FIX_MARK.search(fix_span)
    fix_cmd = fix.group(1).strip() if fix else ""
    if stray := _ANY_MARK.search(text) or _ANY_MARK.search(fix_cmd):
        return _Marks(text, text, fix_cmd, found, f"misplaced mark {stray.group(0).strip()!r}")
    return _Marks(text, text + fix_span, fix_cmd, found, "")


def repo_checks(text: str) -> list[dict[str, Any]]:
    """Parse a `.agent-checks` file: one shell command per line, pure.

    A line may end `# fix: <command>`: the check then carries that `fix`, which the build runs mechanically
    before the gate (a formatter, for example) instead of failing the task on a style-only difference.

    A line `# level: task`, `# level: phase` or `# level: initiative` starts a section: the commands after it, up to
    the next such line, run at that level (`for_level`). Commands before the first marker are task checks, so a file
    with no marker reads exactly as it always has. An entry of the phase or initiative level carries `level`; a task
    entry carries no such key. A marker naming any other level becomes one task check that fails and names the line,
    so a misspelt section is never silently dropped or run at the wrong level. A line may end `# new model only`
    (`NEW_MODEL_ONLY`): it runs at its level on the phase model, and the old model's flat list leaves it out. A task
    line may end `# touched only` (`TOUCHED_ONLY`, which implies `NEW_MODEL_ONLY`): the phase model's task gate runs it
    on the tests the task touched alone. At another level the mark only keeps the line out of the old model's list.
    A line may end `# timeout: <seconds>`: the entry carries that `timeout`, the line's own limit in place of the
    runner's default, and the mark is in neither its `cmd` nor its `name`. The trailing marks may come in any order
    (`_marks`). A timeout that is not a positive whole number, a mark given twice, or a mark left inside the command
    or the fix becomes one task check that fails and names the line.

    Blank lines and other lines starting with `#` are ignored. Each surviving line,
    stripped, becomes an entry whose `name` and `cmd` are the stripped line
    itself, so a failure names the whole command. A duplicate `cmd` within one level keeps only
    its first occurrence; order is otherwise preserved. Odd input (`None`, or
    anything that is not a string) yields `[]` rather than raising — a
    malformed file is a check that did not run, not a crash.
    """
    if not isinstance(text, str):
        return []
    entries: dict[tuple[str, str], dict[str, Any]] = {}  # first occurrence of a cmd per level wins, order preserved
    level = "task"
    for number, line in enumerate((line.strip() for line in text.splitlines()), 1):
        if mark := _LEVEL_MARK.match(line):
            level = mark.group(1)
            if level not in LEVELS:
                entries.setdefault(("task", f"#{number}"), _bad_level(number, level))
            continue
        if not line or line.startswith("#") or level not in LEVELS:
            continue
        peeled = _marks(line)
        limit = peeled.found.get("timeout")
        if peeled.bad:
            entries.setdefault(("task", f"#{number}"), _bad_line(number, peeled.bad))
            continue
        if limit is not None and not (re.fullmatch(r"[0-9]+", limit) and int(limit) > 0):
            entries.setdefault(("task", f"#{number}"), _bad_timeout(number, limit))
            continue
        only, touched = NEW_MODEL_ONLY in peeled.found, TOUCHED_ONLY in peeled.found
        tagged = {} if level == "task" else {"level": level}
        entries.setdefault((level, peeled.cmd), {"name": peeled.name, "cmd": peeled.cmd,
                                                 **({"fix": peeled.fix} if peeled.fix else {}), **tagged,
                                                 **({"timeout": int(limit)} if limit is not None else {}),
                                                 **({NEW_MODEL_ONLY: True} if only or touched else {}),
                                                 **({TOUCHED_ONLY: True} if touched else {})})
    return list(entries.values())


def _bad_level(number: int, level: str) -> dict[str, Any]:
    """The failing task check an unknown `# level:` marker becomes; its own lines are not run at any level."""
    said = f".agent-checks line {number}: unknown level {level!r}, expected one of {', '.join(LEVELS)}"
    return {"name": said, "cmd": f"echo {shlex.quote(said)} >&2; exit 2"}


def _bad_timeout(number: int, value: str) -> dict[str, Any]:
    """The failing task check a `# timeout:` mark with a bad value becomes; its own line is not run at any level."""
    return _bad_line(number, f"bad timeout {value!r}, expected a positive whole number of seconds")


def _bad_line(number: int, why: str) -> dict[str, Any]:
    """The failing task check a command line whose marks do not read becomes; the line is not run at any level."""
    said = f".agent-checks line {number}: {why}"
    return {"name": said, "cmd": f"echo {shlex.quote(said)} >&2; exit 2"}


def for_level(parsed: Sequence[Mapping[str, Any]], level: str) -> list[dict[str, Any]]:
    """Pure. The entries of a parsed `.agent-checks` (`repo_checks`) that run at `level`, in file order: an entry with
    no `level` key is a task check. Raises ValueError on a level outside `LEVELS`."""
    if level not in LEVELS:
        raise ValueError(f"unknown check level {level!r}; expected one of {LEVELS}")
    return [dict(c) for c in parsed if str(c.get("level") or "task") == level]


def old_model_checks(parsed: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pure. What a parsed `.agent-checks` asks of the old model, which knows one repository gate, the task's: the
    task lines, then the lines of the widest level the file names (initiative, else phase) whose command is not
    already listed. The levels nest, each a wider check than the one below, so the widest holds the narrower one's
    intent; running both would run a suite twice. A file with no marker gives its task lines, exactly as before
    levels. Each entry is a task entry: no `level` key. A line marked `# new model only` (`NEW_MODEL_ONLY`), at any
    level, is left out: the old model has its own gate for it (the root suite, CI's `unit (root)` job; the suite a
    `# touched only` task line narrows, which the widest section names whole), so its list stays as it was."""
    widest = [c for c in for_level(parsed, "initiative") or for_level(parsed, "phase") if not c.get(NEW_MODEL_ONLY)]
    task = [c for c in for_level(parsed, "task") if not c.get(NEW_MODEL_ONLY)]
    known = {c["cmd"] for c in task}
    return task + [{k: v for k, v in c.items() if k != "level"} for c in widest if c["cmd"] not in known]






def fixable_checks(checks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Checks carrying a `fix` command, reshaped to `{name, cmd}` for `run_checks`.

    A check without `fix` is not a candidate. The `fix` string runs verbatim —
    `--unsafe-fixes` or any other flag is never added on its behalf. A check's
    `timeout` is carried into its fix entry; no key is added when it has none.
    """
    return [
        {"name": c["name"], "cmd": c["fix"], **({"timeout": c["timeout"]} if "timeout" in c else {})}
        for c in checks
        if c.get("fix")
    ]


def check_timeout(check: Mapping[str, Any], default: int) -> int:
    """The check's own `timeout` in seconds, else `default`; a bool, non-int or non-positive value is refused."""
    if "timeout" not in check:
        return default
    value = check["timeout"]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"check entry {check.get('name')!r} has a bad 'timeout' (need a positive int): {value!r}")
    return value


def _text(value: str | bytes | None) -> str:
    """A partial stream as text: `TimeoutExpired` hands back bytes even when the run asked for text."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _cpu_seconds(usage: Any) -> float:
    """User plus system CPU seconds of a `wait4` rusage, rounded to milliseconds; 0.0 when none was gathered."""
    return 0.0 if usage is None else round(usage.ru_utime + usage.ru_stime, 3)


def _popen(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
    """Edge: a Popen that keeps its own child's rusage on `.rusage` when it reaps it.

    `communicate()` reaps the child and throws the usage away; this is the one place that
    keeps it. `wait4` on this pid counts the child and the descendants it reaped, and no
    other lane's checks, which a before and after `getrusage(RUSAGE_CHILDREN)` would.
    """

    class _Measured(subprocess.Popen):
        rusage: Any = None

        def _try_wait(self, wait_flags: int) -> tuple[int, int]:
            try:
                pid, sts, usage = os.wait4(self.pid, wait_flags)
            except ChildProcessError:
                return self.pid, 0
            if pid == self.pid:
                self.rusage = usage
            return pid, sts

    return _Measured(*args, **kwargs)


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the check's whole process group; a group that is already gone is fine."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def _kill_and_collect(proc: subprocess.Popen[str], first: subprocess.TimeoutExpired) -> str:
    """Kill a timed-out check's group and return the output it produced.

    The second `communicate()` returns everything read so far, so the output on
    `first` is only the fallback. It is bounded: a grandchild that left the
    group (`setsid`) survives the kill and would hold the pipes open forever.
    """
    _kill_group(proc)
    try:
        out, err = proc.communicate(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired as late:
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
        proc.wait()
        out, err = late.stdout, late.stderr
    collected = _text(out) + _text(err)
    return collected if collected else _text(first.stdout) + _text(first.stderr)


_FAILED_ID_RE = re.compile(r"^FAILED (\S+::.*?)(?: - .*)?$", re.MULTILINE)
_COMMAND_SEPARATOR_RE = re.compile(r"&&|\|\||;|\|")
_PYTEST_ERROR_RE = re.compile(r"^ERROR ", re.MULTILINE)


def failed_test_ids(output: str) -> list[str]:
    """Node ids from pytest `FAILED <id>` lines, in order and unique; `[]` when any `ERROR` line is present.

    An error (collection, fixture setup) is not a test a rerun by id can cover, so it forbids the rerun.
    """
    if _PYTEST_ERROR_RE.search(output):
        return []
    return list(dict.fromkeys(_FAILED_ID_RE.findall(output)))


def serial_rerun_cmd(cmd: str, ids: Sequence[str]) -> str | None:
    """`cmd` with its last pytest command cut back to `pytest -q -n 0 <ids>`, or None without ids or a pytest.

    Everything before that command (`cd src/tools &&`) is kept so the ids resolve from the same folder.
    Everything after pytest on it is dropped, so no test path or `-n auto` of the original is carried.
    `-n 0` runs in the one process, and keeps the `--dist` options a suite's ini may set legal.
    """
    separators = list(_COMMAND_SEPARATOR_RE.finditer(cmd))
    cut = separators[-1].end() if separators else 0
    try:
        tokens = shlex.split(cmd[cut:])
    except ValueError:
        return None
    program = next((i for i, t in enumerate(tokens) if Path(t).name == "pytest"), None)
    if program is None or not ids:
        return None
    return f"{cmd[:cut]} {shlex.join([*tokens[: program + 1], '-q', '-n', '0', *ids])}".strip()


def _serial_rerun_passes(
    cmd: str, ids: Sequence[str], worktree: Path, env: Mapping[str, str] | None, limit: int
) -> bool:
    """Edge: run the serial rerun once; a launch error or a timeout is a failed rerun, never a pass."""
    rerun = serial_rerun_cmd(cmd, ids)
    if rerun is None:
        return False
    try:
        proc = subprocess.Popen(
            rerun, shell=True, cwd=worktree, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
    except OSError:
        return False
    try:
        proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired as exc:
        _kill_and_collect(proc, exc)
        return False
    except BaseException:
        _kill_group(proc)
        proc.wait()
        raise
    return proc.returncode == 0


def flaky_note(ids: Sequence[str]) -> str:
    return f"flaky: {', '.join(ids)} passed on serial rerun"


def _write_log(log_dir: Path | None, prefix: str, number: int, text: str) -> dict[str, str]:
    """Edge: write check `number`'s whole output to `log_dir/<n>.log`; `{}` with no `log_dir`."""
    if log_dir is None:
        return {}
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{number}.log").write_text(text)
    return {"log": f"{prefix}{number}.log"}


def _workers_field(env: Mapping[str, str] | None) -> dict[str, str]:
    """The worker count the child env states, as a result field; empty when it states none."""
    return {"workers": env[XDIST_VAR]} if env is not None and XDIST_VAR in env else {}


def run_checks(
    worktree: Path,
    checks: Sequence[Mapping[str, Any]],
    *,
    timeout: int = 600,
    env: Mapping[str, str] | None = None,
    log_dir: Path | None = None,
    log_prefix: str = "",
) -> list[dict[str, Any]]:
    """Execute each configured check in `worktree` and report what actually happened.

    With `log_dir`, check n's whole output is written to `log_dir/<n>.log` and
    its result's `log` is `log_prefix + "<n>.log"`, the path from the run directory.

    Each entry needs `name` and `cmd`; an entry missing either is refused rather
    than silently skipped, because a check nobody ran is not a check, it is a
    gap wearing the shape of one. A command that never finishes is treated as a
    failure with no exit code — a check that hangs forever is not "still
    pending", it is a check that did not pass. `env` is the child's whole
    environment; None inherits this process's. In a worktree whose `.venv` is linked to the shared checkout's
    (`link_venv`), that venv's `bin` goes first on PATH and uv never syncs it (`shared_venv_env`): every check a lane
    runs, task, phase or suite, and every lander gate gets this one environment, whatever launched the process.
    """
    venv_bin = linked_venv_bin(worktree)
    if venv_bin is not None:
        env = shared_venv_env(os.environ if env is None else env, venv_bin)
    results: list[dict[str, Any]] = []
    workers = _workers_field(env)
    for number, check in enumerate(checks, 1):
        name = check.get("name")
        cmd = check.get("cmd")
        if not name or not cmd:
            raise ValueError(f"check entry missing 'name' or 'cmd': {dict(check)!r}")
        limit = check_timeout(check, timeout)

        try:
            # Own session so a timeout can kill the shell's grandchildren too;
            # a child left alive keeps the pipes open and communicate() hangs.
            proc = _popen(
                cmd,
                shell=True,
                cwd=worktree,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=limit)
            except subprocess.TimeoutExpired:
                raise
            except BaseException:
                # `subprocess.run` killed its child on any error; so must this.
                _kill_group(proc)
                proc.wait()
                raise
        except subprocess.TimeoutExpired as exc:
            partial = _kill_and_collect(proc, exc)
            results.append(
                {
                    "name": name,
                    "cmd": cmd,
                    "passed": False,
                    "outcome": "timed_out",
                    "error": f"{HARNESS_FAULT_PREFIX} check {name!r} timed out after {limit}s",
                    "exit_code": None,
                    "counts": _parse_counts(partial),
                    "output_tail": (partial + f"\n[timed out after {limit}s]")[-_TAIL_CHARS:],
                    "excerpt": "\n".join(filter(None, [output_excerpt(partial), f"[timed out after {limit}s]"])),
                    **_write_log(log_dir, log_prefix, number, partial + f"\n[timed out after {limit}s]\n"),
                    "cpu_seconds": _cpu_seconds(getattr(proc, "rusage", None)),
                    **workers,
                }
            )
            continue
        except (FileNotFoundError, OSError) as exc:
            error = str(exc)
            results.append(
                {
                    "name": name,
                    "cmd": cmd,
                    "passed": False,
                    "outcome": check_outcome(None, error),
                    "error": error,
                    "exit_code": None,
                    "counts": {},
                    "output_tail": "",
                    "excerpt": error,
                    **_write_log(log_dir, log_prefix, number, error + "\n"),
                    "cpu_seconds": 0.0,
                    **workers,
                }
            )
            continue

        combined = (stdout or "") + (stderr or "")
        not_found = proc.returncode == 127
        error = f"command not found: {cmd}" if not_found else None
        no_tests_ran = (
            error is None
            and proc.returncode == _PYTEST_NO_TESTS_EXIT_CODE
            and _is_pytest_cmd(cmd)
            and not _has_python_tests(worktree)
        )
        exit_code = proc.returncode
        flaky = failed_test_ids(combined) if exit_code == 1 and error is None else []
        if flaky and _serial_rerun_passes(cmd, flaky, worktree, env, limit):
            exit_code = 0  # the check counts as passed; `flaky` and `note` keep the first failure visible
        else:
            flaky = []
        passed = True if no_tests_ran else exit_code == 0
        outcome = "skipped" if no_tests_ran else check_outcome(exit_code, error)
        results.append(
            {
                "name": name,
                "cmd": cmd,
                "passed": passed,
                "outcome": outcome,
                "error": error,
                "exit_code": exit_code,
                **({"flaky": flaky, "note": flaky_note(flaky)} if flaky else {}),
                "counts": _parse_counts(combined),
                "output_tail": combined[-_TAIL_CHARS:],
                "excerpt": output_excerpt(combined),
                **_write_log(log_dir, log_prefix, number, combined),
                # The first run only: a flaky serial rerun is not added in.
                "cpu_seconds": _cpu_seconds(getattr(proc, "rusage", None)),
                **workers,
            }
        )
    return results


def checks_evidence(
    results: Sequence[Mapping[str, Any]], *, prefix: str = "checks", always_tail: bool = False
) -> list[dict[str, str]]:
    """Map check results to the `{check, output}` evidence-row shape.

    `prefix` names the row family. `always_tail` keeps the output of a passing
    result too, for a command whose output is the evidence itself.

    Verdict first, counts when parsed, exit code always — the same order a
    reader scans a CI summary in, and the same discipline as every other
    evidence row in this system: a claim without the numbers behind it is a
    guess with formatting.
    """
    rows: list[dict[str, str]] = []
    for result in results:
        workers_line = f"workers: {result['workers']}" if result.get("workers") else ""
        if result.get("outcome") == "skipped":
            # A skipped row keeps its worker count too: the variable was applied even though no test ran.
            skipped = "\n".join(filter(None, [_SKIPPED_NO_PYTHON_TESTS, workers_line]))
            rows.append({"check": f"{prefix}:{result['name']}", "output": skipped})
            continue
        counts = result.get("counts") or {}
        counted = ", ".join(f"{v} {k}" for k, v in counts.items())
        verdict = "pass" if result.get("passed") else "FAIL"
        detail = f"{counted} " if counted else ""
        summary = f"{verdict} — {detail}(exit {result.get('exit_code')})"
        captured = result.get("output_tail") or ""
        if result.get("outcome") == "unrunnable":
            error_text = result.get("error") or ""
            body = f"{error_text}\n{captured}" if captured else error_text
        else:
            body = captured
        tail = f"\ncmd: {result.get('cmd')}\n{tail_lines(body)}"
        carries_tail = always_tail or not result.get("passed")
        output = summary + tail if carries_tail else summary
        if result.get("reused"):
            output = f"{output}\n{result.get('excerpt') or 'reused: same tree and command passed earlier'}"
        if result.get("note"):
            output = f"{output}\n{result['note']}"
        if workers_line:
            output = f"{output}\n{workers_line}"
        rows.append({"check": f"{prefix}:{result['name']}", "output": output})
    return rows


def all_passed(results: Sequence[Mapping[str, Any]]) -> bool:
    return all(r.get("passed") for r in results)


def is_harness_fault(reason: str) -> bool:
    return reason.startswith(HARNESS_FAULT_PREFIX)


def _clean_refusal_of(result: Mapping[str, Any]) -> str | None:
    """The clean script's refusal, with the placeholder per hit, for a failed clean line; None for any other check."""
    return clean_refusal(str(result.get("output_tail") or "")) if clean_checks([result]) else None


def check_feedback(results: Sequence[Mapping[str, Any]]) -> str:
    """The failing checks' name, command, exit code and output tail, for a builder to read.

    A failed clean line carries its refusal in place of the tail: each hit's file:line and matched text, bounded, and
    the placeholder to use.
    """
    return "\n\n".join(
        f"{r.get('name')}: {r.get('cmd')}\nexit {r.get('exit_code')}\n"
        f"{_clean_refusal_of(r) or tail_lines(str(r.get('output_tail') or ''))}"
        for r in results
        if not r.get("passed")
    )


def _is_lint_command(name: str) -> bool:
    """Whether a check named by its whole line is lint: every `&&`/`;` step is a `cd` or a lint program.

    `cd src && ruff check .` is lint; `ruff check . && pytest -q` is not.
    """
    steps = [s.split(None, 1)[0] for s in re.split(r"&&|;", name) if s.strip()]
    programs = [s for s in steps if s != "cd"]
    return bool(programs) and all(p in _LINT_NAMES for p in programs)


def refix_route(
    results: Sequence[Mapping[str, Any]],
    checks: Sequence[Mapping[str, Any]],
    *,
    attempts_left: float,
    style_bound: bool,
) -> str:
    """Where an approved build whose checks failed goes next: quarantine, style_pass or revise.

    Only a real failure re-enters, and only while an attempt remains. A failure
    counts as style-only when every failing check has a `fix` command or is
    named lint or ruff, and the `style_pass` seat is bound.
    """
    reason = quarantine_reason(results)
    if reason is None or is_harness_fault(reason) or attempts_left <= 0:
        return "quarantine"
    lint = {c["name"] for c in checks if c.get("fix") or _is_lint_command(c["name"])}
    if style_bound and all(r["name"] in lint for r in results if not r.get("passed")):
        return "style_pass"
    return "revise"


def _norm(path: str) -> str:
    return path.removeprefix("./").rstrip("/")


def _covers(path: str, root: str) -> bool:
    """Path equality or directory prefix; `tests` covers `tests/a.py` but not `tests_extra/a.py`."""
    return root in ("", ".") or path == root or path.startswith(root + "/")


def failed_test_files(output: str) -> tuple[str, ...]:
    """Sorted unique file paths from pytest `FAILED <file>::<test>` lines."""
    return tuple(sorted({_norm(f) for f in _FAILED_LINE_RE.findall(output)}))


def failures_outside(output: str, surfaces: Sequence[str], patch_files: Sequence[str]) -> tuple[str, ...]:
    """The failing files when all lie outside both surfaces and patch files, else ().

    No roots means nothing to be outside of, and an absolute path cannot be placed: both give ().
    """
    files = failed_test_files(output)
    roots = tuple(_norm(r) for r in (*surfaces, *patch_files))
    if not roots or any(f.startswith("/") or any(_covers(f, r) for r in roots) for f in files):
        return ()
    return files


def outside_surface_files(reason: str) -> tuple[str, ...]:
    """The file list from a `configured check failed outside surfaces: ...` reason, else ()."""
    if not reason.startswith(_OUTSIDE_SURFACES_PREFIX):
        return ()
    return tuple(f for f in reason[len(_OUTSIDE_SURFACES_PREFIX):].split(", ") if f)


def _reason_detail(excerpt: str, exit_code: Any) -> str:
    """The one line a quarantine reason carries: the first pytest FAILED (else ERROR) line.

    Without one it is the last line that is not pytest progress, so dots never stand in for a cause.
    Output that is all progress reports the exit code instead.
    """
    lines = [ln for ln in excerpt.splitlines() if ln.strip()]
    if _is_pytest_output(excerpt):
        for kind in ("FAILED ", "ERROR "):
            hit = next((ln for ln in lines if ln.startswith(kind)), None)
            if hit:
                return hit
    meaningful = [ln for ln in lines if not _PROGRESS_RE.match(ln.strip())]
    if meaningful:
        return meaningful[-1]
    return f"exit {exit_code}" if lines and exit_code is not None else ""


def quarantine_reason(
    results: Sequence[Mapping[str, Any]],
    surfaces: Sequence[str] = (),
    patch_files: Sequence[str] = (),
) -> str | None:
    if all_passed(results):
        return None
    faults = ("unrunnable", "timed_out")
    real_failures = [r for r in results if not r.get("passed") and r.get("outcome") not in faults]
    if not real_failures:
        fault = next(r for r in results if r.get("outcome") in faults)
        if fault.get("outcome") == "timed_out":
            return str(fault.get("error"))
        return f"{HARNESS_FAULT_PREFIX} check '{fault['name']}' could not run: {fault.get('error')}"
    failing = [r for r in results if not r.get("passed")]
    tails = [str(r.get("output_tail") or "") for r in failing]
    # Each failing check's tail must hold pytest's whole summary block; otherwise a
    # failure it cannot see (a truncated FAILED line, a lint check) may be in the surfaces.
    complete = all(_SUMMARY_HEADER in t for t in tails)
    outside = failures_outside("\n".join(tails), surfaces, patch_files) if complete else ()
    if outside:
        return f"{_OUTSIDE_SURFACES_PREFIX}{', '.join(outside)}"
    names = ", ".join(r["name"] for r in failing)
    excerpt = str(failing[0].get("excerpt") or output_excerpt(str(failing[0].get("output_tail") or "")))
    summary = _reason_detail(excerpt, failing[0].get("exit_code"))
    detail = f" — {summary}" if summary else ""
    return f"configured check failed: {names}{detail}"
