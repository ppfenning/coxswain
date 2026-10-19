# Getting started: from one checkout to a verified first run

Coxswain is one repository, one package and one profile file. This is the
order a person on a new machine follows, top to bottom, to a first verified
run. `<home>` is the coxswain home, `~/.local/share/coxswain` by default.

## 1. What you are installing

One checkout of [ppfenning/coxswain](https://github.com/ppfenning/coxswain)
at `<home>/installs/coxswain`, with one venv inside it. The folders that
matter here:

- `src/tools`: the `cox` CLI. Its `route` group files work and launches the
  harness.
- `src/graphs`: the harness that runs a graph (`harness_dir` in the profile).
- `src/cartridges`: the shipped cartridges, the skill bodies a run reads,
  the default provider profiles, and a local plugin marketplace for Claude
  Code.
- `crew`: the seats, linked into `~/.claude/agents`.

The `coxswain` package puts two commands on `PATH`: `cox` and `cartridge`.

## 2. Install

```bash
uv tool install --python 3.14 coxswain        # or: brew install ppfenning/coxswain/cox
git clone --branch <tag> https://github.com/ppfenning/coxswain <home>/installs/coxswain
cox install --team <team>
```

`<tag>` is a release tag, such as `v2026.10.0`. `cox install` syncs the
checkout's venv (`uv sync --frozen --all-extras`), installs `cox` editable
from the checkout, writes the profile in section 4 when there is none,
links the crew and runs the doctor. Add the extras this machine needs to
the first command, for example `'coxswain[postgres]'` for a shared store.
`cox install --dry-run` prints the plan first.

## 3. The two interactive logins

Two CLIs need a one-time interactive login before a run can use them
headless:

- The provider CLI named by the profile's `command:` line. For the shipped
  Claude Code provider profile, that's `claude`. Run it once interactively
  and log in, because the harness runs it headless later and cannot answer a
  login prompt.
- `gh auth login`, when the profile's `forge` is `github` or `auto`. The
  harness itself never pushes and never opens a pull request: a finished
  run leaves branches in the target repository. `cox runs land` opens the
  pull request and merges it through `gh`, so `gh` needs to be logged in.

## 4. The profile

The route group reads one profile, `~/.config/agent-tools/profile.yaml`.
`cox install` writes it with these keys:

```yaml
team: <team>
cartridges_dir: <workspace>/cartridges
skills_roots: [<home>/installs/coxswain/src/cartridges/skills-plugins]
provider_profile: <home>/installs/coxswain/src/cartridges/providers/claude-code.yaml
harness_dir: <home>/installs/coxswain/src/graphs
workspace_dir: <workspace>
assume: a
umbrella_dir: <home>/installs/coxswain
```

Add `forge:` and `tracker:` lines when you need them. `<workspace>` is
`<home>/workspaces/main` unless you passed `--workspace`.

`cartridges_dir` is where the TEAM's cartridge lives (the workspace's
`cartridges/`, see section 5), not the checkout's `src/cartridges`.
`skills_roots` and `harness_dir` point into the checkout.

`provider_profile` starts at the shipped default. A provider profile is team
configuration: tiers, budgets, tools and the store. A team keeps its own
copy outside the checkout, in `<home>/providers/` or in the workspace, with
`catalog.yaml` beside it, and points `provider_profile` there:

```bash
mkdir -p <home>/providers
cp <home>/installs/coxswain/src/cartridges/providers/{claude-code,catalog}.yaml <home>/providers/
```

The doctor's `home paths` row fails when `provider_profile` is outside both
`<home>` and the workspace. `assume` is
the gate answer detached runs start with (`a` approves what the gate would
ask, `r` refuses); it defaults to `a` when the key is absent and is passed
through unchanged to the harness. `forge` is how `cox runs land` lands a
task: `local` (the default when the key is absent) needs only git: the task's
commit fast-forwards the default branch once the land's own checks pass, and is
pushed when the repository has an `origin`. `github` opens a pull request,
waits for its checks and squash-merges it through `gh`. `forgejo` does the
same on a Forgejo server (see [forgejo-forge.md](forgejo-forge.md)). `auto`
picks per repository from its `origin`: github.com lands through `github`,
the `FORGEJO_BASE_URL` host through `forgejo`, anything else through
`local`. `cox setup doctor` checks `gh` authentication only for `github` and
`auto`. `tracker` is where `cox route
sync` mirrors the work store: `none` (the default when the key is absent)
mirrors nothing, and `github-projects` syncs a GitHub Project through `gh`. A
run can pause sync by writing `runs/policy.tracker.json`, which wins over the
profile.

`--profile PATH` overrides the profile location for one command;
`AGENT_TOOLS_PROFILE` overrides it for a shell.

## 5. The workspace

`workspace_dir` is a git repository with:

- `work/`: initiatives and their phases, one file per task.
- `runs/`: the pidfile, log, `<run-id>-trace` directory, and usage and
  manifest files a run leaves behind.
- `intake/`: tickets filed but not yet scoped onto a board.
- `cartridges/<team>/`: this team's cartridge, extending `local`, beside
  symlinks to `base` and `local` in the checkout's
  `src/cartridges/cartridges/`.

