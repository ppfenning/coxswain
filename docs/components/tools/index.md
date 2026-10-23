# Tools

Tools is the `cox` CLI itself: the command that installs Coxswain, dispatches
work to the crew, and drives the loop from intake to landing on a branch.

## What it owns

The `src/tools` folder holds the `agent_tools` package: the `cox` command,
its subcommands (`cox install`, `cox setup`, `cox route`, `cox runs` and the
rest), and the dispatcher that enforces the in-flight bound.

## Installing it

Tools is the `coxswain` package's command, so installing `coxswain` installs
it. See [Install](../../install/index.md).

## Its own docs

The folder's README and docs live at
[src/tools](https://github.com/ppfenning/coxswain/tree/main/src/tools).
