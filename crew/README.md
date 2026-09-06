# coxswain-crew

A standing cast of named agent **seats** — who does what, what each may touch,
and how each reports — with no employer inside any of them.

Most agent rosters hardcode a workplace into the definitions: this tracker,
that board, those conventions. Then the team changes, or you do, and the
roster is worthless. Here the seat is the durable part and the employer is a
binding you swap.

## The six repositories

| Repository | Owns | The sentence |
|---|---|---|
| [`coxswain-cartridges`](https://github.com/ppfenning/coxswain-cartridges) | who a run works for | roles → skills, where writes land, tier → model |
| [`coxswain-graphs`](https://github.com/ppfenning/coxswain-graphs) | what runs, and the harness that runs it | sequence, the gate, the ledger |
| **`coxswain-crew`** | who speaks | seats, their authority, their voices |
| [`coxswain-tools`](https://github.com/ppfenning/coxswain-tools) | the `cox` command | run records, traces, landing, screens |
| [`coxswain`](https://github.com/ppfenning/coxswain) | the umbrella | docs, manifest, one-line install |

Cartridges say who a run works for, graphs say what runs, the crew says who
speaks. Each is usable without the others.

## The cast

Three lanes, grouped by what a seat may touch rather than by who reports to
whom. The read-only lane exists so findings arrive from eyes that did not do
the work.

| Seat | Voice | Surface | Write authority |
|---|---|---|---|
| **Nova** | `af_nova` | recon — codebase sweeps, log analysis, research, readiness audits | none |
| **Sky** | `af_sky` | triage and incidents — alerts, failures, blast radius | none |
| **Lewis** | `bm_lewis` | review — reads the diff, never the builder's account of it | none, and no merge |
| **Sarah** | `af_sarah` | build — worktree, branch, tests, one pull request | code on its own branch |
| **Michael** | `am_michael` | ops — CI, PR health, deploys, local tooling | its own PR branches |
| **Emma** | `bf_emma` | the board — epics, phases, item bodies, dates, reconciliation | proposes; the chief writes |
| **Echo** | `am_echo` | writing — docs, RFCs, outward-facing replies | drafts only |
| **Alice** | `bf_alice` | scribe — the visual plan for a swarm, tracked to completion | `plans/` only |
| **Daniel** | `bm_daniel` | steward — reads the run records and proposes what should change: skills, workflows, spend, hygiene | none; every change is a PR |

The **chief of staff** is the session you are talking to, not a file: see
[`seats/SEATS.md`](seats/SEATS.md).

**A reviewer is independent by construction.** Lewis reads the diff and a base
ref, never the builder's summary, and is never the instance that wrote the
code. He advises — *not ready*, *split this*, *close it instead* — and merge
stays human.

## Install

```bash
git clone https://github.com/ppfenning/coxswain-crew ~/repos/coxswain-crew
~/repos/coxswain-crew/install.sh            # symlinks seats into ~/.claude/agents
~/repos/coxswain-crew/install.sh --check    # what is installed, and from where
```

Symlinks, not copies, so editing a seat here changes what every session loads.
`--prefix` installs elsewhere; nothing is overwritten unless it is already a
symlink into this repository.

## Binding a seat to your team

A seat names roles; a cartridge says which skills fill them. Add a `cast:`
block to your team cartridge in `coxswain-cartridges`:

```yaml
cast:
  nova:
    skills:   [audit-and-apply, pr-fleet-sweep]
    context:  "the read side of your internal SDK"
  sarah:
    skills:   [feature-lifecycle, test-driven-development]
```

Unbound seats still work; they simply route to nothing team-specific. Nothing
in this repository names a company, a tracker, a board, or a person.

## Voice is decoration

Every seat is a plain agent definition and works in any session. In a voice
session a seat reports in its own voice, which is how you hear that a finding
came from an independent pair of eyes rather than from the chief restating
itself. In a text session the voice line is dropped. All nine voices ship with
[Kokoro](https://github.com/remsky/Kokoro-FastAPI); none is required.
