<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/shell-banner-dark.svg">
    <img alt="A racing eight seen from above, the coxswain calling the stroke through a megaphone" src="docs/assets/shell-banner-light.svg" width="720">
  </picture>
</p>

<h1 align="center">Coxswain</h1>

<p align="center"><em>Agents pull the oars. You hold the tiller.</em></p>

<p align="center">
  <a href="https://ppfenning.github.io/coxswain/latest/">Docs</a> ·
  <a href="https://ppfenning.github.io/coxswain/latest/releases/">Releases</a> ·
  <a href="https://pypi.org/project/coxswain-tools/">PyPI</a>
</p>

---

Coxswain is a platform that builds software with a crew of AI agents and keeps a person on the tiller. You describe a change; it is filed as a work item, planned, built in a worktree under a dollar budget, reviewed by two independent reviewers who are made to disagree, arbitrated, checked against the project's own tests as evidence, and handed to you as a pull request. You merge. Every step leaves a record you can read back.

**Status: beta.** The loop runs itself daily on its own repositories. Interfaces are still moving; see the [release notes](https://ppfenning.github.io/coxswain/latest/releases/) for what each version changes.

## Install

```sh
uv tool install coxswain-tools
cox setup doctor
```

`cox setup doctor` tells you whether this machine can run an epic and what is missing. `cox install` sets up every component from the manifest in this repository; `cox setup` is a screen over the same steps. The [install guide](https://ppfenning.github.io/coxswain/latest/install/) covers a fresh machine end to end.

## The loop, in one screen

```
cox route file  --repo R --title T --body FILE   # a work item, from a sentence or a file
cox route launch epic --initiative work/<id>     # plan → build → review ×2 → arbitrate → check → validate
cox runs top                                     # every run in flight, live
cox runs land <run> --repo R --apply             # rebase, PR, merge on green, clean up
```

A run is a detached process. It plans one task at a time, builds it in a worktree it owns, runs the real tests, and asks two reviewers — one holding the change to the team's written charter, one whose job is to find what is wrong — before an arbiter decides. A task that fails its budget is split, never given more money. Nothing merges to a default branch without a person.

## What is in the box

This repository is the umbrella: the docs site, the version manifest that pins every component in lockstep, and the installer. The components are their own repositories, tagged together at every release.

| Component | What it owns |
|---|---|
| [`coxswain-cartridges`](https://github.com/ppfenning/coxswain-cartridges) | Who a run works for: roles, skills, budgets, policy, the tighten-only layers a team writes over the base. |
| [`coxswain-graphs`](https://github.com/ppfenning/coxswain-graphs) | What runs: the graphs as pure functions, and the harness that budgets, reviews, checks and records them. |
| [`coxswain-tools`](https://github.com/ppfenning/coxswain-tools) | The `cox` command: install, doctor, route, runs, land, the setup and editor screens. |
| [`coxswain-crew`](https://github.com/ppfenning/coxswain-crew) | The seats: who speaks, with what authority, in what voice. |

The manifest is [`manifest.toml`](manifest.toml). `cox versions` reports every installed component against it.

## Make it yours

Everything a stranger needs is a command in a public repository; everything that makes it yours is a file the command reads. A **cartridge** is that file for your team — which skills each role carries, what budget a build gets, what a reviewer holds code to. A **profile** is that file for your machine. Neither is code. `cartridge init <team>` writes one that inherits everything and lets you tighten what you care about; the [customize guide](https://ppfenning.github.io/coxswain/latest/customize/) walks through it.

## Contributing

Every change to any component lands as a pull request, most of them built by the platform itself. Ruff runs in CI and pre-commit on every repository with one shared rule set; `pre-commit install` mirrors it locally. Ideas go in as intake items and are decomposed before they are built.

## License

MIT. See [LICENSE](LICENSE).
