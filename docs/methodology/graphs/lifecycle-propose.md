# lifecycle-propose

lifecycle-propose is the development loop for one task: scope, plan,
optionally a competing plan and an attack on the plan, build in a
worktree, a handoff check, review, adversary and arbitration. The build
node returns a patch; the harness applies it in a worktree it owns. What
lands is reviewed work and proposals, such as a draft pull request.
Nothing is pushed, opened or merged by the graph. The epic driver runs
it once per ready task, and `shell.py phase` runs it over one phase.

--8<-- "components/graphs/docs/graphs/lifecycle-propose.md"

--8<-- "components/graphs/graphs/delivery/lifecycle-propose.md"