`cox install` creates the default workspace's folders. Make it a git
repository, then create the team cartridge with `cartridge init`, which
also prints the two profile lines for section 4:

```bash
mkdir -p <workspace>/{work,runs,intake,cartridges}
git -C <workspace> init
cartridge init <team> --cartridges-dir <workspace>/cartridges
```

See [`src/cartridges/README.md`](../../cartridges/README.md) for what a
cartridge is; it is not repeated here.

## 6. The skills as a plugin (optional but recommended)

Provider-specific, and only for the Claude Code provider. Either rerun
setup with `--plugins`:

```bash
cox setup install --team <team> --plugins
```

or register the marketplace by hand:

```bash
claude plugin marketplace add <home>/installs/coxswain/src/cartridges
claude plugin install local-skills@coxswain-cartridges
```

## 7. Verify

```bash
cox setup doctor
```

is read-only: it checks profile, paths, harness venv, cartridge, skills,
provider, run store, and workspace, prints a table, and exits 0 when the layer is
ready to launch a run. `--profile PATH` and `--json` both work here too.

On a fresh install the run store does not exist yet, because the first run
creates it. The `store` row then reads `no store yet (it is created by the
first run)` and fails, so doctor exits 1 until you have launched once in
section 8. That is the one failure to expect at this step; fix any other
failing row first, then rerun doctor after the first run and the `store` row
reads `sqlite, N runs`. A store with no `runs` table yet reads the same way
and clears on the first run.

## 8. A first run

Run these from inside `workspace_dir`. `route launch epic --initiative`
resolves its argument against the current directory, and `runs usage` and
`runs series` default `--runs-dir` to the relative path `runs`.

```bash
cd <workspace>
cox route file --repo <path> --title "..." --body FILE
cox route launch epic --initiative work/<slug> --fix-attempts 2
cox route status
cox runs usage <run-id>
cox runs series
```

`route file` prints the path it wrote for `<slug>`; use that printed value,
since `--title` is slugified (lower-cased, non-alphanumerics collapsed to
`-`, truncated to 48 characters) and will not match the title text
verbatim.

A finished run leaves behind its phase branches and a scratch worktree.
Under `runs/` it also leaves a pidfile, a log, a `<run-id>-trace` directory,
and the usage and manifest files that `runs usage` and `runs series` read
back.

## 9. Edit your cartridge

`cox setup` opens a menu; choose `edit cartridge`. It needs `root` (the
installs folder, `<home>/installs`), `team`, and `workspace` filled in first, because the probe that reads your
cartridge builds its interpreter path from `root`.

The screen lists one row per editable key of your RESOLVED cartridge: its
value, and which layer it came from (`base`, `local`, your team, or
`edited`, once you have written something). A row you cannot edit is
marked read-only and says which layer owns it instead.

```
  policy.review_tier = 2 [team]
* crew.reviewer.enabled = true [edited]
  crew.reviewer.skills = review_charter (read-only: base)
```

Keys: `j`/`k` move between editable rows; `space` cycles a choice row
through its values; `e` opens a text row for typing, `ENTER` commits the
typed text, `ESC` cancels the edit (while editing, `q` is an ordinary
character, not the quit key); `u` reverts the row under the cursor to its
last-probed value; `w` writes; `r` re-probes without writing; `q` quits.

`w` writes ONLY `<team>/cartridge.d/edited.yaml`, merging your pending
edits with whatever fragment was already there, then re-probes so the
rows on screen reflect the loader's own view, not your guess at it. A
refused write keeps your pending edits and reports the loader's error on
the section the write touched. Writes are tighten-only: loosening a risk
or a ramp the base cartridge already declared is refused before anything
touches disk.

Starting from no team cartridge at all? Run `cartridge init <team>` first
(section 5).

## Running lanes on a second machine

A second machine can run lanes against the same run store as the first.
In short:

1. Install as in section 2, with the `postgres` extra:
   `uv tool install --python 3.14 'coxswain[postgres]'`.
2. Use the team's provider profile (section 4), with `storage_url` set to
   the shared Postgres. Without it each machine writes its own local SQLite
   store.
3. Clone each repository your work targets at the same absolute path as on
   the first machine.
4. Run `cox setup doctor` and check the `store` row, for example
   `postgresql, 12 runs`. The row never shows the URL or its credentials.
   Then run `cox host beat <name>` there: the beat records the host's
   workspace, which a launch on it needs.
5. On the chair, add the host to the hosts table and check it over ssh:

    ```bash
    cox host add <name> --ssh <ssh destination> --capacity 2
    cox host doctor <name>
    ```

6. Launch on it with `--on`, then fetch and land on the chair:

    ```bash
    cox route launch epic --initiative work/<id> --on <name>
    cox runs fetch <run>
    cox runs land <run> --repo <path>
    ```

The store lease allows one live run per epic across machines.
[Several machines](https://ppfenning.github.io/coxswain/latest/customize/several-machines/)
covers lane hosts, shared traces and landing from any machine in full.
