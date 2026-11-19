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

- **`install`** (once per session): `cox` installed from this checkout, with `[postgres,traces,lake]`, into a venv of its own with `uv pip install`.
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
  by the forge's own `forge_forgejo.parse_branch_tip`, imported from this tree: one of two exceptions to the rule below
  that a smoke test does not import the code under test, so there is one reader of that answer, not two. The other is `test_unit_env_doctor.py`, which judges the doctor's rows by `doctor.failing_required`, the rule `cox upgrade` rolls back on.
- **`edge_machine`** (per test): an edge machine laid out as `cox setup install` lays a home out. Its checkout at
  `~/.local/share/coxswain/installs/coxswain` is this tree, cloned from the forge (`edge_origin`, once per module:
  this tree's tracked files as one commit, so an untracked local file never reaches it, plus one newer commit, both reported green). The checkout sits at the older commit, its
  venv is synced with `uv sync`, and `cox` is a uv tool editable from it, first on PATH. The profile says
  `upgrade_channel: edge`. The sandboxed uv uses the caller's uv cache and managed-interpreter folders
  (`plan.uv_env`), so a sync links what the caller already has instead of downloading it again. It **reads and
  writes** both: a sync adds the packages it fetched to the caller's cache, and when no Python 3.14 is there uv may
  install one into the caller's interpreter folder. Everything else of uv's stays in the sandbox, and a user
  `uv.toml` (under the real `~/.config/uv`) is not read, since HOME and XDG_CONFIG_HOME point into the sandbox.
- **`object_store`** (once per session): a single-node Garage container (`plan.GARAGE_IMAGE`, pinned by digest)
  with its S3 port on one loopback port only and its state on tmpfs. The fixture gives the node a layout, imports a
  key it chose (so no output it reads carries the secret), creates one bucket and grants the key on it. It is
  labelled and reaped like the Postgres container. The `install` carries the `traces` extra, so the doctor's bucket
  probe has pyarrow.

- **`launchable`** (per test): the sandbox set up so `cox route launch` can start the harness. It holds a team
  cartridge `smoke` over the tree's `base` (`plan.team_cartridge_text`), with the smoke skills bound to a plugin
  whose skill bodies are stubs (`plan.skill_plugin_files`) and a check that passes only once the scripted patch is
  applied. A copy of the tree's `src/graphs/shell.py` sits beside the install's venv, where a checkout's
  `src/graphs` holds it. The provider profile names the scripted runner and declares tool use, which the gate requires of a runner that edits files.
  `launchable.scripted(answers)` writes the answers the scripted runner replays, for a launch and for `--scripted` alike, and `launchable.run(argv, cwd)` runs a harness argv.

  `launchable.lifecycle(repo, run_id)` makes one scripted lifecycle run, for the tests that read what a run left.

## Harness tests

Every node of a graph is a model call. The release rehearsal and the forge tests avoid models by never running a
graph. These tests run graphs, offline. The provider profile names the harness's own scripted runner
(`runner: scripted`, with the answers file under `scripted:`), which replays canned answers by role through
`runner.scripted.ScriptedRunner`. It stands in for the model and nothing else, so `cox route launch epic` itself runs
end to end: cox resolves the profile, places the lane, checks the lane's interpreter and toolchain, builds the argv and
the child environment (`route.child_env`), and starts the harness detached. The team cartridge and skills resolve for
real, the patch is applied in a real worktree, and the cartridge's checks run there under the environment cox built.
The gate, the merge onto the phase branch, the push and the store records are all real.

The launch tests hold cox to the environment `route.child_env` builds. The `launchable` fixture puts a probe executable
in the target repository's own venv (`<repo>/.venv/bin`, ignored by git). The `lane-probe` check (`command -v` that
probe) passes only when that folder is on the check's PATH, and the `lane-env` check writes the trace dir and PATH the
harness was given, which the epic test reads: `<repo>/.venv/bin` then the harness bin, first, and
`AGENT_GRAPHS_TRACE_DIR` at `runs/ship-1-trace`. A `child_env` that drops PATH, or that skips both prefixes and the
trace dir, fails both launch tests (checked by making each change in the tree and running them). The store's run row
records `runner: scripted`, so a run whose approvals were canned answers never reads as a model's.

