"""`cox chair parity`: §5.3 step 3 of the cutover plan, as a command. The tick and the queue workers, run dry against
one store, must plan the same actions kind by kind (C6).

Three readings of the same store, every one a dry run that takes no lease and performs nothing:

- `alone`: `cox chair run --once --dry-run --as-holder` with `chair.queues: []`, the tick as it runs before the stop;
- `listed`: the same tick with every kind listed, which must plan none of a worker's actions;
- `workers`: each `cox chair worker --kind <k> --once --dry-run` with every kind listed.

The verdict: each worker plans only actions the lone tick planned, the listed tick plans nothing at all (it is stopped
at the cutover, so what it still plans would strand), and the workers' counts plus the listed tick's equal the lone
tick's, so nothing is lost and nothing is planned twice. Actions are
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
from pathlib import PurePath

from agent_tools.chair_kinds import QUEUE_KINDS

__all__ = [
    "Reading",
    "StoreIdentity",
    "Verdict",
    "judge",
    "scratch_profile",
    "store_refusal",
    "tick_reading",
    "verdict_lines",
    "worker_reading",
    "workspace_refusal",
]

# What a store is, not how it is named: on Postgres the server's `system_identifier` (`pg_control_system()`, one per
# cluster, so a replica or a restored copy on another server differs) and `current_database()`; on sqlite the resolved
# file path. Two URLs that differ only by host spelling, port forwarding, a query string or a symlink are one store.
StoreIdentity = tuple[str, ...]

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
    # The cutover stops the tick, so whatever it still plans with every kind listed is planned by no one after the stop
    # (C6: the old model's land slot was such a gap until it went to `watch`).
    stranded = sorted(a for a in listed.counts if not any(a in w.counts for w in workers.values()))
    if stranded:
        problems.append(
            "the tick, every kind listed, still plans " + ", ".join(f"{listed.counts[a]}x {a}" for a in stranded)
            + ", which no worker plans: once the tick is stopped nothing would"
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


def store_refusal(live: StoreIdentity | str, target: StoreIdentity | str) -> str | None:
    """Pure. Why parity may not run against `target`, or None. Each side is the store's identity, or the line saying
    why it could not be read; an unreadable side refuses, since nothing then shows the target is not the live store."""
    if isinstance(target, str):
        return f"cannot tell what store --store-url is ({target}); refusing rather than risk the live one"
    if isinstance(live, str):
        return f"cannot read this profile's own store to tell it apart from --store-url ({live})"
    if live == target:
        return ("--store-url is the store this profile uses (the same "
                + ("database on the same server" if live[0] == "postgres" else "file")
                + "); restore a dump into a throwaway store")
    return None


def workspace_refusal(live: PurePath, given: PurePath | None) -> str | None:
    """Pure. Why `--workspace` may not be used, or None: the profile's own workspace (or a directory inside it, such as
    its runs directory), where the dry runs' read caches of the throwaway store would land beside the live ones. Both
    paths are resolved by the caller."""
    if given is None:
        return None
    if given == live or live in given.parents:
        return f"--workspace {given} is this profile's own workspace; leave it out, and a scratch one is used"
    return None