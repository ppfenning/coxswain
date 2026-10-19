# Policy

This page settles what each policy knob changes and what a sane default
looks like, for the values most teams touch first. Every key here lives
under `policy:` in a cartridge. The values quoted are base's.

## review_tier

How much review a change must survive. The harness puts each change in
one of three tiers:

| Tier | Who reviews |
| --- | --- |
| 0 | The charter reviewer alone. |
| 1 | The charter reviewer and the adversary. |
| 2 | Both, and the arbiter runs whether or not they disagree. |

The keys decide the tier:

- `tier2_surfaces` (base: schema, migration, deletion, auth,
  infrastructure, cross_repo, production_write_path). A change on any of
  these is tier 2, whatever its size. This check runs first.
- `tier0_patterns` (base: docs_only, rename_only, config_bump,
  param_tweak). A change whose patterns are all in this list is tier 0.
- `tier0_max_changed_lines` (base: 30). A change this small, in one
  module, is tier 0. Unset or 0 turns this off.
- `tier1_max_changed_lines` (base: 150) and `tier1_max_modules` (base: 1).
  A change within both is tier 1. Anything bigger is tier 2.

No tier skips review. Tier 0 is the cheapest review, not none.

## plan_competition.min_tier

The pre-build tier at which the plan competition runs: a second,
independent plan, an arbiter that picks or merges the two, and an attacker
that tests the chosen plan before anyone builds it. The tier is judged
from the task's surfaces and patterns, since no diff exists yet. Base sets
1. Set 0 to run the competition on every task, or 3 to never run it.

## build_budget_usd_max

The most a work item may ask for with its own `budget_usd:` field. Base
sets 6.0, the runaway ceiling. A build without `budget_usd` uses its
measured guide. The harness refuses an item that asks for more than this
and quotes the number. See [Budgets](../methodology/budgets.md).

## dispatch.max_in_flight

How many harness runs the chair may have alive at once on one machine.
Base sets 3. It applies per machine; each lane host has its own capacity
in the hosts table. A sane default is small enough that a person
reviewing pull requests is not handed more at once than they can read.

## gate

How far a land goes without a person: `ticket` (base), `phase`, `epic` or
`full`. See [Landing](../methodology/landing.md#pull-requests-are-where-a-person-decides).

## pacing

How a launch reasons about the 5-hour window: `window_hours`,
`spent_vs_elapsed_margin`, `late_threshold`, `min_headroom_usd`, and the
`tier_ladder` and `effort_ladder` a launch degrades along.

The hard stops are not cartridge keys. The chair reads
`hard_stop_fraction` (default 0.99) and `weekly_hard_stop_fraction`
(default 0.93) from `<runs>/policy.pacing.json` in the workspace. See
[Autonomous chair](autonomous-chair.md#limits).
