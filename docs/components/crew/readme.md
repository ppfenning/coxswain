# crew

A standing cast of named agent **seats** — who does what, what each may touch,
and how each reports — with no employer inside any of them. This folder is
part of the coxswain repository and ships in every release. To install
Coxswain, see the [root README](https://github.com/ppfenning/coxswain/blob/main/README.md).

Most agent rosters hardcode a workplace into the definitions: this tracker,
that board, those conventions. Then the team changes, or you do, and the
roster is worthless. Here the seat is the durable part and the employer is a
binding you swap.

## What is here

```
seats/          one agent definition per seat (eleven), and SEATS.md for the chair
install.sh      links every seat into ~/.claude/agents (SEATS.md is not linked)
check_seats.py  checks every seat's frontmatter: name, description, tools, an explicit model
```

## Checks

From this folder:

```sh
cd crew && python3 check_seats.py
```

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
| **Emma** | `bf_emma` | the board — epics, phases, item bodies, dates, reconciliation | proposes; the chair writes |
| **Echo** | `am_echo` | writing — docs, RFCs, outward-facing replies | drafts only |
| **Alice** | `bf_alice` | scribe — the visual plan for a swarm, tracked to completion | `plans/` only |
| **Daniel** | `bm_daniel` | steward — reads the run records and proposes what should change: skills, workflows, spend, hygiene | none; every change is a PR |
| **Liam** | `am_liam` | courier — reads the courier inbox, resolves each reference, relays it to its owner | none; acks only |
| **Model steward** | none declared | model choice — owns which model each role runs on, read off the run stats; not daniel, who changes skills and workflows | proposes, never applies; one weekly pull request to the provider profile |

The **chair** is the session you are talking to, not a file: bare `cox` takes
the chair lease for its session unless a live `cox chair run` loop, the same
role run unattended, already holds it. [`seats/SEATS.md`](https://github.com/ppfenning/coxswain/blob/main/crew/seats/SEATS.md) still calls it the chief of
staff.

**A reviewer is independent by construction.** Lewis reads the diff and a base
ref, never the builder's summary, and is never the instance that wrote the
code. He advises — *not ready*, *split this*, *close it instead* — and merge
stays human.

## Install

`cox install` and `cox upgrade` run `install.sh` for you. To run it by hand
from a checkout:

```bash
crew/install.sh            # symlink every seat into ~/.claude/agents
crew/install.sh --check    # what is installed, and from where
```

Symlinks, not copies, so editing a seat here changes what every session loads.
`--prefix DIR` installs into `DIR` instead. A file that is not a symlink is never
overwritten; `install.sh` says so and skips that seat. `--uninstall` removes only
the links that point here.

## Binding a seat to your team

A seat names roles; a cartridge says which skills fill them. Add a `cast:`
block to your team cartridge (see `src/cartridges`):

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
came from an independent pair of eyes rather than from the chair restating
itself. In a text session the voice line is dropped. The model steward declares
no voice. The other ten voices ship with
[Kokoro](https://github.com/remsky/Kokoro-FastAPI); none is required.
