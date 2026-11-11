# Efficiency

This page shows where Coxswain's cost and speed numbers come from and
which views read them. Each view answers one question about whether the
system is getting cheaper and faster.

## Where the numbers come from

Every model call is a row in the run store. The row carries its cost, its
turns, its tokens, and its task.

## The dashboard

The Efficiency section of the Coxswain dashboard charts these questions.
See [Dashboards](dashboards.md) to run it.

- **Cost per turn by model.**
- **Cache-read share.**
- **Cost per completion.**
- **Landed per day.**
- **First-try build rate.**
- **Lead time.**
- **Spend by task outcome.**

Quarantines by cause is also a chart.

## Cost per completion

A task counts as a completion when it is done, or approved and merged
into its phase or PR branch, or landed on main. It counts once, however
it later lands.

Cost per completion is spend divided by completions in the same window.
The status line and the feed report it for today, the five-hour window
and the week.

The feed keeps the old keys for one release as deprecated aliases:
`per_landed` under `spend.today`, and `cost_per_landed_task` under each
`spend_split` window. Towpath reads `cost_per_completion` first.

## `cox stats gates`

For each review step, `cox stats gates` shows its cost and how often its
verdict changed the outcome. It flags steps that change fewer than 5% of
outcomes. Those steps are candidates for a cheaper tier, or for skipping
on small tasks.

## `cox stats tiers`

For each role and model, `cox stats tiers` shows the landed rate, the
first-try rate, and the cost per completion.

It picks the cheapest model within 5 points of the best landed rate. Each
model needs at least 20 tasks to count. It then shows what the pick would
have saved.

The command changes nothing. To apply a pick, edit your provider profile.

## Shadow tier trial

`shadow_tier` is a profile setting that trials a cheaper tier on a role
without moving the role. It maps a role to the tier to try.

```yaml
shadow_tier:
  handoff: cheap
  validate_chunk: cheap
```

Each call of a listed role makes one extra call at the named tier. The
trial therefore costs more than not running it, and keeps costing more
until you remove the entry. The extra verdict is recorded on the acting
call and never used. The acting call alone decides the outcome.

Only `handoff` and `validate_chunk` are accepted. A code-writing role is
refused when the profile is read.

`cox stats shadow-tier` reads the recorded pairs and reports, for each
role, the agreement between the two verdicts, the costly-disagreement
rate, and the cost saved. It also says whether the role clears the bar.
The bar is 95% agreement and at most 1% costly disagreements, over at
least 100 calls. A role that clears it prints `READY`. A role that does
not prints `NOT YET` with the numbers it missed by. Use `--role` to read
one role.

The wrong belief to drop is that the report is a switch. A `READY` line
does not change any tier. Nothing moves a role to a cheaper tier until an
operator edits the profile.

## `cox stats causes`

`cox stats causes` groups quarantined attempts by cause, with sample
reasons. The causes are ticket, code, review, harness, and unknown.

## The docket

`cox route context` prints an efficiency line for today.
