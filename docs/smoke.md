# Smoke suite

The smoke suite runs real `cox` commands, the way a person or a unit runs them, against a real git repository and a
real Postgres. Nothing in it is mocked, stubbed or faked. It is the second half of
[H10](hardening.md#h10-functional-core-tests-that-prove-it-runs): unit tests cover the pure core, and the smoke suite
proves the glue that calls it. It also holds H1's contract tests, one per process boundary that has broken.

Run it from the repository root:

```sh
pytest -q -rs smoke
```

It needs `uv` and either `docker` or a Postgres you point it at. Without them it skips and says why. A first run
takes about a minute, most of it starting the Postgres container.

## What the harness provides

The fixtures live in `smoke/conftest.py`. Every decision they make (which Postgres to use, the `docker` argv, the
sandbox environment, the profile text, whether the install is this tree) is a pure function in `smoke/plan.py`, tested
without mocks in `tests/test_smoke_plan.py`.

- **`install`** (once per session): `cox` installed from this checkout into a venv of its own with `uv pip install`.
  The install is not editable. Before any test runs, the fixture compares every installed `.py` file with the tree,
  so a stale build or another checkout cannot stand in for this one (#1184, #1185). The venv sits at
  `<install>/.venv`, so a profile whose `harness_dir` is `<install>` makes every harness child `cox` starts run the
  same install.
- **`postgres`** (once per session): the server named by `COX_SMOKE_POSTGRES_URL`, or else a `postgres:16`
  container started with `docker` on a random loopback port and removed at the end of the session. The suite opens
  only a loopback URL. Any other host is refused, so it can never reach a shared or live store.
- **`store_url`** (per test): a fresh database on that server, opened once by `harness.store_cli` so the schema
  migrations apply. It is dropped after the test.
- **`sandbox`** (per test): a temporary home with a routing profile at `~/.config/agent-tools/profile.yaml`, a
  provider profile naming the test's store, a workspace with `work/` and `runs/`, and a git config. Every `cox` runs
  with HOME, the XDG roots and git's global config inside the sandbox, the install's `bin` first on PATH, and nothing
  else inherited from the caller except the locale. `sandbox.cox(...)` runs the installed `cox`.
  `sandbox.harness(module, ...)` runs `python -m <module>` under the install's interpreter, the same interpreter
  `cox` starts. `sandbox.store_rows()` reads the store back through `harness.store_queue`.
- **`repo`** (per test): a clone at `~/src/app` of a bare origin in the test's temporary directory, with one
  commit on `main` pushed.

## Contract tests

Each contract test fails if its boundary drifts. The three contract files were checked by putting a drift back into a
scratch copy of the tree and watching them fail.

| Test | Boundary | What it would have caught |
| --- | --- | --- |
| `test_contract_store_cli.py` | `cox` to `harness.store_cli set-state` | #1174: `--expect quarantined` and the target `blocked` were refused with "invalid choice". Also a live drift (below). |
| `test_contract_route_edit.py` | `cox route import` and `cox route edit` to the store | A move the store refuses, or a file and a store row that disagree. Its unit tests replace the store with a double. |
| `test_contract_runs_recover.py` | `cox runs recover` to git and the store | The git argv, the worktree add and remove, and the close. The repo is passed as `~/src/app`; an unexpanded tilde is the bug #572 fixed for a partial land. |
| `test_sandbox.py` | the harness itself | `cox` reading anything outside the sandbox, or a child interpreter importing another checkout. |

**A drift found while building this suite.** The chair's revert of a phase quarantines its tasks with
`set-state <initiative> <task> quarantined` (`cli._quarantine_phase_with`). The store CLI refuses that target, because
only the epic driver writes `quarantined`. The caller ignores the result, so the store never records the quarantine.
The case is marked `xfail(strict=True)` in `test_contract_store_cli.py`. Whoever fixes it deletes the mark, and the
test then has to pass.

## Where it runs

- **CI.** The required `test` job has a `postgres:16` service and a `smoke` step. That step sets
  `COX_SMOKE_POSTGRES_URL` to the service and `COX_SMOKE_REQUIRED=1`, so a missing `uv` or Postgres fails the job
  instead of skipping it.
- **Lane checks.** `.agent-checks` runs `pytest -q -rs smoke` after the component suites. A lane host with docker
  runs it against its own container. One without docker skips it and prints the reason.

The containers are labelled `coxswain-smoke=1`. If a session is killed before its teardown,
`docker rm -f $(docker ps -q --filter label=coxswain-smoke=1)` removes any container it left behind.

## Adding a smoke test

A smoke test drives an entry point (`cox <verb>`, a unit's command line) and checks what a person would see: the exit
code, the output, the files, the git refs and the store rows. It does not import the code under test, and it does not
patch anything. When a test needs a fact from the code, such as an enum, it asks the installed interpreter through
`sandbox.python_json`, so the fact comes from the same install the commands run.

## Design: a sandbox forge

This part is not built yet. H10 needs the smoke suite to cover the forge calls (`gh pr create`, checks, merge) as well
as git and the store. Today they are covered only by stubbed runners. `cox` has three forges, chosen per repository
by its `origin` (`forge_auto`): `forge_github` shells out to `gh`, `forge_forgejo` calls Forgejo's REST API, and
`forge_local` uses git alone.

**Option A: a Forgejo container.** Start `forgejo` beside the Postgres container, create a user, a token and a
repository through its API, and push to it over HTTP.

- For: a real server with real pull request, status and merge behaviour. It runs offline, it is hermetic, and it is
  cheap to start, the same way Postgres is. It drives `forge_forgejo` end to end with no new code in `cox`.
- Against: Forgejo's API is not GitHub's. `gh` speaks GitHub's REST v3 and GraphQL, and Forgejo serves neither.
  The `gh` argv and the JSON `cox` parses from it stay unproven. Forgejo's merge and check semantics also differ in
  detail from GitHub's.

**Option B: a recorded HTTP replay.** Record `gh`'s traffic against github.com once, then serve the recordings to
`gh` from a local server (`GH_HOST` pointed at it).

- For: it exercises the real `gh` binary and the argv and JSON `cox` uses with it.
- Against: a replay is a stub with extra steps. It returns what GitHub said on the day of the recording. It cannot
  notice that GitHub or `gh` changed, which is the drift a contract test exists to catch. Recordings carry tokens and
  repository names that must be scrubbed. Every new call needs a new recording against a live repository. `gh`
  talks HTTPS to an enterprise host, so the replay server also needs a certificate that `gh` trusts.

**Option C: a dedicated sandbox repository on GitHub.** Run the `gh` contract tests against a throwaway repository
with a scoped token.

- For: the only option that proves the `gh` boundary against the real service.
- Against: it needs the network and a secret, it is slow and rate limited, and fork pull requests cannot run it.

**Recommendation.** Use **A** as the smoke suite's sandbox forge. It is real, hermetic and fits the docker pattern
this suite already uses, so the land, merge and check-wait flows get end-to-end coverage through `forge_forgejo` in
every CI run and lane check. Cover the `gh`-only boundary with **C**: a handful of contract tests on a schedule and
before a release cut, not on every pull request. Do not build B. It would cost as much to keep up as C, and it would
prove less.

A forge-neutral flow should be tested once, through Forgejo. Only argv building and output parsing are specific to
`gh`, and those already sit in pure functions that unit tests cover.
