"""Chair reader for `land_conflicts`: the squash conflicts that retrying the land would only repeat.

Source: `land.jsonl`, one row per land. A `squash_phase` stop records `conflict_paths`, `branch_sha` and `from_sha`
(`land.stop_fields`). The row has no initiative or phase key: the initiative is the run id without its `-N` suffix,
and the phase is named by the branch in `stop_detail`, `squash of epic/<initiative>/<phase> conflicts in: ...`.
"""

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agent_tools import repo_path
from agent_tools.chair_land_conflict import LandConflict
from agent_tools.jsonl_log import parse_log

Row = Mapping[str, object]
Key = tuple[str, str, str]  # (initiative, phase, run)
Commits = tuple[str, str]  # (phase branch tip, main tip)

MAIN = "main"


def squash_key(row: Row) -> Key | None:
    """(initiative, phase, run) of a `squash_phase` row; None for any other row or a detail that names no phase branch."""
    run = str(row.get("run") or "")
    initiative = re.sub(r"-\d+$", "", run)
    named = re.fullmatch(rf"squash of epic/{re.escape(initiative)}/(?P<phase>.+?) conflicts in: .*", str(row.get("stop_detail") or ""), re.DOTALL)
    if row.get("stopped_step") != "squash_phase" or not initiative or named is None:
        return None
    return initiative, named["phase"], run


def land_conflicts(rows: Sequence[Row], current: Mapping[tuple[str, str], Commits]) -> list[LandConflict]:
    """The newest `squash_phase` row per run and phase, when its recorded commits still equal `current`.

    `current` maps (initiative, phase) to the phase branch tip and main tip now. A row with no recorded commit id,
    or a phase absent from `current`, is never kept, so that land can be retried."""
    newest = {key: row for row in sorted(rows, key=lambda r: str(r.get("ts") or "")) if (key := squash_key(row)) is not None}
    return [
        {"initiative": key[0], "phase": key[1], "run": key[2], "paths": [str(p) for p in row.get("conflict_paths") or []]}
        for key, row in newest.items()
        if row.get("branch_sha") and row.get("from_sha")
        and (str(row["branch_sha"]), str(row["from_sha"])) == current.get((key[0], key[1]))
    ]


def _rev_parse(repo: str, ref: str) -> str | None:
    argv = repo_path.git_argv(repo, os.path.expanduser("~"), "rev-parse", "--verify", "--quiet", ref)
    r = subprocess.run(argv, capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def read_land_conflicts(
    runs_dir: str,
    repos: Mapping[str, str],
    rev_parse: Callable[[str, str], str | None] = _rev_parse,
) -> list[LandConflict]:
    """Edge. `[]` on an unreadable log. A phase whose repo is unknown or whose refs do not resolve has no current ids, so it is not a conflict."""
    if not runs_dir:
        return []
    try:
        text = (Path(runs_dir) / "land.jsonl").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    rows = list(parse_log(text).rows)
    phases = dict.fromkeys((k[0], k[1]) for row in rows if (k := squash_key(row)) is not None)
    tips = {
        (i, p): (rev_parse(repos[i], f"epic/{i}/{p}"), rev_parse(repos[i], MAIN))
        for i, p in phases
        if repos.get(i)
    }
    return land_conflicts(rows, {key: (b, m) for key, (b, m) in tips.items() if b and m})