| Test | Entry point | What it proves |
| --- | --- | --- |
| `test_harness_launch.py` (epic) | `cox route launch epic` -> `shell.py epic` -> `harness.cli.main` -> `_run_graph` -> `run_epic` | The launch returns with its pid file and launch record, and the detached harness completes the one-task epic with nothing quarantined. `epic/ship/p1` carries the built file, here and on origin. No worktree stays registered, the task is `approved` by `epic-driver:ship-1` and the run is `ok`. The `cox runs land ... --gate full --apply` that the run names then puts the file on origin's main and marks the task done. |
| `test_harness_launch.py` (dead run) | `cox route launch epic` | With no answers file, the harness takes its lease and then cannot build its runner. The store records the run `error` with its end time, and the lease is free, so the next launch, with the answers written, runs `ship-2` to completion. |
| `test_harness_launch.py` (lifecycle) | `shell.py lifecycle --scripted ... --repo ... --result-out ...`, the generic `_run_graph` branch | The patch is applied in a worktree of the repository and both checks pass there. The gate records one gated proposal, the result file says so, the worktree is removed, and the store has the run `ok`. |

## Operator commands and the feed

| Test | Entry point | What it proves |
| --- | --- | --- |
| `test_operator_commands.py` (stats) | `cox stats ingest`, `cox stats series` | A scripted harness run's records load into `stats.db`, and `series` reports the run. A second ingest adds no row. |
| `test_operator_commands.py` (usage) | `cox usage assess` | With a `spend:` weekly ceiling of $10, the verdict is `go` with no spend. After one $12.50 node call is written through the store's own writer, it is `stop`, exit 4, at 125% of the ceiling. |
| `test_operator_commands.py` (courier) | `cox courier send`, `inbox`, `ack` | A reference that does not resolve is refused. A sent `coxswain://task/t1` is listed for its label only, lives in the store's bus and not in `courier.jsonl`, and is gone from the inbox once acked. An unknown id is refused. |
| `test_lake.py` | `cox lake sync`, `query`, `doctor` | The run in the store syncs into the Iceberg lake, `cox lake query` reads it back, and `cox lake doctor` reports the runs table's snapshot. |
| `test_dash_feed.py` | `cox dash --once`, `cox dash --feed` | The snapshot towpath reads is schema 1, with the run, the history, the queued initiative and its ready task, and the board. A second `--feed` prints what the first one publishes, computes nothing of its own while the first holds the lock, and takes over once the first exits. `--feed --once` is refused. |
| `test_contract_host_cli.py` | `cox host add`, `capacity`, `drain`, `activate`, `remove`, `login` | Each write reaches `harness.store_cli host`, and the row read back through the store's own `host list` matches. A login is merged into the host's versions and leaves its beat alone. A refusal prints the store's reason. |

**Bugs found while building these tests, each fixed in its own commit.** None showed in the unit tests, which replace
the store CLI with doubles or install every extra at once.

- `cox host remove` sent `host delete`, which the store CLI did not have, so every remove failed with "invalid
  choice". The store CLI now has `host delete`, which deletes only a draining host and takes no `--by`, since the
  hosts table keeps no record of a removed row.
- `cox host login` sent `--login-ok`, `--reason`, `--failures` and `--by`. The store CLI's `host login` takes one
  `--login-json`, so every login exited 2. Its unit test's `FakeStore` accepted the wrong argv.
- A store refusal reached the operator as `store_cli exit 3: {}`. `agent_tools.store_cli.runner` returned the
  refusal's bare stdout payload instead of the reason on stderr.
- The `lake` extra did not install pyarrow, so `cox lake sync` failed on the install that `cox lake` itself tells a
  person to make.

## Contract tests

Each contract test fails if its boundary drifts. The first three contract files were checked by putting a drift back
into a scratch copy of the tree and confirming that they fail.

