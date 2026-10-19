# epic-reconcile

epic-reconcile compares an epic's declared state with its actual state.
Both arrive as arguments (`--epic` and `--observed`); the graph reads no
tracker itself, so a run can be replayed. Set arithmetic finds the
drift first, then one node judges it. What lands is one correction
proposal per drift, gated like any other write, never a silent rewrite
of the board. It runs from `shell.py reconcile`; the coxswain does not
dispatch it.

--8<-- "components/graphs/docs/graphs/epic-reconcile.md"

--8<-- "components/graphs/graphs/ops/epic-reconcile.md"
