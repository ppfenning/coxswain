"""C5, §5.3 step 3: the queue workers together plan what the tick alone planned, against a real Postgres.

One sandbox with something due for several kinds: the first housekeeping, a quarantined task to triage, and a queued
intake to decompose. Then, kind by kind:

- `cox chair run --once --dry-run` with `chair.queues` empty plans every kind, as before C3;
- with every kind listed, the tick plans none of the kinds a worker plans;
- each `cox chair worker --kind <k> --once --dry-run` plans its own kind's actions, and together the workers' actions
  are what the tick planned alone: none lost, none planned twice.

Actions are compared by kind and count: the tick's `would:` line names each action, a worker's pass line counts them.
Nothing is stubbed.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

from smoke import plan

psycopg = pytest.importorskip("psycopg")

KINDS = ("housekeeping", "watch", "quarantine", "decompose", "launch")

QUARANTINE = """import json
from harness import store_api
url = {url!r}
row = next(r for r in store_api.queue_rows(url, "ship") if r["task_id"] == "t1")
attempt = {{"run": "ship-1", "cause": "review", "reason": "smoke: t1's checks failed", "ts": "2026-11-20T10:00:00Z"}}
extra = {{**(row.get("extra") or {{}}), "attempts": [attempt]}}
print(json.dumps(type(store_api.upsert_queue_row(url, {{**row, "state": "quarantined", "extra": extra}}, "2026-11-20T10:00:00Z")).__name__))
"""


def _seed(sandbox) -> None:
    """A started initiative whose run exited with t1 quarantined (t0 done), and one intake waiting for a decompose."""
    sandbox.write("work/ship/initiative.md", plan.work_item_text({"id": "ship", "title": "Ship"}, "Ship it."))
    for task, state in (("t0", "done"), ("t1", "ready")):
        fields = {"state": state, "title": f"Task {task}", "id": task, "phase": "p1", "repo": "app"}
        sandbox.write(f"work/ship/p1/{task}.md", plan.work_item_text(fields, f"Task {task} body"))
    sandbox.write("intake/idea.md", plan.work_item_text({"title": "An idea", "repo": "app"}, "Build it."))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    assert sandbox.python_json(QUARANTINE.format(url=sandbox.store_url)) == "Upserted"
    with psycopg.connect(sandbox.store_url, connect_timeout=10, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, launched_at, ended_at, status) "
            "VALUES ('ship-1', '2026-11-20T09:00:00Z', '2026-11-20T10:00:00Z', 'quarantined')"
        )


def _list(sandbox, kinds: str) -> None:
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + f"chair:\n  queues: [{kinds}]\n", encoding="utf-8")


def _tick(sandbox) -> tuple[Counter[str], list[str]]:
    """The action kinds a dry-run tick plans, counted, from its `would: kind:subject, ...` status segment, and the
    subjects of its `needs_chair` lines, which are the tick's own and no kind's."""
    done = sandbox.cox("chair", "run", "--once", "--dry-run", timeout=180)
    assert done.returncode == 0, done.stdout + done.stderr
    status = done.stdout.strip().splitlines()[-1]
    part = next((p for p in status.split(" | ") if p.startswith("would:")), "would:")
    would = [w.strip() for w in part.removeprefix("would:").split(",") if w.strip()]
    asks = [w.split(":", 1)[1] for w in would if w.startswith("needs_chair:")]
    return Counter(w.split(":", 1)[0] for w in would if not w.startswith("needs_chair:")), asks


def _worker(sandbox, kind: str) -> Counter[str]:
    """The action kinds one dry-run worker pass plans, counted, from its pass line (`planned N | 2x launch_epic ...`)."""
    done = sandbox.cox("chair", "worker", "--kind", kind, "--once", "--dry-run", timeout=180)
    assert done.returncode == 0, done.stdout + done.stderr
    line = next(l for l in done.stdout.splitlines() if l.startswith(f"chair worker {kind} (dry run)"))
    return Counter({k: int(n) for n, k in re.findall(r"(\d+)x (\S+) dry_run", line)})


def test_the_workers_together_plan_what_the_tick_planned_alone_and_the_tick_none_of_it(sandbox):
    _seed(sandbox)
    alone, asked = _tick(sandbox)
    for kind in ("housekeeping", "request_triage", "launch_decompose"):
        assert alone[kind], alone  # the seed gives three kinds work
    _list(sandbox, ", ".join(KINDS))
    left, unowned = _tick(sandbox)
    workers = {kind: _worker(sandbox, kind) for kind in KINDS}
    why = (f"alone {dict(alone)} {asked}, tick with every kind listed {dict(left)} {unowned}, "
           f"workers {({k: dict(v) for k, v in workers.items()})}")
    # No worker runs here, so the tick raises its one alarm per listed kind with no owner (`queue_unowned`).
    assert sorted(unowned) == sorted([*asked, *["chair-queue"] * len(KINDS)]), why
    for kind, planned in workers.items():
        assert not set(planned) & set(left), (kind, why)  # the tick plans none of a listed kind's actions
        assert all(alone[a] >= n for a, n in planned.items()), (kind, why)  # nothing the tick alone did not plan
    assert sum(workers.values(), Counter()) + left == alone, why  # none lost, none planned twice
