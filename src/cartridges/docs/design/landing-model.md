# The landing model

> Written when tools, graphs and cartridges were separate repositories (`coxswain-tools`, `coxswain-graphs`,
> `coxswain-cartridges`). Since 2026.10.0 they are the folders `src/tools`, `src/graphs` and `src/cartridges` of
> one repository, each with its own `docs/design/`. Read a repository name below as that folder.

Status: approved by the chair 2026-09-08 at the operator's direction (2026-09-08): "PRs for tickets within the same
phase should merge with each other with one parent PR. The tickets get stacked in the phase PR, and the PR is
diffed to main. Once phase is complete, we only have one merge to main"; and "phase PRs should go through a
check to remove verbose changes, we want what goes into main to have all the fat trimmed." Groups intake G2
from `workspace/plans/agent-platform/2026-09-08-intake-grouping.md`. Committed identically to
`coxswain-tools`, `coxswain-graphs` and `coxswain-cartridges` under `docs/design/` so any build seat can read it.

> Superseded in part by §8. Sections 0 to 7 describe the phase as the unit of landing. The unit is now set by `land_at`,
> and its default, `ticket`, lands one pull request per initiative. Read the phase text below as the `level` case.

## 0. The defect, once

The epic builds a PHASE: every task branch is merged `--no-ff` into `epic/<initiative>/<phase>`
(`harness/epic.py:1303`), and `validate_phase` judges the tasks together (`:997`, `phase_verdict`). But
`cox runs land` lands a TASK: it cherry-picks one commit onto `pr/<task>` from main (`agent_tools/land.py:69
land_plan`), squash-merges it, then (a) deletes every `agents/<run>/*` branch — the siblings included — and
(b) keeps the phase branch, which the next epic run refuses as stale (`epic.py:684-700`). Measured
2026-09-07/08: ~10 stale-branch refusals, 2 manual git recoveries, 4 lint-only CI failures after the PR was
already open, one `cox runs land` failure from a relative `runs/` path. Every one disappears when the unit
that is built is the unit that lands.

> The phase is the unit of landing. One PR per phase, diffed to main, its tickets stacked inside it, its
> `validate_phase` reasoning as the PR body, one squash to main, then every branch the phase owned is gone.

## 1. `cox runs land <run> --phase <phase>`  (tools)

New mode beside `--task`; the default when the run record shows more than one task in the phase.

- **Precondition, pure:** `phase_landable(items, records) -> str | None` — every work item in the phase is
  `done` or `dropped` (§3), and every `done` item has a task record whose `_approved` (`land.py:41`) is None.
  Returns the first reason it is not; the edge prints it and exits 2.
- **Plan** (`land_plan` gains a `phase` branch of logic; same step vocabulary, `cli.py:272-310` executes it):
  `pick_branch epic/<initiative>/<phase>` → `checks` (§4, run in a fresh worktree of that branch) → `push`
  → `pr_create` with title `epic <initiative>: <phase>` and body from `phase_pr_body` (§2) → `wait_checks` →
  `merge --squash --delete-branch` → `clean_phase` → `mark_done` for every task record in the phase.
- **`clean_phase`** deletes `epic/<initiative>/<phase>`, `epic/<initiative>/<phase>--*` and
  `agents/<run>/<task>` for exactly the tasks in this phase, plus their worktrees under the configured root.
  Nothing else. It is the ONLY step that deletes branches; the existing run-scoped `clean` is removed.
- **`--task` survives** for a one-task phase and for emergencies, with its cleanup narrowed to
  `agents/<run>/<task>` and its own phase branch left for `--phase` to finish. Its "exactly one commit ahead"
  rule is unchanged.
- **Paths:** `_land_record` (`cli.py:234`) resolves `runs/` against the profile's `workspace_dir` and
  accepts `--runs-dir`; its refusal names the absolute directory searched and the count found:
  `land: looked in <abs>, found N task records, expected 1`.
- The stacked-task rule from intake 2026-09-06 ("a stacked task lands into its parent's PR") is SUBSUMED:
  inside a phase the stack is already merged on the phase branch.

## 2. The PR body carries the phase's evidence  (tools)

`phase_pr_body(phase_record, task_records) -> str`, pure: the phase goal line; `phase_verdict.reasoning` from
the phase record (`runs/<run>:<phase>.json`, key `phase_verdict`); then one block per ticket — id, title,
review verdict, adversary verdict, arbitration verdict (or "unanimous"), fix-loop attempts, files touched.
Dropped tickets are listed under "Dropped" with the reason from their body. No prose is generated; every line
is a field the record already holds.

## 3. `dropped` is a state  (cartridges, graphs)

- `core/workstore.py:61 STATES` gains `"dropped"`; `set_state` accepts it; `read_initiative` treats it as
  terminal like `done`. An item is dropped by a person or the chair, never by a graph.
- `harness/epic.py` skips `dropped` tasks when selecting work and when computing phase completeness
  (`record["status"] == "complete"` at `:581` counts done+dropped as complete). `agent_tools/route.py`
  `initiative_states` / `initiative_summaries` do the same, so `cox route context` reports a phase with one
  dropped ticket as complete, not stuck.
