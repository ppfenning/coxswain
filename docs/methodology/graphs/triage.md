# triage

The `triage` graph runs as the `triage-quarantine` subcommand, because
`triage` already names alert triage. It classifies a task stranded at
its attempt cap. `facts` turns the task's attempt record into keyed
facts with no model. The `triage` node classifies the item and must cite
those facts. `emit` builds the write by class: a `ticket_amend`, an
`item_create`, a `decompose` handoff or a `notify` proposal. A
`(class, diagnosis)` pair that already fired on the same item is
escalated to a person instead of repeated. The epic driver invokes it.
Its full specification is
[triage-quarantine.md](https://github.com/ppfenning/coxswain/blob/main/src/graphs/graphs/ops/triage-quarantine.md).

--8<-- "components/graphs/docs/graphs/triage.md"
