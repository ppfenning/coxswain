# The epic driver

The epic driver is not a graph. It is `harness/epic.py`, the code that
`cox route launch epic` starts, and it holds no `SPEC`. It walks a filed
initiative phase by phase. For each phase it branches from the parent
phase's head, runs `lifecycle` once per ready task, applies and checks
each patch in a worktree the harness owns, runs `validate` over the
phase, gates the phase once, and records it. A task that reaches its
attempt cap goes to `triage-quarantine`. Nothing it does merges to a
default branch: what lands is phase branches, task pull requests and
proposals.

The page below is the driver's specification, kept beside the graphs it
drives.

--8<-- "components/graphs/graphs/delivery/epic-swarm.md"
