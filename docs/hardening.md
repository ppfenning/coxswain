# Hardening plan

Written 2026-11-16, after a week in which the landing model, the store-as-board move and the autonomous chair
reached the live machines together, and almost nothing landed. The open initiatives were folded by hand (PRs
0, A, B and C: #1175, #1176, #1177, #1178 with towpath #239). This page lists what to change so that the next
week does not need a hand fold.

Every item names the evidence that put it here, the change, and when it is done. The order at the end is the
order to build in.

## What went wrong, in one line each

| Failure | Where it lived |
| --- | --- |
| The store command rejected `quarantined`, so every triage request and hold failed | a subprocess argument list the chair's tests stubbed |
| Whole-suite checks failed in every lane and passed by hand | `--basetemp` and temp paths leaking from the harness into child processes |
| Subprocesses ran another checkout's code | editable installs shared between checkouts |
| The lander blocked every land for 30 minutes at a time | a synchronous check wait in a single worker |
| One initiative relaunched eight times and built nothing | the landing record said a phase merged; the branch did not have it |
| Quarantines reached the chair with no cause or run | the store-mode board never fed attempts to the chair |
| Edge rolled back good commits on two machines | the doctor failed on rows unrelated to the upgrade |
| The edge timer hung | `gh` under systemd, with no timeout |
| Branches decomposed against an old `main` kept failing | nothing refreshed a stale initiative branch |
| The chair tick takes about 105 s of work | re-reading files and one subprocess per store read |

None of these is a language problem. They sit at seams: between processes, machines, repositories and the
record of what happened. The plan hardens the seams first.

## Measured on the chair machine (2026-11-16)

- Tick: facts 76 s (approved 29 s, idle_stall 12 s, stale_candidates 5.7 s, decomposed_intake 5.5 s), perform
  15–19 s, export 11 s.
- 23 call sites in `src/tools` run `python -m harness…` as a subprocess; 46 modules spawn subprocesses.
- 396 `dict[str, Any]` / `Mapping[str, Any]` signatures across `chair_*`, `lander*` and `harness/epic.py`.
- No type checker runs in CI.
- `src/tools/agent_tools/cli.py` is 16,256 lines; `src/graphs/harness/epic.py` is 6,503.

## H1. One process boundary, typed

**Evidence.** The store CLI's state choices drifted from the work states (#1174). The suites' subprocesses
imported a different checkout's `core` and `harness` (seen building #1171, #1173 and in lanes).

**Change.**

1. A typed, in-process store API in `harness` (read rows, set state with compare-and-set, leases, task records,
   landing records) that `agent_tools` calls directly. The 23 `python -m harness…` call sites move to it.
2. The CLIs stay as thin wrappers over the same API, for people and for remote hosts.
3. One installed package per machine. No editable install points at another checkout; a lane's worktree runs its
   own code through `PYTHONPATH` set by the harness, never through the shared venv.
4. A contract test per remaining process boundary (`cox` ↔ harness CLIs, forge `gh`, git) that runs the real
   command, not a stub.

**Done when.** No `python -m harness` call remains in `agent_tools`; a test asserts the CLI's choices equal the
API's enums; a lane check imports only its worktree's code (a test proves it with two checkouts).

## H2. Types on the core

**Evidence.** 396 loose dict signatures in the code that plans and performs actions on every project.

**Change.**

1. Pyright (or mypy) in strict mode on `chair_*`, `lander*`, `harness/epic.py`, `harness/store*`, `core/`, as a
   required CI job. Other modules stay on the basic level.
2. `Facts`, `Action`, `Result`, landing records and task records become frozen dataclasses (or fully typed
   TypedDicts with no `Any`), with one constructor each that validates input from the store.
3. Action kinds are an enum, and the executor's dispatch is checked exhaustive.

**Done when.** The strict job passes with no ignores in those modules, and no `dict[str, Any]` remains in their
signatures.

## H3. The lander does not block

**Evidence.** A repo with no CI held every land for 30 minutes, again and again (#1167). A repo with CI still holds
the lander for the length of its run.

**Change.**

1. A land polls its checks once per pass and returns `waiting`; the next pass picks it up. No in-pass wait.
2. Lands and phase merges run per repository, so one repository's slow checks never hold another's.
3. Landing record ↔ git reconciliation: each pass, a record that says a phase merged is checked against the
   initiative branch. A mismatch is repaired when it can be (re-merge) and held with a reason when it cannot.

**Done when.** A test lands two initiatives in two repositories while one repository's checks stay pending; the
efficiency case (record merged, branch missing the commit) is detected and held with a reason on the first pass.

## H4. No loops, no stale work

**Evidence.** `efficiency-…` relaunched eight times with the same outcome. Sixteen initiative branches were 18–51
commits behind `main`; six conflicted.

**Change.**

1. No-progress guard: an initiative whose last N runs (setting, default 2) ended with the same phase states and
   no new task state is held with a "needs you" reason naming the repeated outcome. This replaces timing-based
   loop rules.
2. A stale-branch policy: before a run starts, its initiative branch is brought forward onto its parent (merge
   when clean, a rectify task in the `integrate` phase when it conflicts), so a phase never builds on an old base.
3. The epic's own attempt cap and resume-from-last-attempt read run records in store mode, as the chair does
   since #1173.
4. A triage call that fails (the graph errors or refuses every answer) holds the task with the failure named,
   instead of quarantining it again.

**Done when.** Tests cover each: a repeated outcome holds after N runs; a run on a branch behind its parent
starts from the refreshed branch; a store-mode task's attempt cap counts record attempts.

## H5. Edge and deploy cannot wedge a machine

**Evidence.** The edge timer hung on `gh` under systemd. Edge rolled back twice for doctor rows unrelated to the
move (a token file, a plugin path). The macOS lane host has no scheduler. Lane hosts sync their install checkout to `main`
between timer runs, which skips the green-commit rule.

**Change.**

1. Every `gh` and `git` call has a timeout and a clear failure line.
2. The doctor has two tiers: rows required for an upgrade (venv, imports, store reachable, units start) and
   advisory rows. Edge rolls back only on the required tier.
3. One environment definition for the chair, lander and upgrade units (token, store, traces), written by one
   command, so a new unit cannot miss the token again.
4. A launchd schedule for macOS hosts.
5. A lane host builds in its own checkout, separate from the install, so the install moves only through edge.

**Done when.** A hung `gh` is killed and reported; an advisory doctor failure does not roll back; all three units
read one env file; the macOS lane host upgrades on a schedule; a lane sync never moves the install.

## H6. The chair tick is cheap, then event-driven

**Evidence.** About 105 s of work per tick, most of it in fact gathering that re-reads unchanged data.

**Change.**

1. Measure, then cache by change stamp (the Postgres stamp from PR B) across the chair's readers, starting with
   `approved` (29 s) and `idle_stall` (12 s). Target: a quiet tick under 5 s.
2. Then the event-driven chair, redesigned from today's shape: a queue per kind of work, as the lander already has
   for lands, fed by store changes, run exits and timers. This replaces the dropped `chair-is-event-driven…`
   design, which predates the lander.

**Done when.** A quiet tick takes under 5 s on the chair machine; a design page for the queues is approved before
any build.

## H7. The operator is told

**Evidence.** The chair sat idle for hours more than once, and only a person looking noticed.

**Change.**

1. A stall alarm for the landing model: no land for N hours while initiatives are assembled or open; no launch
   while lanes are free and work is ready; the same failure reason N ticks in a row. One courier event per stall,
   cleared when it clears. This replaces the dropped `the-chair-tells…` initiative.
2. Towpath shows held tasks and their "needs you" reasons as their own state, not as blocked.
3. The release tool's merge-back and tap steps are idempotent and resume cleanly (both stalled at 11.8 and 11.9).

**Done when.** A test stall raises one event and its all-clear; towpath renders a held task distinctly; a
re-run of `devtools release` after a finished merge-back exits 0.

## H8. One contract across repositories

**Evidence.** Towpath's fixture copies had drifted from coxswain's before PR B.

**Change.** Towpath's tests read coxswain's feed fixtures at a pinned coxswain version (fetched in CI, or a
generated package), not hand copies; the feed carries a contract version towpath checks.

**Done when.** Changing a coxswain fixture without a towpath change fails towpath's CI.

## H9. Split the monoliths

**Evidence.** `cli.py` at 16k lines and `epic.py` at 6.5k carry most of the regressions above.

**Change.** Mechanical moves, one command group or one driver stage per module, after H1 and H2 so the types
guard the moves. No behaviour change.

**Done when.** No module in `agent_tools` or `harness` exceeds 2,000 lines.

## H10. Functional core, tests that prove it runs

**Evidence.** H5's first phase, built by the epic on 2026-11-17, passed every task review, arbitration and the full
lane check, yet four of its five tasks added functions that no running code called: edge still rolled back on the
full doctor, its `gh` calls stayed unbounded, the lane-sync guard was never passed its paths, and no unit read the
new environment file (#1191, review). Each task was judged against unit tests of its own helper, with the process
boundaries stubbed; nothing ran the glue that should have called it. The suites are large (about 12,700 tests in
`src/tools`) and lean on stubs and fakes, which is also how the store CLI's state choices drifted unseen (#1174).

**Rule: functional core, imperative shell, for all work.** Logic lives in pure functions: no I/O, no clock, no
environment, no global state; data in, data out. Side effects (subprocesses, `gh`, `git`, the store, files, the
network, the clock) live in thin glue at the edges that only gathers inputs, calls the core and acts on its result.
New code follows this, and code a change touches is moved toward it.

**Change.**

1. **Unit tests cover the pure core, with no mocks.** A unit test passes example inputs to a pure function and
   checks its output. It does no I/O. If a test needs a mock, monkeypatch or fake to reach the logic, the logic is
   tangled with an effect: split it out instead of mocking. A function that builds a command, a query or a decision
   is tested; the glue that runs it is not unit-tested.
2. **The glue is proven by end-to-end smoke tests against real infrastructure.** A small set of tests runs real
   `cox` commands, as a person or a unit would, against a real git repository, a throwaway Postgres and a sandbox
   forge, in containers as the release rehearsal already does. Each covers a happy path at an entry point
   (`cox upgrade` rolls back on a required doctor row and not on an advisory one; a lane sync leaves the install
   alone; the chair, lander and upgrade units start from one environment file). They replace stubbed boundary tests,
   and absorb H1.4's contract tests.
3. **Every real bug gets a regression test,** at the level where it lived: a pure-function case when the logic was
   wrong, a smoke case when the wiring was. Coverage is not a target.
4. **A done-when names a runtime behaviour at an entry point,** proven by a smoke test from item 2, not a helper's
   return value.
5. **Unwired code is a finding.** A check lists the public symbols a diff adds and fails when one has no caller
   outside tests (`.github/check_unwired.py`; an epic lane diffs against its phase branch, `COX_LANE_BASE`). It
   catches a name nothing calls, not a caller that passes the wrong arguments (the lane-sync guard that was never
   passed its paths): that is the smoke suite's to prove.
6. **Review asks two questions.** Task review, arbitration and `validate_phase` ask: is the logic pure and tested
   without mocks, and which smoke test runs the entry point that reaches this change? A mock the patch adds to a
   unit test, or no such smoke test, means revise. The smoke half applies as the smoke suite grows: where an entry
   point has no smoke case yet, the patch that changes it adds one.
7. **Decompose keeps a helper and its wiring together,** in one task or one phase, so a phase cannot complete with
   library code alone.
8. **Prune.** Tests that only exercise mocks, stubs or fakes are retired once a pure-function test or a smoke test
   covers the same behaviour. The suite gets smaller and each test earns its place.

**Done when.** The smoke suite runs in CI and in the lane check against real git, Postgres and a sandbox forge, and
covers the H5 done-when lines; the unused-symbol check runs in CI and the lane check; the review, validator and
decompose skills carry items 6 and 7, and a test shows a review of an unwired helper or a mocked unit test returns
revise; new code merged after this lands adds no mocks to unit tests.

## Order

1. **H5** (edge and deploy): small, and every later change ships through it.
2. **H1** (one process boundary): removes the class of bug behind most of this week.
3. **H2** (types): guards everything after.
4. **H3** and **H4** (lander, loops, stale work): what stops a hand fold being needed again.
5. **H7** (operator told).
6. **H6** (tick cost, then event-driven), **H8** (contracts), **H9** (split).

**H10** (functional core, tests that prove it runs) applies from now on to all work and every review. Its smoke
suite is built first, alongside **H1**, since H1's boundary work and every later item are proven through it; the
prune (item 8) runs after **H9**.

Each item is built as its own initiative once the chair is stable on PRs 0–C, and each lands behind the review
step those PRs used: an independent reviewer reads the diff before it merges.
