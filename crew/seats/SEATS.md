# The seat contract

A **seat** is a standing role in a small agent organisation: a bounded surface,
an explicit write authority, a tiering rule, and a brief discipline. Seats are
the part that does not change when you change employers.

Each seat is three layers, and only the first lives here:

| Layer | What it is | Where it lives |
|---|---|---|
| **Seat** | surface, write authority, tiering, brief discipline | this repository, `seats/*.md`; the voices in the table below |
| **Role binding** | which skills this seat routes to *for a given team* | `src/cartridges`, a team cartridge's `crew:` block |
| **Rendering** | the file a harness actually loads | generated into `~/.claude/agents/` by `install.sh` |

## Rules every seat obeys

1. **A seat routes; it never re-implements.** A definition that restates a
   skill's procedure drifts the moment the skill changes and nothing tells you.
   Name the skill, do not copy it.
2. **The tool allowlist IS the write boundary.** Prose in the body is not
   enforced; `tools:` is. Write authority belongs in the frontmatter.
3. **The model is explicit, never inherited.** `model: inherit` puts whatever
   the main loop is running on a job that did not ask for it.
4. **Voice is decoration.** Every seat works in any session, spoken or typed.
   A seat may not fail because no text-to-speech service is reachable.
5. **Depth stops at the seat's own workers.** A seat may spawn workers; those
   workers spawn nobody.
6. **Judgment stays home.** Find, locate, extract and classify go to cheap
   tiers. Verify, judge, arbitrate, synthesize, scope and root-cause stay on
   the seat's own turn — a confident shallow answer misleads whoever acts on it.

## The roster

Seats are named by function. The file stem, the frontmatter `name:` and the
key a cartridge's `crew:` block binds are all the function name.

| Seat | Surface | Write authority |
|---|---|---|
| **recon** | codebase sweeps, log analysis, research, readiness audits | none |
| **triage** | alerts, failures, root cause, blast radius | none |
| **reviewer** | reads the diff, never the builder's account of it | none, and no merge |
| **builder** | worktree, branch, tests, one pull request | code on its own branch |
| **ops** | CI, PR health, deploys, local tooling | its own PR branches |
| **planner** | the board: epics, phases, item bodies, dates, reconciliation | proposes; the chair writes |
| **writer** | docs, RFCs, outward-facing replies; routes plans and recaps to `visual-plan` and `visual-recap` | drafts only |
| **retro** | reads the run records and proposes what should change: skills, workflows, spend, model choice, hygiene | none; every change is a PR |

## The voices

Voice is decoration (rule 4), and this table is the only place it lives. A
voice session reads the seat's voice and spoken name from here; no seat
prompt, cartridge or code path depends on either. Every seat that has a voice
has a row, and `check_seats.py` fails a seat that has none.

| Seat | Voice | Spoken name |
|---|---|---|
| recon | `af_nova` | Nova |
| triage | `af_sky` | Sky |
| reviewer | `bm_lewis` | Lewis |
| builder | `af_sarah` | Sarah |
| ops | `am_michael` | Michael |
| planner | `bf_emma` | Emma |
| writer | `am_echo` | Echo |
| retro | `bm_daniel` | Daniel |

The voices ship with [Kokoro](https://github.com/remsky/Kokoro-FastAPI); none is
required.

## Old names (deprecated)

The seats used to be named after people. For one release a cartridge's
`crew:` (or `cast:`) block may still bind an old name: the cartridge loader
resolves it to the seat below and names the layer in the resolved
cartridge's `deprecations` list. A retired seat's binding is ignored, with a
deprecation naming where its work went. No old name is rendered into
`~/.claude/agents`; `install.sh` removes the links it left there. The
aliases go in the next release.

| Old name | Seat |
|---|---|
| sarah | builder |
| lewis | reviewer |
| nova | recon |
| michael | ops |
| emma | planner |
| echo | writer |
| daniel | retro |
| sky | triage |
| model-steward | retired: model choice is retro's |
| alice | retired: writer routes plans and recaps to `visual-plan` and `visual-recap` |
| liam | retired: courier events are typed |

## The brief every seat writes

1. what is already established
2. what is explicitly out of scope
3. a return-shape limit
4. parse programmatically, never read wholesale
5. a stop condition

## The chair is not a file here

The seat that routes, briefs, gates and synthesizes is the **chair**: the
**session you are talking to**, which holds the chair lease (a live
`cox chair run` loop holds it when no session does). It has no definition file because it is not a subagent. It runs
on whatever model the session was started with, and that choice never reaches
the cast: seats carry their own `model:`, and a graph run's models come from
the provider profile's tier bindings.
