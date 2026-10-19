---
name: scope-work
description: Decide what shape one piece of work is — epic, parent with subtasks, or a single item — before anyone plans it.
---

# Scoping

Before work is planned it must be sized, because size decides where it lands
and how it is tracked. You apply the team's own threshold — which arrives in
the cartridge as `epic_threshold`, never from your own sense of bigness — and
route the result by the state of the work.

## Discipline

- **Most work is not an epic.** The threshold is a bar to clear, not a
  default. An organisation whose every item is an epic has a board nobody can
  read; err toward the smallest shape the work honestly fits.
- **Phases, tickets, repos: count them honestly.** The threshold keys on how
  many genuinely-ordered phases, how many independent tickets, and whether the
  change spans repositories. Do not inflate a checklist into phases —
  sequential steps inside one deliverable are one ticket.
- **`state` routes it.** Work being done now is `active`; scoped and scheduled
  is `planned`; roadmapped-for-later is `future` — and future work never
  enters the active set. Unscoped work reaching an active board is how boards
  stop meaning anything.
- **Attach before you create.** If an existing epic covers this area,
  `parent_epic` names it; a second epic for the same area splits the record of
  one effort.
- **`rationale` shows the arithmetic.** Which counts crossed which threshold,
  in one or two sentences. The reviewer of a scoping decision checks the
  counting, not the taste.

## Failure modes

- Epic inflation — prestige sizing for ordinary work.
- Counting hoped-for follow-ups as tickets. Scope what is asked.
- Routing future work as active because it feels urgent.
