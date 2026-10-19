# Components

The parts of Coxswain. Four are folders of the one repository,
[ppfenning/coxswain](https://github.com/ppfenning/coxswain), and ship in every
release. Dash is its own repository and is optional.

| Name | Where | Installed | What it does |
| --- | --- | --- | --- |
| [Tools](tools.md) | `src/tools` | always, provides `cox` | The `cox` CLI: install, dispatch, run the loop, land. |
| [Graphs](graphs.md) | `src/graphs` | always | The graphs a run executes, and the harness that runs them. |
| [Cartridges](cartridges.md) | `src/cartridges` | always | A team's or repo's context: roles, skills, policy, providers. |
| [Crew](crew.md) | `crew` | always, linked into `~/.claude/agents` | The agent seats that plan, build, review, and validate. |
| [Dash](dash.md) | [ppfenning/coxswain-dash](https://github.com/ppfenning/coxswain-dash) | `--with dash`, provides `towpath` | A terminal dashboard for the fleet. |

The four folders are one checkout at one release tag, with one venv, so they
always match. `manifest.toml` names only dash, at the tag a release pins it to.

Each folder runs its own tests from inside it:

```sh
cd src/tools && uv run pytest -q
```

The same works for `src/graphs` and `src/cartridges`. The root suite runs from
the repository root with `uv run pytest -q`.
