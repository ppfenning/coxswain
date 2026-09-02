# The seat contract

A **seat** is a standing role in a small agent organisation: a bounded surface,
an explicit write authority, a tiering rule, and a brief discipline. Seats are
the part that does not change when you change employers.

Each seat is three layers, and only the first lives here:

| Layer | What it is | Where it lives |
|---|---|---|
| **Seat** | surface, write authority, tiering, brief discipline, voice | this repository, `seats/*.md` |
| **Role binding** | which skills this seat routes to *for a given team* | `agent-cartridges`, a team cartridge's `cast:` block |
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

## The brief every seat writes

1. what is already established
2. what is explicitly out of scope
3. a return-shape limit
4. parse programmatically, never read wholesale
5. a stop condition

## The chief of staff is not a file here

The seat that routes, briefs, gates and synthesizes is the **session you are
talking to**. It has no definition file because it is not a subagent. It runs
on whatever model the session was started with, and that choice never reaches
the cast: seats carry their own `model:`, and a graph run's models come from
the provider profile's tier bindings.
