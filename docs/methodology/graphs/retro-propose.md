# retro-propose

retro-propose reads the ledger back. It computes the stats itself, per
`(kind, subject)` bucket: outcome counts, the clean streak, reversals,
and cleans that were not first-try. One node then reasons over those
numbers, and every observation and proposal must cite the buckets it
rests on; a cite to a bucket that does not exist is refused. It emits
only `doc_update` proposals. Zero ledger rows means no model call at
all. The coxswain can dispatch it when the ledger has rows, and
`shell.py retro` runs it directly.

--8<-- "components/graphs/docs/graphs/retro-propose.md"

--8<-- "components/graphs/graphs/ops/retro-propose.md"
