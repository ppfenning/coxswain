# phase-review

phase-review is one review round of a whole phase. The phase lane builds a
phase's tasks without a review each, rolls each up onto the phase branch, runs
the phase smoke at the tip, and then asks this graph about the branch's diff
against its base, with the smoke's checks quoted as evidence. Its reviewer,
adversary and arbiter are lifecycle-propose's own, under the same tier rules;
the phase is reviewed at tier 1 or above, so a bound adversary always runs.

The reviewer (`review_phase`, bound to the `review-phase` skill, or the charter
reviewer where a team binds no phase reviewer) answers two questions: does the
phase meet its goal, and does it carry fat. `approve` finishes the phase
approved. `revise` asks for one revision task, which the lane builds and rolls
up like any task, then smokes and reviews again. `reject` quarantines the
phase with the reviewer's reason. The loop's rules are `harness/review_loop.py`:
at most three rounds an episode, and a revise the budget cannot buy finishes the
phase quarantined, "revision budget spent".
