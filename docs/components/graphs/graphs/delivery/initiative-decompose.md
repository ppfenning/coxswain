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

## A task is not written until it is in the store and on disk

Straight after the arms have applied the proposals, the harness hands each cleared task file to the
writer `route file` uses (`write_filed_item`). A task is cleared when the gate approved it or the
policy auto-applied it. The writer builds the queue row with the same codec, rewrites the same text
and records the export baseline. It only warns when the store is unreachable, so the row is read
back through the tools' own reader, the one `route edit` uses. A task already in the store keeps its
row, so a second decompose never resets the state of work that has started. A task that is on disk
but not in the store cannot be launched or edited, so it counts as missing. The run always ends with
`decompose: <n> task(s) written, <m> missing: <ids>` and exits non-zero when `m > 0`. If the
cartridge routes no `planned` landing, every cleared task is reported missing with the routing error.

Each missing task gets a stderr line saying which half is absent and why. The apply arm's own
detail is logged per target as `<kind> <target>: applied|not applied: <detail>`. The expected file
path is `<landing>/<initiative>/<phase>/<task>.md`, the path the proposal's action tells the arm to
create. A stale file already at that path would count as on disk.

The apply arm is a model arm that writes the file, so the store row follows the file. A run killed
between the two leaves a file with no row and prints no closing line. `cox route store-fill` repairs
that case.

## Refusing to emit nonsense

A cycle means nothing in it can ever become ready — the phase would stall with
no explanation — so a decomposition containing one is **refused before anything
is proposed**, and refused again by the work store on read. Both checks exist
because the failure is silent and the cost of finding it late is a stalled phase
nobody can diagnose.

`totals` reports `edges_dropped` and `immediately_startable`, which is the
number worth watching: it is how much of the phase can begin at once.

**Status:** implemented in [`initiative_decompose.py`](https://github.com/ppfenning/coxswain/blob/main/src/graphs/graphs/delivery/initiative_decompose.py).
