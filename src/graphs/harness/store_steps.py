"""Step results: one `step_results` row per finished step of a run, written once.

The key is `(run_id, step)`, with `step` from `step_key`: `task/<task_id>/<kind>` for a task's own steps and
`phase/<kind>/<round>` for a phase's. A write is an insert-ignore, so a repeat of the same step in the same run changes
nothing: a second save of one result, a retried write after a lost connection and a resumed run all leave the row the
first write left.

`steps_of_result` turns one lifecycle result, as the epic driver saves it, into its plan, build, review, adversary,
arbitration and checks rows. It is pure: the run, the place and the time arrive as arguments. Only `put_step` and
`steps_for` touch the store.

On today's model a fix-loop re-entry (`epic_build.refix_task`) is a second lifecycle run of the same task inside the
same epic run; its graph run id ends `:refix<n>`, and its steps are keyed as a task of their own,
`task/<task>-refix-<n>/<kind>`, the way a revision task is (`task/<phase>-rev-<round>/build`). The `task_id` column
still names the task.

Only the steps this run performed get a row. A result a run carries but did not produce (a resumed run's reused
result, the quarantined attempt a rescue starts from) names its origin: its graph `run_id` is the run that ran its
plan, build and reviews, and `change_facts.checks_run` the run that ran the checks stored with it (`with_checks`). A
graph step is this run's when that run id is the run or one of its graphs (`<run>:...`, `belongs`); checks are when
`checks_run` is. A build the result did not make (`build.reused`, the patch a rescue reviews) has no row.

Where today's lanes write them (`epic_records.record_steps`, which `save_task_result` calls):
- a task's graph steps and its checks, when its result is saved (`epic_phase`);
- a fix-loop re-entry's graph steps, approved or not, and the checks that sent the attempt before it back
  (`epic_build.refix_task`);
- the checks that failed before a successful style-pass edit, as `checks-prestyle` (`checks_stage`), since the checks
  re-run on the edit take the attempt's `checks` key;
- a rescue's review, adversary and arbitration, whatever their verdict (`epic_execute.rescue_task`).

Steps that run but get no row yet: a rescue's harness checks (`rescue_checks.verify_patch` returns evidence rows, not
check results), the style-pass edit itself, and every phase-level step but the phase lane's smoke (`phase_lane._smoke`
writes `phase/smoke/<round>`): suite and the phase validators.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal, cast

from harness.store_dialect import Connection, Json, JsonObject, insert_ignore, json_load, json_text
from harness.store_types import StepRow

__all__ = [
    "PHASE_KINDS",
    "STEP_COLUMNS",
    "TASK_KINDS",
    "belongs",
    "collision_steps",
    "put_step",
    "refix_of",
    "step_key",
    "step_row",
    "steps_for",
    "steps_of_result",
    "with_checks",
]

Scope = Literal["task", "phase"]

# A task's own steps, and a phase's. review, adversary and arbitration are a task's on today's model, where each
# task is reviewed in its own lane, and a phase's on the phase model.
TASK_KINDS: tuple[str, ...] = (
    "plan", "build", "checks", "checks-prestyle", "rollup", "collision", "review", "adversary", "arbitration",
)
PHASE_KINDS: tuple[str, ...] = ("smoke", "review", "adversary", "arbitration", "suite", "triage")

STEP_COLUMNS: tuple[str, ...] = (
    "run_id", "step", "initiative", "phase", "task_id", "kind", "round", "ok", "verdict", "subject_sha", "worker",
    "claim_epoch", "started_at", "finished_at", "cost_usd", "result_json", "artifacts_json",
)
_KEY = ("run_id", "step")

_REFIX = re.compile(r":refix(\d+)(?::|$)")


def step_key(scope: Scope, ident: str, kind: str, round_: int) -> str:
    """Pure. The step's key inside its run: `task/<ident>/<kind>`, or `phase/<kind>/<round>` (`ident` unused there).

    Raises ValueError on a kind the scope has no step for, an ident that is empty or holds `/`, or a negative round,
    so no two different steps can spell one key."""
    kinds = TASK_KINDS if scope == "task" else PHASE_KINDS
    if kind not in kinds:
        raise ValueError(f"no {scope} step of kind {kind!r}")
    if round_ < 0:
        raise ValueError(f"round {round_} is negative")
    if scope == "phase":
        return f"phase/{kind}/{round_}"
    if not ident or "/" in ident:
        raise ValueError(f"task id {ident!r} is empty or holds '/'")
    return f"task/{ident}/{kind}"


def refix_of(graph_run_id: str) -> int:
    """Pure. The fix-loop re-entry a lifecycle graph run id names (`<run>:<phase>:refix<n>:<task>`); 0 for none."""
    found = _REFIX.search(graph_run_id)
    return 0 if found is None else int(found.group(1))


def belongs(origin: str, run_id: str) -> bool:
    """Pure. Whether `origin`, a run id a result names, is `run_id` itself or one of its graphs (`<run_id>:...`)."""
    return bool(run_id) and (origin == run_id or origin.startswith(f"{run_id}:"))


def with_checks(
    result: Mapping[str, object], checks: Sequence[Mapping[str, object]], run_id: str, *, stage: str = ""
) -> dict[str, object]:
    """Pure. `result` with `checks` under `change_facts`, stamped with `run_id`, the run that ran them, and `stage`
    when the checks are not the attempt's last (`prestyle`: before a style-pass edit); no stage is the last."""
    facts = {k: v for k, v in _mapping(result.get("change_facts")).items() if k != "checks_stage"}
    staged = {"checks_stage": stage} if stage else {}
    return {**result, "change_facts": {**facts, "checks": [dict(c) for c in checks], "checks_run": run_id, **staged}}


