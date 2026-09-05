# Policy

This page settles what each policy knob changes and what a sane default
looks like, for the four values most teams touch first.

## review_tier

Which provider tier fills the charter reviewer and adversary seats for
this team's runs. Raising it spends more per review in exchange for a
stronger read on harder changes. A sane default is the same tier the
builder runs on — spending less on review than on the work it's
checking usually means the review is the weaker of the two.

## plan_competition.min_tier

The minimum tier a candidate plan must have run on to be considered when
more than one plan is generated for the same task. A sane default is
whatever tier plan itself normally runs on — this knob exists for teams
that want cheap draft plans filtered out before a human ever compares
them, not to gate every team's planning step.

## build_budget_usd_max

The ceiling `budget_usd` on a build node can't exceed, even for a team
that wants to try raising it. This is the enforcement point for
[Budgets](../methodology/budgets.md)'s tighten-only rule at the team
level: a team can set a build's own `budget_usd` lower than this, never
higher. A sane default is the base tier's own build ceiling — a team
wanting more room should be splitting the task, not raising this.

## dispatch.max_in_flight

The cap on how many runs the coxswain will dispatch at once — the
in-flight bound described in [Crew and
seats](../methodology/crew-and-seats.md). A sane default is small enough
that a human reviewing pull requests isn't handed more at once than they
can actually read; raising it trades review attention for throughput,
and that trade should be a deliberate policy choice, not a default
nobody looked at.
