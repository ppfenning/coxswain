# Components

The repositories that make up a Coxswain release, pulled in from `manifest.toml`.

Three are required: without them there is no CLI and nothing to run. Three
are optional, installed with `cox install --with <flag>`. Six components come
from six repositories, but they are not the same six: the desktop shell ships
from this umbrella repository rather than one of its own. Every component,
required or not, is tagged with the same Coxswain version at release, so
`manifest.toml` always tells you exactly what a given install has, in
lockstep.

| Name | Repository | Required or flag | What it does |
| --- | --- | --- | --- |
| [Cartridges](cartridges.md) | [ppfenning/coxswain-cartridges](https://github.com/ppfenning/coxswain-cartridges) | required | Packages a team's or repo's context: conventions, charter, thresholds. |
| [Graphs](graphs.md) | [ppfenning/coxswain-graphs](https://github.com/ppfenning/coxswain-graphs) | required | Defines the ordered nodes a run executes: plan, build, review, arbitrate, validate. |
| [Tools](tools.md) | [ppfenning/coxswain-tools](https://github.com/ppfenning/coxswain-tools) | required, provides `cox` | The `cox` CLI: install, dispatch, and run the loop. |
| [Crew](crew.md) | [ppfenning/coxswain-crew](https://github.com/ppfenning/coxswain-crew) | flag: `crew` | The agent seats that plan, build, review, and validate. |
| [HUD](hud.md) | [ppfenning/coxswain-hud](https://github.com/ppfenning/coxswain-hud) | flag: `hud` | A live view of in-flight runs and the docket. |
| [Desktop](desktop.md) | this repository's `desktop/` | flag: `desktop` | A desktop shell around the HUD. |