def step_row(
    *,
    run_id: str,
    step: str,
    initiative: str,
    phase: str,
    task_id: str,
    kind: str,
    round_: int,
    ok: bool,
    verdict: str,
    result: JsonObject,
    finished_at: str,
    started_at: str = "",
    subject_sha: str = "",
    worker: str = "",
    claim_epoch: int = 0,
    cost_usd: float = 0.0,
    artifacts: Sequence[JsonObject] = (),
) -> StepRow:
    """Pure. A `step_results` row; every column filled, '' or 0 for none. A blank `started_at` reads `finished_at`."""
    return {
        "run_id": run_id,
        "step": step,
        "initiative": initiative,
        "phase": phase,
        "task_id": task_id,
        "kind": kind,
        "round": round_,
        "ok": 1 if ok else 0,
        "verdict": verdict,
        "subject_sha": subject_sha,
        "worker": worker,
        "claim_epoch": claim_epoch,
        "started_at": started_at or finished_at,
        "finished_at": finished_at,
        "cost_usd": cost_usd,
        "result_json": result,
        "artifacts_json": list(artifacts),
    }


def _mapping(value: object) -> Mapping[str, object]:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def _is_mapping(value: object) -> bool:
    """Whether `value` is a mapping, without narrowing it: `_mapping` gives it its type."""
    return isinstance(value, Mapping)


def _list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _rows(value: object) -> list[Mapping[str, object]]:
    return [_mapping(v) for v in _list(value) if _is_mapping(v)]


def _count(value: object) -> int:
    return len(_list(value))


def _json(value: object) -> Json:
    """A cell for result_json: JSON scalars and lists of strings as they are, anything else as its text."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    listed = _list(value)
    return [str(v) for v in listed] if listed or value == [] else str(value)


def _round_of(result: Mapping[str, object]) -> int:
    """The review round the result's build and review belong to: the fix loop's attempt, 1 when it names none."""
    attempts = _mapping(result.get("fix_loop")).get("attempts")
    return attempts if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts > 0 else 1


def _plan(result: Mapping[str, object]) -> tuple[bool, str, JsonObject] | None:
    plan = _mapping(result.get("plan"))
    if not plan:
        return None
    attack = _mapping(result.get("plan_attack"))
    return True, str(attack.get("verdict") or ""), {
        "steps": _count(plan.get("steps")),
        "files_expected": _json(plan.get("files_expected") or []),
    }


def _build(result: Mapping[str, object]) -> tuple[bool, str, JsonObject] | None:
    build = _mapping(result.get("build"))
    if not build or build.get("reused") is True:
        return None
    facts = _mapping(result.get("change_facts"))
    ok = bool(str(build.get("patch") or "").strip()) and not build.get("quarantine") and result.get("failed_node") != "build"
    return ok, "", {
        "patch_ok": _json(facts.get("patch_ok")),
        "changed_lines": _json(facts.get("changed_lines")),
        "files_touched": _json(build.get("files_touched") or []),
        "quarantine": str(build.get("quarantine") or ""),
    }


def _judge(result: Mapping[str, object], kind: str, counted: str) -> tuple[bool, str, JsonObject] | None:
    """A review or adversary answer: its verdict, approve is ok, and how many findings or objections it raised."""
    answer = _mapping(result.get(kind))
    if not answer:
        return None
    verdict = str(answer.get("verdict") or "")
    return verdict == "approve", verdict, {"verdict": verdict, counted: _count(answer.get(counted))}


def _arbitration(result: Mapping[str, object]) -> tuple[bool, str, JsonObject] | None:
    answer = _mapping(result.get("arbitration"))
    if not answer:
        return None
    verdict = str(answer.get("verdict") or "")
    return verdict == "approve", verdict, {"verdict": verdict, "sided_with": str(answer.get("sided_with") or "")}


def _checks(result: Mapping[str, object], run_id: str) -> tuple[bool, str, JsonObject] | None:
    """The configured checks `run_id` ran: under change_facts, stamped `checks_run`, in an epic save; at the top of a
    result the run's own `lifecycle` graph returned."""
    facts = _mapping(result.get("change_facts"))
    stamped = _rows(facts.get("checks")) if belongs(str(facts.get("checks_run") or ""), run_id) else []
    top = _rows(result.get("checks")) if belongs(str(result.get("run_id") or ""), run_id) else []
    ran = stamped or top
    if not ran:
        return None
    each: list[Json] = [
        {"name": str(c.get("name") or ""), "passed": c.get("passed") is True, "exit_code": _json(c.get("exit_code")),
         "outcome": str(c.get("outcome") or "")}
        for c in ran
    ]
    return all(c.get("passed") is True for c in ran), "", {"checks": each}


