# The epic driver

The epic driver is not a graph. It is `harness/epic.py`, the code that
`cox route launch epic` starts, and it holds no `SPEC`. It computes waves
of phases from the phase graph of a filed initiative. A phase sits in a
wave once all of its parent phases sit in earlier waves. Independent
phases in a wave run together, one lane each. A phase opens only once
every parent phase is complete, and it branches from its parent phase's
head. Inside a phase, ready tasks run in parallel up to `max_parallel` or
the usage headroom. Each phase ends with one record. A task that reaches
its attempt cap goes to `triage-quarantine`. Nothing it does merges to a
default branch: what lands is phase branches, task pull requests and
proposals.

The driver runs one initiative. Epic is its historical name, kept in the
command and the graph. It is not the epic group of initiatives. See
[the hierarchy](../index.md#the-hierarchy).

The page below is the driver's specification, kept beside the graphs it
drives.

--8<-- "components/graphs/graphs/delivery/epic-swarm.md"
