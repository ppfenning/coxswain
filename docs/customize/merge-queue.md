# Merge queue

The merge queue lands several approved sources in one pull request instead of one
at a time. It is off by default. This page covers the problem it solves, the
two profile keys that turn it on, and what happens when a batch fails.

## The problem

Lands are serialized per repository. Each land waits for CI before the next
one starts. Ten approved sources with disjoint changes still cost ten CI runs
in a row, and the last one waits behind nine others.

The queue batches sources whose changes cannot collide. The batch goes through
CI once, so the wait is paid once for the whole group.

## The profile setting

The queue is configured by a top-level `merge_queue` mapping in the profile.

```yaml
merge_queue:
  batch_size: 4
  wait_s: 120
```

`batch_size` is the most sources one batch holds. It is an integer of at least
1 and defaults to 1. A value of 1 means the queue is off and every land goes
through alone. A value of 2 or more turns the queue on.

`wait_s` is how many seconds the queue waits for more sources before it opens a
batch that is not full. It is a number of at least 0 and defaults to 0. A batch
opens when it holds `batch_size` sources or when `wait_s` has passed, and
never with nothing in it.

A missing section uses both defaults. A malformed value falls back to the
default for that key alone, so a bad `wait_s` does not discard a good
`batch_size`.

## What goes in one batch

Sources are batched only when their changed files do not overlap. Two sources
that touch the same file land in separate batches.

Two phases of one initiative never share a batch, because the second phase is
built to follow the first. A phase whose changed files are unknown never shares
a batch either, since the queue cannot show that it is disjoint.

## How a batch lands

A batch is one pull request. It carries one attributed squash commit per
source, so each source keeps its own author line and message.

The pull request is merged with a rebase merge, which keeps every commit as
written. The repository must permit rebase merges. If it does not, the batch
merge fails.

Each task in the batch is marked done against the batch pull request, not
against the branch of its own source.

## When a batch fails

A failing batch is bisected. The queue splits the batch into a left half and a
right half and runs each as its own batch. The left half lands before the
right. A half that fails is split again until the failing source stands alone.

The good sources land. The failing source goes back to the chair as a
`needs_chair` escalation, with the failure attached. One bad source costs the
others some extra CI runs, but it does not hold them back.

## Latency against CI runs

Batch size and wait trade latency for fewer CI runs. A larger `batch_size`
spreads one CI run over more sources, but each source may wait longer for the
batch to fill. A larger `wait_s` raises the chance that a batch fills, at the
price of a longer delay.

`wait_s` also delays a lone land. When only one source is ready, the queue
holds it for up to `wait_s` seconds in case another arrives. Leave `wait_s` at 0
if a single land should never wait.