def steps_of_result(
    result: Mapping[str, object],
    *,
    run_id: str,
    initiative: str,
    phase: str,
    task: str,
    at: str,
    claim_epoch: int = 0,
    worker: str = "",
) -> tuple[StepRow, ...]:
    """Pure. One lifecycle result's step rows, in plan, build, review, adversary, arbitration, checks order.

    A step the result does not carry (no plan on a resumed task, no checks before they ran, no arbitration when the
    reviewers agreed) has no row. `result_json` holds only the step's verdict, counts and check names and exits:
    never a patch or a log. Plan is round 0; every other step is the round of the result's fix loop.

    Only the steps `run_id` performed: a result another run produced gives no plan, build or review rows, and checks
    another run ran give no checks row (see the module docstring)."""
    origin = str(result.get("run_id") or "")
    ours = belongs(origin, run_id)
    refix = refix_of(origin) if ours else 0
    ident = f"{task}-refix-{refix}" if refix else task
    round_ = _round_of(result)
    graph = (
        ("plan", 0, _plan(result)),
        ("build", round_, _build(result)),
        ("review", round_, _judge(result, "review", "findings")),
        ("adversary", round_, _judge(result, "adversary", "objections")),
        ("arbitration", round_, _arbitration(result)),
    )
    stage = str(_mapping(result.get("change_facts")).get("checks_stage") or "")
    found = (*(graph if ours else ()), (f"checks-{stage}" if stage else "checks", round_, _checks(result, run_id)))
    return tuple(
        step_row(
            run_id=run_id, step=step_key("task", ident, kind, n), initiative=initiative, phase=phase, task_id=task,
            kind=kind, round_=n, ok=step[0], verdict=step[1], result=step[2], finished_at=at, worker=worker,
            claim_epoch=claim_epoch,
        )
        for kind, n, step in found
        if step is not None
    )


def put_step(conn: Connection, row: StepRow) -> int:
    """Edge. Insert one step row unless its (run_id, step) is already there; 1 when inserted, 0 on a repeat."""
    sql = insert_ignore(conn.dialect, "step_results", STEP_COLUMNS, _KEY)
    cells = cast("Mapping[str, object]", row)
    return conn.execute(sql, [json_text(cells[c]) if c.endswith("_json") else cells[c] for c in STEP_COLUMNS])


def steps_for(conn: Connection, run_id: str) -> list[StepRow]:
    """Edge. Every step row of one run, by step key in code-point order, the JSON columns decoded. Sorted here, not
    by ORDER BY, so both engines list the same order whatever the server's collation."""
    sql = f"SELECT {', '.join(STEP_COLUMNS)} FROM step_results WHERE run_id = {conn.dialect.placeholder}"
    rows = [
        cast(StepRow, {c: json_load(v) if c.endswith("_json") else v for c, v in zip(STEP_COLUMNS, r, strict=True)})
        for r in conn.query_all(sql, (run_id,))
    ]
    return sorted(rows, key=lambda row: row["step"])


def collision_steps(conn: Connection) -> list[StepRow]:
    """Edge. Every `collision` step row (`lane_merge.collision_row`), oldest first by when it finished: the record that
    two tasks a decompose called independent edited the same files, which the chair counts as a miss of that
    decompose (`chair_decompose_streak`)."""
    sql = f"SELECT {', '.join(STEP_COLUMNS)} FROM step_results WHERE kind = {conn.dialect.placeholder}"
    rows = [
        cast(StepRow, {c: json_load(v) if c.endswith("_json") else v for c, v in zip(STEP_COLUMNS, r, strict=True)})
        for r in conn.query_all(sql, ("collision",))
    ]
    return sorted(rows, key=lambda row: (str(row["finished_at"] or ""), row["run_id"], row["step"]))
