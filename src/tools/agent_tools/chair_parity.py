"""`cox chair parity`: §5.3 step 3 of the cutover plan, as a command. The tick and the queue workers, run dry against
one store, must plan the same actions kind by kind (C6).

Three readings of the same store, every one a dry run that takes no lease and performs nothing:

- `alone`: `cox chair run --once --dry-run --as-holder` with `chair.queues: []`, the tick as it runs before the stop;
- `listed`: the same tick with every kind listed, which must plan none of a worker's actions;
- `workers`: each `cox chair worker --kind <k> --once --dry-run` with every kind listed.

The verdict: each worker plans only actions the lone tick planned, the listed tick plans none of them, and the workers'
counts plus the listed tick's equal the lone tick's, so nothing is lost and nothing is planned twice. Actions are
compared by kind and count, as C5's smoke test (`smoke/test_chair_queue_parity.py`) compares them: the tick's `would:`
segment names each action, and a worker's pass line counts them. The `needs_chair` lines are set aside on both sides:
the listed tick raises one `queue_unowned` per kind, since no worker runs during the comparison.

Pure: this module parses the outputs and judges them; the edge (`cli_chair_parity`) runs the commands.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from agent_tools.chair_kinds import QUEUE_KINDS

__all__ = ["Reading", "Verdict", "judge", "scratch_profile", "tick_reading", "verdict_lines", "worker_reading"]

_WORKER_COUNT = re.compile(r"(\d+)x (\S+) dry_run")


@dataclass(frozen=True)
class Reading:
    """What one dry run planned: action kinds counted, and the subjects of its `needs_chair` lines."""

    counts: Counter[str]
    asks: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    ok: bool
    problems: tuple[str, ...]


def tick_reading(stdout: str) -> Reading | None:
    """The tick's plan from its last status line's `would: kind:subject, ...` segment; None when the output has no
    status line (the tick refused or failed). A tick with nothing to do has no `would:` segment: an empty reading."""
    lines = [line for line in stdout.strip().splitlines() if " | " in line]
    if not lines:
        return None
    part = next((p for p in lines[-1].split(" | ") if p.startswith("would:")), "would:")
    would = [w.strip() for w in part.removeprefix("would:").split(",") if w.strip()]
    asks = tuple(w.split(":", 1)[1] if ":" in w else "" for w in would if w.startswith("needs_chair"))
    return Reading(Counter(w.split(":", 1)[0] for w in would if not w.startswith("needs_chair")), asks)


def worker_reading(kind: str, stdout: str) -> Reading | None:
    """One dry-run worker pass from its pass line (`chair worker K (dry run) ... | planned N | 2x launch_epic dry_run`);
    None when there is no pass line. `needs_chair` counts are set aside, as on the tick's side."""
    line = next((l for l in stdout.splitlines() if l.startswith(f"chair worker {kind} (dry run)")), None)
    if line is None:
        return None
    counts = Counter({k: int(n) for n, k in _WORKER_COUNT.findall(line)})
    asks = counts.pop("needs_chair", 0)
    return Reading(counts, ("",) * asks)


def judge(alone: Reading, listed: Reading, workers: Mapping[str, Reading]) -> Verdict:
    """Pure. Whether the workers together plan what the tick alone planned, kind by kind (the module docstring)."""
    problems: list[str] = []
    for kind in QUEUE_KINDS:
        planned = workers.get(kind)
        if planned is None:
            problems.append(f"{kind}: no dry-run pass was read")
            continue
        both = sorted(set(planned.counts) & set(listed.counts))
        if both:
            problems.append(f"{kind}: the listed tick still plans {', '.join(both)}, which the {kind} worker plans")
        extra = sorted(a for a, n in planned.counts.items() if n > alone.counts.get(a, 0))
        if extra:
            problems.append(
                f"{kind}: plans more than the tick alone: "
                + ", ".join(f"{a} {planned.counts[a]} > {alone.counts.get(a, 0)}" for a in extra)
            )
    total: Counter[str] = Counter(listed.counts)
    for reading in workers.values():
        total.update(reading.counts)
    lost, doubled = alone.counts - total, total - alone.counts
    if lost:
        problems.append("planned by the tick alone and by no worker: " + ", ".join(f"{n}x {a}" for a, n in sorted(lost.items())))
    if doubled:
        problems.append("planned twice: " + ", ".join(f"{n}x {a}" for a, n in sorted(doubled.items())))
    return Verdict(not problems, tuple(problems))


def _counts(reading: Reading) -> str:
    return ", ".join(f"{n}x {a}" for a, n in sorted(reading.counts.items())) or "nothing"


def verdict_lines(alone: Reading, listed: Reading, workers: Mapping[str, Reading], verdict: Verdict) -> list[str]:
    """Pure. The report: what each reading planned, then the verdict and each problem."""
    rows = [
        f"tick alone ({'chair.queues: []'}): {_counts(alone)}",
        f"tick, every kind listed: {_counts(listed)}",
        *(f"worker {kind}: {_counts(workers[kind]) if kind in workers else 'no pass read'}" for kind in QUEUE_KINDS),
    ]
    tail = ["parity: match"] if verdict.ok else ["parity: MISMATCH", *(f"  {p}" for p in verdict.problems)]
    return [*rows, *tail]


_REPLACED = ("workspace_dir:", "provider_profile:")


def scratch_profile(text: str, provider_profile: str, workspace: str, kinds: Sequence[str]) -> str:
    """Pure. The routing profile `text` for one dry run: its own lines, with `workspace_dir` and `provider_profile`
    pointed at the scratch ones and `chair.queues` set to `kinds`; the rest of its `chair:` block is kept. Written as
    the profile's own line format (`route.parse_profile` reads a subset of YAML), never re-serialized."""
    kept: list[str] = []
    chair: list[str] = []
    in_chair = False
    for line in text.splitlines():
        top = bool(line) and not line[0].isspace()
        if top:
            in_chair = line.split("#", 1)[0].strip() == "chair:"
            if in_chair or line.startswith(_REPLACED):
                continue
        if in_chair:
            if line.strip() and not line.strip().startswith("queues:"):
                chair.append(line)
            continue
        kept.append(line)
    return "\n".join([
        *kept, f"workspace_dir: {workspace}", f"provider_profile: {provider_profile}",
        "chair:", *chair, f"  queues: [{', '.join(kinds)}]",
    ]) + "\n"
