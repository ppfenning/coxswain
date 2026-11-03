"""What a land that runs as its own process leaves on disk, and the child entry point that leaves it.

The core is plain data in and out. The edge, `main`, runs the spec's argv and nothing else.
It imports nothing from the chair modules, so the child starts fast and no cycle forms.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

TIMEOUT_CODE = 124


@dataclass(frozen=True)
class LandSpec:
    key: str
    action: dict
    argv: list[str]
    repo: str
    timeout_s: float
    started_at: float  # epoch seconds
    host: str
    pid: int


@dataclass(frozen=True)
class LandOutcome:
    code: int
    output: str


def encode_spec(spec: LandSpec) -> str:
    return json.dumps(asdict(spec), sort_keys=True)


def encode_outcome(outcome: LandOutcome) -> str:
    return json.dumps(asdict(outcome), sort_keys=True)


def _is_num(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _object(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_spec(text: str) -> LandSpec | None:
    d = _object(text)
    if d is None:
        return None
    argv = d.get("argv")
    well_formed = (
        isinstance(d.get("key"), str)
        and isinstance(d.get("action"), dict)
        and isinstance(argv, list)
        and all(isinstance(a, str) for a in argv)
        and isinstance(d.get("repo"), str)
        and _is_num(d.get("timeout_s"))
        and _is_num(d.get("started_at"))
        and isinstance(d.get("host"), str)
        and _is_int(d.get("pid"))
    )
    if not well_formed:
        return None
    return LandSpec(d["key"], d["action"], argv, d["repo"], d["timeout_s"], d["started_at"], d["host"], d["pid"])


def parse_outcome(text: str) -> LandOutcome | None:
    d = _object(text)
    if d is None or not _is_int(d.get("code")) or not isinstance(d.get("output"), str):
        return None
    return LandOutcome(d["code"], d["output"])


def land_key(action: dict) -> str:
    """Readable slug of run plus task_id, else phase, then a hash of the raw identity so distinct actions never collide."""
    run, task, phase = (str(action.get(k, "")) for k in ("run", "task_id", "phase"))
    identity = (
        ["task", run, task]
        if task
        else ["phase", run, phase]
        if phase
        else ["action", json.dumps(action, sort_keys=True, default=str)]
    )
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", "--".join(p for p in (run, task or phase) if p))[:80].strip("-.")
    return f"{slug}--{digest}" if slug else f"land--{digest}"


def paths_for(state_dir: Path, key: str) -> tuple[Path, Path]:
    """(spec path, outcome path)."""
    return state_dir / f"{key}.spec.json", state_dir / f"{key}.outcome.json"


def dead_outcome(spec: LandSpec, pid: int) -> LandOutcome:
    return LandOutcome(-1, f"land process {pid} exited without an outcome")


def count_by_repo(specs: list[LandSpec]) -> dict[str, int]:
    return {repo: sum(1 for s in specs if s.repo == repo) for repo in {s.repo for s in specs}}


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()


def _run(spec: LandSpec) -> LandOutcome:
    """Never raises: any failure of the land is its outcome, so a finished land is never read back as dead.

    The land leads its own process group, so a timeout kills its grandchildren too. Output is decoded with
    replacement and keeps stderr interleaved with stdout, including what was printed before a timeout.
    """
    if not spec.argv:  # Popen([]) can succeed having run nothing, which would read back as a clean land
        return LandOutcome(127, "land could not start: empty argv")
    try:
        proc = subprocess.Popen(
            spec.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", start_new_session=True,
        )  # fmt: skip
    except Exception as e:  # an empty argv raises IndexError, a missing binary OSError
        return LandOutcome(127, f"land could not start: {type(e).__name__}: {e}")
    try:
        output, _ = proc.communicate(timeout=spec.timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        output, _ = proc.communicate()
        return LandOutcome(TIMEOUT_CODE, f"land timed out after {spec.timeout_s:g}s\n{output}")
    except Exception as e:
        _kill_group(proc)
        proc.wait()
        return LandOutcome(1, f"land failed while running: {type(e).__name__}: {e}")
    return LandOutcome(proc.returncode, output)


def main(spec_path: str) -> int:
    """Run the spec's argv and write the outcome file beside the spec. Returns 2 when the spec is unreadable."""
    path = Path(spec_path)
    try:
        spec = parse_spec(path.read_text())
    except OSError:
        spec = None
    if spec is None:
        return 2
    _, outcome_path = paths_for(path.parent, spec.key)
    _write_atomic(outcome_path, encode_outcome(_run(spec)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
