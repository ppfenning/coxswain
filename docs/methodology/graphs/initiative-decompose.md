# initiative-decompose

initiative-decompose turns an idea into phases, tasks and the dependency
edges between them. An adversary then attacks the edges, because an edge
that is not real serializes work that could run at once. What lands is
one `item_create` proposal per task; the work store is written only by
an apply arm after the gate clears them. `cox route launch decompose`
starts it, and the coxswain can dispatch it when the intake queue holds
items.

--8<-- "components/graphs/docs/graphs/initiative-decompose.md"

--8<-- "components/graphs/graphs/delivery/initiative-decompose.md"
