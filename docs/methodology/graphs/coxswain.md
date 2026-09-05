# coxswain

The coxswain graph is the dispatch loop itself: it reads the docket,
picks the next task whose dependencies are satisfied, and starts the
graph that task's kind calls for. What lands is a run id and a worktree
for the task it picked, not a change to any board.

--8<-- "components/graphs/docs/graphs/coxswain.md"

--8<-- "components/graphs/graphs/ops/coxswain.md"
