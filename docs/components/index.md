# Components

The repositories that make up a Coxswain release, pulled in from `manifest.toml`.

Three are required: without them there is no CLI and nothing to run. Two are
optional: crew, installed with `cox install --with crew`, and dash, installed
with cargo (see its page). Each lives in its own
repository, tagged with the same Coxswain version at release. Every component,
required or not, is tagged with the same Coxswain version at release, so
`manifest.toml` always tells you exactly what a given install has, in
lockstep.

| Name | Repository | Required or flag | What it does |
| --- | --- | --- | --- |
| [Cartridges](cartridges.md) | [ppfenning/coxswain-cartridges](https://github.com/ppfenning/coxswain-cartridges) | required | Packages a team's or repo's context: conventions, charter, thresholds. |
| [Graphs](graphs.md) | [ppfenning/coxswain-graphs](https://github.com/ppfenning/coxswain-graphs) | required | Defines the ordered nodes a run executes: plan, build, review, arbitrate, validate. |
| [Tools](tools.md) | [ppfenning/coxswain-tools](https://github.com/ppfenning/coxswain-tools) | required, provides `cox` | The `cox` CLI: install, dispatch, and run the loop. |
| [Crew](crew.md) | [ppfenning/coxswain-crew](https://github.com/ppfenning/coxswain-crew) | flag: `crew` | The agent seats that plan, build, review, and validate. |
| [Dash](dash.md) | [ppfenning/coxswain-dash](https://github.com/ppfenning/coxswain-dash) | flag: `dash`, provides `coxtop` | A terminal dashboard for the fleet (beta, from 0.26.0). |