- The six items today marked `done` with a "superseded" note (intake 2026-09-08 "no state for a superseded
  ticket") are moved to `dropped` by the chair once the loader accepts it; not this spec's code.

## 4. The checks the project runs are the checks the harness runs  (cartridges, tools)

- `cartridges/local/cartridge.yaml:84` `checks:` becomes `[{name: lint, cmd: "ruff check ."}, {name: tests,
  cmd: "pytest -q"}]`, lint first because it is cheapest. `harness/checks.py run_checks` already runs the
  list and records `unrunnable`; resolution follows the pytest precedent (venv first, `uv run`, then PATH).
- `land.checks_argv` returns a LIST of argvs, one per configured check, built from the same `checks:` the
  cartridge resolves for the repo — the edge passes the resolved list in `repo_facts["checks"]`; the
  `checks` step runs them in order and stops at the first failure, naming it. A land never opens a PR that
  CI will fail for a rule the worktree could have run.

## 5. Trim the assembled diff, under a coverage floor  (graphs, last)

After `validate_phase` returns satisfied and before the phase is marked complete, the epic runs `style_pass`
ONCE over the assembled phase diff (`git diff main...epic/<initiative>/<phase>`) with the brief it already
has ("make the narrow style edit an approved diff still needs, or return nothing") plus one sentence: "remove
duplication the tickets introduced separately; remove shims whose exit condition the phase has met". Its
patch is applied on the phase branch only if the mechanical floor holds:

    pytest --collect-only -q  before → set A ;  after → set B ;  require A ⊆ B

and the configured checks pass. Line count may fall; collected test ids may not. A trim that trips the floor
is discarded and recorded on the phase record as `trim: refused (coverage floor)`, never retried. This is
`stack_rebase`-class write on a branch the phase owns; it stays inside the run and is visible in the PR body
as `Trim: <n> lines removed` or `Trim: none`.

## 6. Out of bounds

No change to review, arbitration or `validate_phase` judgement; no auto-merge policy change (`merge_main`
stays `never` in the base cartridge — the chair runs `land`); no rewriting of history on `main`; no change to
how task branches merge into the phase branch. The `pr/<task>` naming stays for `--task` mode.

## 7. Tests each part must carry

§1 `phase_landable` on a literal phase with (all done), (one ready), (one dropped + rest done), (one done but
unapproved); `land_plan` in phase mode yields the step list above and `clean_phase` names only this phase's
branches. §2 `phase_pr_body` on a two-ticket literal record with one arbitration and one unanimous. §3
`set_state(path, "dropped")` round-trips; a phase of done+dropped is complete; `initiative_summaries` reports
it complete. §4 `checks_argv` yields two argvs from a two-check config and the `checks` step stops at the
first failure. §5 the floor accepts A ⊆ B and refuses a removed test id; a refused trim leaves the branch
untouched.

## 8. Landing granularity

`landing_areas.land_at` is `ticket`, `level` or `task`. Absent means `ticket`. `core/landing.py resolve_land_at` picks
the initiative's `land_at` first, then the cartridge or overlay value, then `ticket`, and checks every value given.
It is set in three places. The team cartridge sets `landing_areas.land_at`. The repo overlay `.agent/cartridge.yaml`
may set the same key and replaces the team value. An initiative sets `land_at:` in the frontmatter of `initiative.md`
(`core/workstore.py read_initiative`), which wins over both.

The approval gate applies only at that granularity. `land_gate_level` returns the `land_at` value. At `ticket`, with no
`assume`, `harness/epic.py` clears the batch itself and writes no gate diff and no ledger row. The person decides on
the one pull request. At `level` and `task` the batch goes through the gate. In `agent_tools/land.py`, `gate_level`
takes the earlier stop of the team `gate` and the rung the PR opens: `level` maps to `phase`, `ticket` to `epic`, and
`task` keeps the team value.

What each value lands:

- `ticket`: one pull request per initiative. Levels and tasks open none. A task branch merges into its level branch,
  levels stack on their parent level's branch, and the stack is the single PR. `_card_phase_plan` opens it at the last
  level, waits for checks, squash-merges, then marks every ticket task done. A task not in the merged PR is left in its
  state with a note.
- `level`: one PR per completed level, in `epic.py`. It is based on the parent level's PR branch when that PR exists,
  else on the default branch.
- `task`: one PR per merged task against the default branch. A failed PR is a note, not a quarantine.

A level is a set of tasks that can run together. It exists to order tasks and stack their branches. The harness calls
it a phase and names its branch `epic/<initiative>/<phase>`. A level with several parents is assembled from them onto
the default ref by `harness/assembly.py assemble`, and its record carries `assembled_from`.

Status vocabulary, from `agent_tools/dash_feed.py _task_state`:

| Word | Source |
| --- | --- |
| `ready` | Store `ready`, every need landed, no live lane. |
| `building` | A live lane covers the task's initiative and phase. |
| `approved` | Store `approved`, or a task `runs stranded` lists. |
| `merged` | `approved` with `extra["merged"]` (`route.is_merged`), or `done` written at another `land_at`. |
| `landed` | `done` at the configured `land_at`. |

The merged state is stored. A task merged into its level branch stays `approved` and its row keeps `merged` and
`merged_commit`. Only a land writes `done`. On restart `_ready_view` presents a merged parent as `done` for `needs`, so
a dependent level does not wait for a person. `_merge_missing` checks that `merged_commit` is an ancestor of the level
branch, and quarantines the one task whose commit is gone. A merged task is not rebuilt. At `task` a restart opens the
PR of any stored merged task that has none. At `level` it reuses the level's PR or opens the missing one.

The task cap is `--max-parallel` on the phase and epic commands. Without it the cap is the provider profile's
`max_parallel`, a positive integer, else the free lane count (`harness/lanes.py resolve_max_parallel`).

A phase still waits for its parent phases. The epic driver holds a phase until every parent is complete, and a parent
that missed its goal blocks its dependents. Running a ready task ahead of an unfinished earlier level is planned work in
#626 and is not current behaviour. `next_launches` in `harness/lanes.py` exists with tests and is not called by the
epic driver.
