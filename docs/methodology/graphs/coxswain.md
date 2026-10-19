# coxswain

The coxswain graph is one `dispatch` node. `harness/cos.py` assembles a
docket — the graph registry with each entry marked runnable or not, the
intake queue, the ready set, the ledger's shape — and the node picks
which graphs to run next and says why. It may pick only entries the
docket marks runnable; the driver can build arguments for `retro`,
`decompose`, `triage` and `sweep`. The driver, not the graph, then
invokes what was picked under one run id. The graph's own proposals are
always empty. `cox route launch cos` starts it.

--8<-- "components/graphs/docs/graphs/coxswain.md"

--8<-- "components/graphs/graphs/ops/coxswain.md"
