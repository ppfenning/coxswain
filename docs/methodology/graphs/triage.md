# triage

The `triage` graph runs as the `triage-quarantine` subcommand, because
`triage` already names alert triage. It classifies a task stranded at
its attempt cap, or a quarantined task the chair sent to triage. `facts`
turns the task's attempt record into keyed facts with no model. The
`triage` node reads those facts and the whole initiative: its goal, its
phases with their goals, and the other tasks. It must cite the facts.

The class decides what happens to the task:

- `ticket_defect` appends an instruction to the ticket, and the task is
  built again.
- `drop` drops the task.
- `combine` folds the task into a named sibling task in the same phase.
- `shape`, `platform_defect` and `hold` hold the task for a person.
  `platform_defect` also files an intake.

A `(class, diagnosis)` pair that already fired on the same item holds the
task for a person instead of repeating. The epic driver invokes it, and
[the autonomous chair](../../customize/autonomous-chair.md#quarantines)
says when.
Its full specification is
[triage-quarantine.md](https://github.com/ppfenning/coxswain/blob/main/src/graphs/graphs/ops/triage-quarantine.md).

--8<-- "components/graphs/docs/graphs/triage.md"
