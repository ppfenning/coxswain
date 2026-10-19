# graphs

The graphs a run executes, and the **harness** that runs them. This folder is
the `harness`, `runner` and `graphs` packages inside the coxswain repository;
they ship as part of the one `coxswain` package. To install Coxswain, see the
[root README](https://github.com/ppfenning/coxswain/blob/main/README.md).

Four nouns, one seam each:

| Noun | Owns | Lives in |
|---|---|---|
| **Harness** | consequences: side effects, policy, the gate, the ledger | `harness/` |
| **Graph** | sequence — what runs, in what order, at what tier. Writes nothing | `graphs/` |
| **Cartridge** | who a run works for: role → skill, where writes land | `../cartridges` |
| **Runner** | execution: canned dicts in tests, the model live | `runner/` |

```
harness/   the runtime. resolve, discover, run, gate, apply, record
graphs/    the programs. pure functions of (args, runner)
  delivery/   work that produces work: initiative-decompose, lifecycle-propose, phase-validate,
              review-diff, rescue-review
  ops/        keeping a running system honest: coxswain, epic-reconcile, retro-propose,
              triage-propose, triage, sweep
runner/    node execution: the protocol, a scripted runner, the live one
shell.py   the harness entry point: `python shell.py <graph> ...`
docs/      the contract every graph must satisfy, and one generated page per graph
fixtures/  canned runs and a small work store for offline runs and tests
tests/     this folder's suite, including the portability check
```

A graph is `run(args, runner) -> dict`. Nodes ask for a role and a tier, never
a skill or a model: the cartridge maps role to skill, the provider profile maps
tier to model. Nothing in a graph writes; the harness applies a proposal in a
worktree it created, after the gate approved it. See
[`docs/GRAPH-CONTRACT.md`](docs/GRAPH-CONTRACT.md) for the full contract.

## Tests

From this folder:

```sh
cd src/graphs && uv run pytest -q
```

The suite runs with no network and no key: `ScriptedRunner` replays canned
nodes.

## Running one offline

From this folder, against the cartridges beside it:

```sh
scratch="$(mktemp -d)"
uv run python shell.py triage --team local \
  --cartridges-dir ../cartridges/cartridges \
  --skills-root ../cartridges/skills-plugins \
  --alerts fixtures/alerts.json \
  --scripted fixtures/triage-run.json --assume r \
  --runs-dir "$scratch" --ledger "$scratch/ledger.jsonl"
```

`--scripted` replays canned nodes, so no key is needed. `--assume r` (or `a`)
answers the gate without asking. Pass `--ledger` as above for a throwaway run:
without it the gate decisions are appended to the default ledger,
`$XDG_STATE_HOME/agent-graphs/ledger.jsonl`, or `~/.local/state/agent-graphs/ledger.jsonl`
when `XDG_STATE_HOME` is unset. Drop `--scripted` to run against the real
model; the provider profile decides which model each tier means. A host's
`cox route launch` starts the harness the same way, with `harness_dir` set to
`<checkout>/src/graphs`.

## The graphs

The registry is every module under `graphs/` that declares a `SPEC`; `harness/registry.py` discovers them. `shell.py` takes the subcommand.

| Graph | Subcommand | Namespace | Shape |
|---|---|---|---|
| `initiative-decompose` | `decompose` | delivery | decompose → adversary-on-the-edges → emit. An idea into phases and a task DAG |
| `lifecycle-propose` | `lifecycle` | delivery | scope → plan → [alternative plan → arbitrate plans] → [attack the plan] → build (worktree) → handoff → review → adversary → arbitrate → emit |
| `phase-validate` | `validate` | delivery | validate_chunk per task → validate_phase against the phase's ORIGINAL goal. Invoked by the epic driver |
| `review-diff` | `review-diff` | delivery | review_charter → review_adversary → verify_evidence → [arbitrate]. Verdict and findings out, nothing written or pushed |
| `rescue-review` | `rescue-review` | delivery | handoff → review_charter → review_adversary → [arbitrate] → emit. A patch that already exists, reviewed with the harness's own check results |
| `coxswain` | `cos` | ops | one `dispatch` node over a driver-assembled docket. Selects; the harness invokes |
| `epic-reconcile` | `reconcile` | ops | compare (set arithmetic) → emit. Declared state vs actual |
| `retro-propose` | `retro` | ops | stats (pure arithmetic over ledger rows) → retro → emit. Proposes only what it can cite |
| `triage-propose` | `triage` | ops | fetch → classify → verify → emit. Zero writes; proposes corrections to the runbook it just used |
| `triage` | `triage-quarantine` | ops | facts (pure arithmetic over an item's attempts) → triage → emit. Classifies a stranded item at the attempt cap and emits by class, or escalates a repeated `(class, diagnosis)` pair |
| `sweep` | `sweep` | ops | sweep_plan → sweep_apply → sweep_verify → [review pair]. One rule applied everywhere it matches; sweep_apply calls a model only when the rule can't be scripted, one build call, and sweep_verify runs no model at all |

`shell.py` also takes `phase`, `epic` and `rescue`. These are drivers, not graphs: they invoke graphs, block on them and own side effects, so they live in `harness/` and declare no `SPEC`. `epic` is the epic driver (`harness/epic.py`, specified in [`graphs/delivery/epic-swarm.md`](graphs/delivery/epic-swarm.md)), which `cox route launch epic` starts.

Each graph's diagram is generated from its own docstring by `python -m harness.graphdoc`; the committed pages live under [`docs/graphs/`](docs/graphs/index.md).

## License

MIT. See [LICENSE](https://github.com/ppfenning/coxswain/blob/main/src/graphs/LICENSE).