| Test | Boundary | What it would have caught |
| --- | --- | --- |
| `test_contract_store_cli.py` | `cox` to `harness.store_cli set-state` | #1174: `--expect quarantined` and the target `blocked` were refused with "invalid choice". Also the target `quarantined` (below). |
| `test_contract_chair_quarantine.py` | the chair's revert of a phase to the store | A quarantine the store refuses and the chair drops, a task outside the reverted land moved, or a quarantine with no `main_red` cause on its row. |
| `test_contract_route_edit.py` | `cox route import` and `cox route edit` to the store | A move the store refuses, or a file and a store row that disagree. Its unit tests replace the store with a double. |
| `test_contract_runs_recover.py` | `cox runs recover` to git and the store | The git argv, the worktree add and remove, and the close. The repo is passed as `~/src/app`, so recover's own handling of a tilde path is covered. That is the kind of bug #572 fixed in the partial land (`cox runs land`). That path is not covered here. |
| `test_contract_store_api.py` | `cox runs pause` and `cox runs resume` to the store, in process through `harness.store_api` | A pause that never reaches the store (`cox runs move` once called it with no store at all), or an unknown run read as a failure instead of a refusal. |
| `test_doctor_running_install.py` | `cox setup doctor` to the interpreter running it | The `running install` row missing, or a plain install read as editable. |
| `test_contract_store_bounds.py` | an in-process store call to a locked row | A store call with no time bound: a held row lock must end the write as `Failed` within the lock bound. Also a storage URL with its own `options`, which once replaced the lock and statement bounds instead of adding to them. |
| `test_lander_unreadable_record.py` | `cox lander once`, `status` and `adopt` to a Postgres store holding one malformed landing record | One bad row stopping every lander pass in the store, a stall nobody is told of (or told of twice), or any reader writing into the bad row. |
| `test_chair_dry_run_read_only.py` | `cox chair run --once --dry-run` to a Postgres store, read-only for every session and then writable | A dry run that writes: its courier read once migrated the store (a `CREATE TABLE`, refused on a read-only store) and loaded `courier.jsonl` into the empty log. `schema_version` and every table's row count must not change. Both cases failed before the fix. |
| `test_chair_tick_cache.py` | the chair's store readers to Postgres's change counters | A quiet tick that reads the store again, or a write that re-reads readers of other tables (H6). |
| `test_chair_actions_window.py` | `cox chair run --dry-run` and the chair's wiring to a Postgres store holding 34,000 `chair_actions` rows | A quiet tick that reads the whole `chair_actions` window again, a row written behind the tail that the kept window misses, or an intake's decompose runs that differ when read from the tick's shared reads (H6). |
| `test_unit_env_doctor.py` | a hand-run `cox setup doctor` to the units' env file | The object_store keys held only in the file the upgrade unit names (written by `cox upgrade --install-timer --environment-file`) and missing from the shell: the required rows fail and a good `cox upgrade` rolls back. Also a file value overriding one the shell already sets (the shell must win). Each test failed with the doctor's loading taken out. |
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
| `test_edge_upgrade.py` | `cox upgrade` on an edge machine | H5: with no token file (an advisory doctor row), the move to the newer green commit stands, prints the advisory warning, and is recorded `upgraded`. With the store on a closed port (a required row), the move rolls back to the older commit, the record says `rolled_back`, and the next run holds that commit instead of retrying it. The git, `uv sync`, tool reinstall and doctor all run for real. With `FORGEJO_BASE_URL` and `FORGEJO_TOKEN` only in `~/.config/agent-tools/garage.env` and not in the shell, a hand-run `cox upgrade` loads them and upgrades. With the load in `cli_install._upgrade` taken out, it refuses, because its CI query falls back to `gh`. |

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
- **B, recorded GitHub answers replayed to the real `gh` (built: `test_contract_gh.py`).** This was first rejected
  for three reasons. Two of them do not hold. `gh` needs no certificate for the replay: for its local-development host
  `github.localhost` it speaks plain http to `api.github.localhost`, and Go's client sends that through `HTTP_PROXY`. `HTTPS_PROXY` names the replay too, so nothing gh asks of any host reaches the network. gh's own background calls are switched off by its documented variables: `GH_TELEMETRY=0` and `DO_NOT_TRACK=1` (its telemetry to cafe.github.com once failed a test on CI as a CONNECT miss), and the update and extension-update checks. A stray request is still a miss; no host is allowed.
  The recordings carry no token either: the recorder adds its own on the way out and never writes one down, and it
  replaces every git identity (an object with a `name` and an `email`) with the sandbox's own. The third reason holds:
  a replay returns what GitHub said on the day it was recorded, so it cannot notice GitHub's API changing. What it
  does notice is a change on either side of the process boundary, which is what H1.4's contract tests are for. `cox`
  might build an argv that `gh` refuses, or ask `gh` something new. A `gh` release might rename a `--json` field,
  change an exit code, or ask the API something the recording lacks. Any of these fails the test: a request with no
  recorded answer gets a 599 and is listed. The tests run in every CI run, offline, in about a second each.
