# triage-propose

triage-propose is morning triage of an alert queue. The alerts arrive as
an argument; the graph fetches nothing itself. It classifies each alert
against the runbook, verifies the classification, and proposes
`doc_update` corrections to the runbook it used. It writes nothing else.
Alerts over the fetch cap are counted and deferred to the next run,
never dropped. The coxswain can dispatch it when alerts are provided,
and `shell.py triage` runs it directly.

--8<-- "components/graphs/docs/graphs/triage-propose.md"

--8<-- "components/graphs/graphs/ops/triage-propose.md"
