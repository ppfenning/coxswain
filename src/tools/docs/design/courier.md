# Courier: the reference scheme and the bus contract

> Since this was written a seventh kind joined the six below: `initiative`, whose `<id>` resolves to
> `work/<id>/initiative.md`, or to the initiative whose frontmatter `id` matches (`agent_tools/courier.py`).

Courier lets one graph or chair hand a note to another without polling the
other's state. This fixes the reference, resolver, and bus so tickets 2 and
3 build without re-deciding anything.

## The reference

A courier reference is a URI: `coxswain://<kind>/<id>`, for six kinds. Every
`<id>` comes from one of three stores this codebase already keeps: the run
record (`runs/<run>/tasks/<phase>/<task>.json`), the autonomy ledger
(`~/.local/state/agent-graphs/ledger.jsonl`; `run-stats-store.md` §1: one row
per gate decision, keyed by `run_id` in `<run>:<node>` form and a `kind` of
`item_create`, `state_move`, `draft_pr_create`, `self_modification`,
`merge_stack`, or `stack_rebase`), or the work-store file.

- `run` — `<id>` is the run id addressing that run's own record.
- `task` — `<id>` is the task path the work-store file already carries.
- `pr` — `<id>` is `<run_id>:draft_pr_create`, the ledger's `run_id` and
  `kind` naming the row that gates the PR.
- `intake` — `<id>` is the intake ticket id the work-store file already carries.
- `proposal` — `<id>` is `<run_id>:<kind>`, the same ledger fields naming
  any other gated row — no minted id, just the ledger's own key.
- `finding` — `<id>` is the run id; resolves to the run record's
  `arbitration.reasoning`, falling back to the first `why_wrong` in
  `adversary`, the fields `_objection` (`runs_detail.py`) actually reads.

No kind gets a new id: every `<id>` is a run id, a ledger `run_id:kind`
pair, or a work-store path.

## The resolver

One function, `resolve(ref) -> record`, takes a reference and returns the
record it names. `run` and `finding` resolve through the run-record lookup
keyed by the run id; `pr` and `proposal` resolve through the ledger lookup
keyed by `run_id:kind`; `task` and `intake` resolve through the work-store
lookup keyed by the path. It never assembles a filesystem path from the
reference — every kind goes through a lookup the codebase already has.

## The bus

An append-only `courier.jsonl` at the workspace root — the directory a
profile's `workspace_dir` names (`agent_tools/route.py`, `_KNOWN_KEYS`). One
JSON object per line, carrying at minimum: `ref`, `from` (sender label),
`to` (recipient label), `note`, `id` (message id), and `ack` (acknowledged
flag).

Three commands operate on it:

- `cox courier send <ref> --to <label> --note <text>` appends one line with
  a fresh message id and `ack: false`.
- `cox courier inbox [--label]` lists each message id whose last line has
  `ack: false`, filtered to `to` matching `--label` when given.
- `cox courier ack <id>` appends a new line with that same message id and
  `ack: true`; the last line per id decides acknowledgment, so acking never
  rewrites a prior line.

### Typed events and topics

An entry may also carry three optional keys. `event` names what happened,
`fields` is a dict of its details, and `topic` groups entries for a reader
that follows one subject. A key with no value is absent, so an untyped entry
is byte for byte what it was before. `send` refuses an `event` outside the
list with a `ValueError`.

The eight events are `needs_attention`, `level_merged`, `merge_failed`,
`relaunched`, `stuck`, `gate_passed`, `gate_failed` and `epic_complete`.
Four of them wake a model: `needs_attention`, `merge_failed`, `stuck` and
`gate_failed`, named `EXCEPTION_EVENTS`. `needs_model(entry)` is true for an
entry whose event is one of those four. The other four are routine and are
read without a model.

`inbox` filters on `topic` and `event` by equality when they are given.

### The per-consumer ack

More than one consumer can read a label. A consumer acks by appending a line
that records its name in an `acks` list and leaves the legacy `ack` flag as
it was, false. `inbox` given a `consumer` hides an entry when that consumer
is in `acks` or when `ack` is true. Given no consumer, `inbox` looks only at
`ack`, as before. The legacy `ack` hides an entry from everyone.

The last line per id still decides the entry, but the `acks` lists of every
line of that id are unioned, and `ack` is true once any line of that id set
it. Two consumers that each append from a stale read of the bus cannot lose
one another's ack, and a consumer ack written from a read that predates a
legacy ack does not bring the entry back.

### Publishing

`append_file` writes one line to the bus without reading and rewriting it. A
torn final line, one missing its newline, is closed first. Event publishers
use it so they never read-modify-write `courier.jsonl`.

## Labels

`from` and `to` hold labels the codebase already uses elsewhere, not a new
namespace: chair labels in the `chair-YYYY-MM-DD` shape built in
`agent_tools/cli.py`'s `_launcher` (`f"chair-{...:%Y-%m-%d}"`, the label
passed to `_route_chair_take`), and graph names as used everywhere else a
graph is named.

## Who writes

Two writers today: the chair, and the steward's proposals. Named as a
future writer only, not designed here: the epic's `approved but not
landed` lines, which become sends once a separate harness-repo ticket
lands.

## Who reads

Two readers. The next chair, on `cox` start: bare `cox` takes the chair
(`_route_chair_take(chair_a)` in `_launcher`) and prints the inbox for its
own label right after. And `route context`, which shows the inbox count for
the label asked about, alongside the rest of the context it renders.

## Out of scope

No network transport and no second machine. The Forgejo-backed workspace
directory is the only transport courier uses; `courier.jsonl` lives where
every session already has filesystem access, and reaching it never crosses a
network boundary this design has to name.