- **C, a sandbox repository on GitHub (not built).** The only option that proves the API as it is today. It needs
  the network, a secret and a repository the token may write to, which nobody has provisioned. It is slow and rate
  limited, and fork pull requests cannot run it. Recording mode covers its read half against the real service on
  demand: `COX_SMOKE_GH_RECORD=1 COX_SMOKE_GH_TOKEN=<token> pytest -q smoke/test_contract_gh.py` runs the same tests
  against api.github.com and rewrites their recordings. Its write half, a merge that really merges, still waits for a
  sandbox repository.

**What the recordings are.** One JSON file per test under `smoke/gh_recordings/`, each answer filed by method and
path (query parameters sorted), or for GraphQL by the operation's kind, name and variables. The recorder forwards
only reads: GET, and GraphQL queries. It refuses every write with a 599, with one exception: a branch deletion, which
it forwards only after a GET has shown the branch is already gone, so GitHub answers 422 and nothing changes. The
subject is this repository, which is public. Pull request #1061 is merged and carries one task, and its head commit
and the `ci.yml` push runs on main are recorded too. `gh_recordings/*.json` names the `gh` version that recorded it.

| Test | Boundary | `gh` command |
| --- | --- | --- |
| `test_pr_view_reads_every_field_a_land_needs` | `forge_github.read_pr`, `pr_land.carried_tasks` | `gh pr view --json number,state,url,headRefName,headRefOid,baseRefName,body` |
| `test_cox_pr_land_of_a_merged_pr_marks_the_task_it_carries_done` | `cox pr land` | `gh pr view`, then the store's mark-done |
| `test_check_rows_of_a_commit_read_its_check_runs_and_statuses` | `forge_github.pr_check_rows` | `gh api --paginate .../check-runs` and `.../status` |
| `test_required_checks_of_main_are_read` | `forge_github.read_required_for_base`: exactly `["test"]`, the recorded ruleset's one required check | `gh api .../rules/branches/main` |
| `test_run_list_finds_the_green_ci_push_runs_on_main` | `cli_install._edge_run`, `forge_github.ci_runs_argv` and `parse_green_shas`, as `cox upgrade` on the edge channel runs them | `gh run list --workflow ci.yml --branch main --event push --json headSha,status,conclusion` |
| `test_pr_checks_watch_of_a_finished_pr_lists_no_failing_check` | `chair_revert_ports.GhForgePort.wait_checks`, the chair's revert path | `gh pr checks <n> --watch` |
| `test_pr_merge_of_a_merged_pr_reads_as_merged` | `forge_github.merge_pr` | `gh pr merge <n> --squash --delete-branch` on a pull request that is already merged |

## Known fakes

Each fake below stands in for a boundary that no real test covers yet. Each one names the test that will retire it.

- `tests/devtools/gh_stub.py` is a `gh` executable put on PATH by `tests/devtools/test_release_rerun_e2e.py`. The
  release rerun test runs the real `python -m devtools release` against real git repositories with bare origins. The
  stub stores pull requests and releases in a JSON file and merges into the bare origin with real git. It cannot
  notice a change in `gh`'s argv or JSON. It is retired by contract tests of those commands, recorded as
  `test_contract_gh.py`'s are (option B above), or by option C. Such tests would cover each `gh` command the release uses: `pr create`, `pr list`,
  `pr checks`, `pr merge`, `release create`, `release edit`, `release view`, `run list`, `run view`, `workflow run`,
  and the `api` read of the branch protection's required checks.
