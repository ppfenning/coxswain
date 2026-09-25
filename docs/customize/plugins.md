# Plugins

Coxswain's core repositories name no vendor. Anything that talks to a
particular tracker, forge, model vendor or hosted service is a plugin:
a Python package that registers under an entry-point group, which core
looks up by name. The public
[coxswain-plugins](https://github.com/ppfenning/coxswain-plugins)
repository holds the ones Coxswain ships.

## The groups

| Group | Loaded by | Chosen by | Built in |
| --- | --- | --- | --- |
| `coxswain.sources` | `cox route pull` | `--source <name>`, configured under the profile's `sources.<name>` | none |
| `coxswain.forges` | `cox runs land` | the profile's `forge:` key | `local` (plain git, the default) and `github` |
| `coxswain.trackers` | `cox route sync` | the profile's `tracker:` key | `none` (the default) and `github-projects` |
| `coxswain.system_one` | the harness's fast path | the provider profile's `system_one.backend` | `model-tier` |
| `coxswain.runners` | the harness | the provider profile's `runner:` key | `claude-code` and `anthropic` |

A name that is neither built in nor registered is refused with a line
naming the group, except in system one: an unregistered backend turns
system one off for that run, and the run goes on without it.

## Installing

`cox` loads sources, forges and trackers from its own environment:

```sh
uv tool install coxswain-tools --with "coxswain-plugins @ git+https://github.com/ppfenning/coxswain-plugins"
```

The harness loads system-one backends and runners from its own virtual
environment, the `harness_dir` in your profile:

```sh
uv pip install --python <harness_dir>/.venv/bin/python "coxswain-plugins[jev] @ git+https://github.com/ppfenning/coxswain-plugins"
```

A plugin that needs a vendor's client library declares it as an extra,
as `jev` does. Installing one plugin never pulls in another's
dependencies. `cox setup doctor` lists what each side can load in its
`plugins` row.

## Writing one

Register the entry point in your package's `pyproject.toml`:

```toml
[project.entry-points."coxswain.forges"]
myforge = "my_package.forge"
```

Each group expects a different shape:

- **Source** (a module): `candidates(config, listing) -> tuple[Ref, ...]`,
  `read(raw) -> Candidate`, `taken(link, intake_links) -> bool` and
  `mark_argv(ref, intake_path) -> list[str]`. The profile's
  `sources.<name>` block gives `repos`, `filter` and `token_env`. An
  adapter reads a tracker and files intake tickets. It never moves the
  tracker's state.
- **Forge** (a module): `find_open_prs(repo, branch)`, `push(repo,
  branch)`, `open_pr(repo, title, body, *, head=None, base=None)`,
  `wait_checks(repo, timeout_s, *, ref="HEAD")` and `merge(repo, step)`.
  Each except the first returns `(ok, detail)`. `merge` updates the local
  default branch without checking anything out.
- **Tracker**: resolves by name today, but `route sync` still runs only
  the built-in GitHub Projects sync.
- **System-one backend** (a callable): `make(model, api_key, block)`
  returns a decider whose `decide(question, state)` returns an answer
  with a confidence. Set `make.needs_key = True` for a hosted service. Its
  key then comes only from the environment variable named in
  `system_one.key_env`, never from the provider's own key. Set
  `make.wants_runner = True` to be handed the run's runner as `runner=`.
  A backend runs in shadow until `cox stats system-one` says it has
  graduated.
- **Runner** (a callable): `factory(profile, *, role_skills, workdir,
  repo)` returns a runner whose `run(*, role, tier, schema, prompt, ...)`
  returns the node's structured result.
