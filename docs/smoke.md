# Smoke suite

The smoke suite runs real `cox` commands, the way a person or a unit runs them, against a real git repository, a
real Postgres and a sandbox forge (a Forgejo container). Nothing in it is mocked, stubbed or faked. It is the second half of
[H10](hardening.md#h10-functional-core-tests-that-prove-it-runs): unit tests cover the pure core, and the smoke suite
proves the glue that calls it. It also holds H1's contract tests, one per process boundary that has broken.

Run it from the repository root:

```sh
pytest -q -rs smoke
```

It needs `uv` and either `docker` or a Postgres you point it at. The forge tests (marked `forge`) also need `docker`.
Without them it skips and says why. On the development machine, with a warm uv cache, `-m "not forge"` took 60 s
(most of it the install and the Postgres container) and `-m forge` took 96 s (most of it the two edge upgrades). One
run of both together took 3 min 13 s while other work loaded the machine.

## What the harness provides

The fixtures live in `smoke/conftest.py`. Every decision they make (which Postgres to use, the `docker` argv, the
sandbox environment, the profile text, whether the install is this tree) is a pure function in `smoke/plan.py`, tested
without mocks in `tests/test_smoke_plan.py`.

- **`install`** (once per session): `cox` installed from this checkout into a venv of its own with `uv pip install`.
  The install is not editable. Before any test runs, the fixture compares every installed module, and every file
  `[tool.setuptools.package-data]` lists, with the tree. A stale build or another checkout therefore cannot stand in
  for this one (#1184, #1185). The venv sits at `<install>/.venv`. A profile whose `harness_dir` is `<install>`
  therefore makes every harness child that `cox` starts run the same install.
- **`postgres`** (once per session): the server named by `COX_SMOKE_POSTGRES_URL`, or else a container started with
  `docker` on a random loopback port and removed at the end of the session. The image is `postgres:16` pinned by
  digest (`plan.POSTGRES_IMAGE`). CI's service uses the same digest. The suite takes a URL only when it names one
  loopback host (`localhost`, `127.0.0.1` or `::1`). It refuses a URL that lists several hosts (`a:5432,b:5432`), and
  one whose query sets `host`, `hostaddr` or `service`, since libpq would connect wherever those say. A URL it
  refuses fails the run, whether or not the run is required.
- **`store_url`** (per test): a fresh database on that server. It is created, and opened once by `harness.store_cli`
  so the schema migrations apply, under the install's own sandboxed environment. The caller's `PYTHONPATH`, `PG*`
  variables and HOME do not reach it. It is dropped after the test.
- **`sandbox`** (per test): a temporary home with a routing profile at `~/.config/agent-tools/profile.yaml`, a
  provider profile naming the test's store, a workspace with `work/` and `runs/`, and a git config. Every `cox` runs
  with HOME, the XDG roots and git's global config inside the sandbox, and with the install's `bin` first on PATH.
  Nothing else comes from the caller except the locale. `sandbox.cox(...)` runs the installed `cox`.
  `sandbox.harness(module, ...)` runs `python -m <module>` under the install's interpreter, the same interpreter
  `cox` starts. `sandbox.store_rows()` reads the store back through `harness.store_queue`.
- **`repo`** (per test): a clone at `~/src/app` of a bare origin in the test's temporary directory, with one
  commit on `main` pushed.
- **`forge`** (once per session): a Forgejo container (`plan.FORGEJO_IMAGE`, Forgejo 11 from Forgejo's own registry,
  data.forgejo.org, pinned by digest; the pull is retried up to three times) published on one loopback port only, with sqlite on tmpfs, no ssh, no sign-up and Actions off. The fixture creates one admin
  user and a token with `forgejo admin` inside the container. It is labelled and reaped like the Postgres container.
- **`forge_sandbox`** (per test): the sandbox with `forge: auto` in its profile and `FORGEJO_BASE_URL` and
  `FORGEJO_TOKEN` in its environment, so `cox` lands a repository whose origin is on the forge through
  `forge_forgejo`, chosen by `forge_auto` as on a person's machine.
- **`forge_repo`** (per test): a new repository on the forge with one commit on `main`, cloned at `~/src/app` with
  the forge as its http origin. `forge_repo.report(sha, state)` reports a commit status, as a CI job would.
  `forge_repo.pull(n)` and `forge_repo.branch_tip(branch)` read back what the forge holds. A branch answer is read
  by the forge's own `forge_forgejo.parse_branch_tip`, imported from this tree: the one exception to the rule below
  that a smoke test does not import the code under test, so there is one reader of that answer, not two.
- **`edge_machine`** (per test): an edge machine laid out as `cox setup install` lays a home out. Its checkout at
  `~/.local/share/coxswain/installs/coxswain` is this tree, cloned from the forge (`edge_origin`, once per module:
  this tree's tracked files as one commit, so an untracked local file never reaches it, plus one newer commit, both reported green). The checkout sits at the older commit, its
  venv is synced with `uv sync`, and `cox` is a uv tool editable from it, first on PATH. The profile says
  `upgrade_channel: edge`. The sandboxed uv uses the caller's uv cache and managed-interpreter folders
  (`plan.uv_env`), so a sync links what the caller already has instead of downloading it again. It **reads and
  writes** both: a sync adds the packages it fetched to the caller's cache, and when no Python 3.14 is there uv may
  install one into the caller's interpreter folder. Everything else of uv's stays in the sandbox, and a user
  `uv.toml` (under the real `~/.config/uv`) is not read, since HOME and XDG_CONFIG_HOME point into the sandbox.

## Contract tests

Each contract test fails if its boundary drifts. The first three contract files were checked by putting a drift back
into a scratch copy of the tree and confirming that they fail.

| Test | Boundary | What it would have caught |
| --- | --- | --- |
| `test_contract_store_cli.py` | `cox` to `harness.store_cli set-state` | #1174: `--expect quarantined` and the target `blocked` were refused with "invalid choice". Also the target `quarantined` (below). |
| `test_contract_chair_quarantine.py` | the chair's revert of a phase to the store | A quarantine the store refuses and the chair drops, a task outside the reverted land moved, or a quarantine with no `main_red` cause on its row. |
| `test_contract_route_edit.py` | `cox route import` and `cox route edit` to the store | A move the store refuses, or a file and a store row that disagree. Its unit tests replace the store with a double. |
| `test_contract_runs_recover.py` | `cox runs recover` to git and the store | The git argv, the worktree add and remove, and the close. The repo is passed as `~/src/app`, so recover's own handling of a tilde path is covered. That is the kind of bug #572 fixed in the partial land (`cox runs land`). That path is not covered here. |
| `test_sandbox.py` | the harness itself | A child interpreter importing another checkout. It also checks that `cox` resolves the profile the sandbox HOME names, and that every path `cox route context` reports is under the sandbox or the install. It does not prove that `cox` opens no other file; the environment the sandbox builds is what keeps the real home out of reach. |

**A drift found while building this suite, now closed.** The chair's revert of a phase quarantines its tasks with
`set-state <initiative> <task> quarantined` (`cli._quarantine_phase_with`). The store CLI refused that target with
"invalid choice", and the caller ignored the exit, so the store never recorded the quarantine. The store CLI now takes
its choices from `core.workstore.STATES`, and a unit test holds the parser's choices equal to it. The chair quarantines
only the approved and done tasks of the reverted land, each by a compare-and-set on the state it read, and marks each
row with the `main_red` cause. A write the store refuses is named in the chair's needs_chair. A new drift goes into
`KNOWN_DRIFT` in `test_contract_store_cli.py` as a strict xfail; whoever fixes it deletes the entry.

## Forge and edge tests

These are marked `forge`. Each one fails when what it proves stops being true. I checked the edge tests by putting a
drift back into the tree and running them: with every doctor row required, the advisory test fails; with the required
rows dropped from the verdict, the rollback test fails.

| Test | Entry point | What it proves |
| --- | --- | --- |
| `test_forge_land.py` | `cox runs land --task t1 --gate full --apply` | On green checks, the land pushes `pr/t1`, opens the pull request, waits for the commit status the test reports, squash-merges and deletes the branch, and the local `main` follows. On failing checks it stops at `wait_checks`, names the failing context, and leaves the pull request open and `main` alone. |
| `test_edge_upgrade.py` | `cox upgrade` on an edge machine | H5: with no token file (an advisory doctor row), the move to the newer green commit stands, prints the advisory warning, and is recorded `upgraded`. With the store on a closed port (a required row), the move rolls back to the older commit, the record says `rolled_back`, and the next run holds that commit instead of retrying it. The git, `uv sync`, tool reinstall and doctor all run for real. |

`cox upgrade` asks the forge which commits passed CI. For an origin on the `FORGEJO_BASE_URL` host it reads Forgejo's
commit statuses (`forge_forgejo.green_commits`). For any other origin it asks `gh run list` for the `ci.yml` push
runs, as before. An edge machine whose checkout comes from Forgejo needs `FORGEJO_BASE_URL` and `FORGEJO_TOKEN` in the
unit environment file.

**Bugs found while building these tests.** Neither showed against the fake server the unit tests use.

- Forgejo records a pushed branch from a queue after `git push` returns. A pull request opened at once was refused
  with "The target couldn't be found", so every land through Forgejo stopped at `pr_create`. `forge_forgejo.push` now
  waits, up to 30 s, until the server shows the branch at the pushed commit.
- A commit status row names its state `status`; only the combined rollup says `state`. `forge_forgejo` read the rows
  by `state`, so a failed check was reported as "failing checks: combined status" instead of by its context.

macOS launchd stays an operator check: the suite runs on Linux and starts no launchd agent.

## Where it runs

- **CI.** The required `test` job has a Postgres service (the pinned image) and two steps: `smoke` runs
  `-m "not forge"` and `smoke (forge)` runs `-m forge`, which starts the Forgejo container with the runner's docker.
  Both set `COX_SMOKE_POSTGRES_URL` to the service and `COX_SMOKE_REQUIRED=1`, so a missing `uv`, Postgres or docker
  fails the job instead of skipping it.
- **Lane checks.** `.agent-checks` runs `pytest -q -rs smoke` after the component suites. A lane host with docker
  runs it against its own container. A host with no docker on PATH skips it and prints the reason. A host whose
  `docker` is on PATH but whose daemon does not answer fails, because that is a host fault a skip would hide. Set
  `COX_SMOKE_SKIP_BROKEN_DOCKER=1` on such a host to skip there instead.

Every container carries the label `coxswain-smoke=1`. It also carries its session's id (`coxswain-smoke.session`,
printed in the pytest header as `smoke session: <id>`) and its start time (`coxswain-smoke.started`, epoch
seconds). A new session first removes smoke containers more than two hours old, which a killed session left behind.
It never touches a younger container, so it cannot remove one that another live session is using. To remove one
session's container by hand:

```sh
docker rm -f $(docker ps -aq --filter label=coxswain-smoke.session=<id>)
```

## Adding a smoke test

A smoke test drives an entry point (`cox <verb>`, a unit's command line) and checks what a person would see: the exit
code, the output, the files, the git refs and the store rows. It does not import the code under test, and it does not
patch anything. When a test needs a fact from the code, such as an enum, it asks the installed interpreter through
`sandbox.python_json`, so the fact comes from the same install the commands run.

## The sandbox forge, and what it leaves to GitHub

`cox` has three forges, chosen per repository by its `origin` (`forge_auto`): `forge_github` shells out to `gh`,
`forge_forgejo` calls Forgejo's REST API, and `forge_local` uses git alone. Three ways to test the forge calls were
weighed.

- **A, a Forgejo container (built).** A real server with real pull request, status and merge behaviour. It runs
  offline, is hermetic, and starts as cheaply as Postgres does. It drives `forge_forgejo` end to end. It cannot prove
  the `gh` argv or the JSON `cox` parses from `gh`: Forgejo serves neither GitHub's REST v3 nor its GraphQL, and its
  merge and check semantics differ from GitHub's in detail.
- **B, a recorded HTTP replay (rejected).** It would run the real `gh` binary, but a replay returns what GitHub said
  on the day of the recording, so it cannot notice that GitHub or `gh` changed, which is the drift a contract test
  exists to catch. Recordings carry tokens to scrub, each new call needs a new recording, and `gh` would need a
  certificate it trusts for the replay host.
- **C, a sandbox repository on GitHub (designed, not built).** The only option that proves the `gh` boundary against
  the real service. It needs the network and a secret, it is slow and rate limited, and fork pull requests cannot run
  it.

A forge-neutral flow is tested once, through Forgejo. What is specific to `gh` is argv building and output parsing,
which sit in pure functions that unit tests cover. **C** covers the rest, as follows, when it is built:

- A throwaway repository the project owns, with a fine-grained token scoped to it alone (contents, pull requests,
  statuses, actions read), held as a repository secret.
- A workflow on a weekly schedule, on `workflow_dispatch`, and as a step of the release cut. Never on pull requests:
  a fork cannot read the secret, and a forge outage must not block a land.
- Tests marked `github`, skipped without the secret, each one an entry point as the forge tests here are: a land
  through `forge_github` (`gh pr create`, `gh pr checks`, `gh pr merge --squash --delete-branch`), `merge_state` and
  `update_branch` on a pull request behind its base, and `cox upgrade` on an edge machine whose checkout comes from
  the sandbox repository, which runs `gh run list` (`forge_github.ci_runs_argv` and `parse_green_shas`) against real
  workflow runs.
- The test repository is reset to one commit on `main` at the start of each run, and every branch and pull request a
  run creates carries the run id, so a killed run's leftovers are found and closed by the next.

## Known fakes

Each fake below stands in for a boundary that no real test covers yet. Each one names the test that will retire it.

- `tests/devtools/gh_stub.py` is a `gh` executable put on PATH by `tests/devtools/test_release_rerun_e2e.py`. The
  release rerun test runs the real `python -m devtools release` against real git repositories with bare origins. The
  stub stores pull requests and releases in a JSON file and merges into the bare origin with real git. It cannot
  notice a change in `gh`'s argv or JSON. It is retired by the scheduled contract tests against a sandbox GitHub
  repository (option C above). Those tests cover each `gh` command the release uses: `pr create`, `pr list`,
  `pr checks`, `pr merge`, `release create`, `release edit`, `release view`, `run list`, `run view`, `workflow run`,
  and the `api` read of the branch protection's required checks.
