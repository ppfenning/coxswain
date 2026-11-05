# initiative-decompose — specification

The front of the pipeline. An idea arrives as prose; phases, tasks, and the
dependency edges between them come out. Everything downstream — what can run in
parallel, what has to wait — is decided here.

| Node | Role | Tier | Notes |
|---|---|---|---|
| `size` | `size` | cheap | **optional**; runs first when an `intake` is given: `task` or `epic`, a one-sentence reason, surfaces for a task |
| `decompose` | `decompose` | standard | idea → phases → tasks, with edges and surfaces; skipped on a `task` verdict |
| `adversary` | `review_adversary` | deep | **attacks the dependency edges** |
| `emit` | — | — | one `item_create` proposal per task |

**Args:** `run_id`, `date`, `cartridge` (resolved, required, no fallback), `idea`.

**Returns:** `{run_id, date, idea, shape, phases, tasks, challenge, proposals[], totals}`

## The adversary is pointed at the edges, deliberately

This is the highest-leverage adversarial pass in the system, and it has one job:
find dependency edges that are not real.

Every spurious edge silently serialises work that could have run at the same
time, and nothing downstream will ever question it — the phase runner simply
sees a blocked task and waits. The person who just drew the graph is also the
last person likely to notice they drew too many edges, which is exactly the
situation a second, hostile reader is for.

It works in both directions. Dropping a false edge buys parallelism; adding a
real one that was missed prevents a task starting on ground that is not there
yet, which is the failure that parallelism would otherwise cause. Two things it
is not allowed to do: invent a task that does not exist, and make a task depend
on itself.

## Refusing to emit nonsense

A cycle means nothing in it can ever become ready — the phase would stall with
no explanation — so a decomposition containing one is **refused before anything
is proposed**, and refused again by the work store on read. Both checks exist
because the failure is silent and the cost of finding it late is a stalled phase
nobody can diagnose.

## The idea reach check

An intake's text can name a path that lies outside the target repository. The decompose does not refuse
such an intake. It rewrites the outside path so the text no longer points past the repo, and it keeps
going.

- Each rewrite becomes a note. The notes are written as a `lint:` block list in the initiative
  frontmatter, so the human at the gate sees what was changed.
- Filing prints the same notes as `routing: lint: ` lines, so the two surfaces report one set of
  rewrites.
- A path covered by `reach_allow` is left alone. So is a path inside the target repo.

One refusal remains. A body that is only an outside path has nothing left once the path is rewritten.
It is refused before any model call, so no tokens are spent on it.

`heal_idea_reach` in [`ticket_lint.py`](ticket_lint.py) is the single home of the rule. The decompose
and the filing both call it, so the two cannot drift.

## The layout check

[`decompose_layout.py`](decompose_layout.py) checks the proposed tasks before anything
is proposed. It reads only the tasks, never the repository. Three things are reported.

- A task whose body does not name every path in its `surfaces`. This **blocks**, as a
  `lint-refused` quarantine. A body that names no path at all was never written against its
  surfaces, so that case is flagged and does not block.
- A migration, test or script filename in a body that is not among the task's surfaces. This
  **blocks**. It catches a body that says `tests/test_widget_new.py` while the surface says
  `tests/test_widget.py`. Absolute paths are outside the repository and are skipped.
- A `needs` edge whose body names no id, title or surface of the task it needs. This is
  **advisory**: it lands on the ticket as a `lint:` entry for the human at the gate.

The outcome carries `layout`: `findings`, `width` and `layers`. `width` is the most tasks
with no `needs` path between them, from `max_width` in `harness/lanes.py`. It is reported
and never enforced here.

`totals` reports `edges_dropped` and `immediately_startable`, which is the
number worth watching: it is how much of the phase can begin at once.

**Status:** implemented in [`initiative_decompose.py`](initiative_decompose.py).
