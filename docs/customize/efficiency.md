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
- **Cost per landed task.**
- **Landed per day.**
- **First-try build rate.**
- **Lead time.**
- **Spend by task outcome.**

Quarantines by cause is also a chart.

## `cox stats gates`

For each review step, `cox stats gates` shows its cost and how often its
verdict changed the outcome. It flags steps that change fewer than 5% of
outcomes. Those steps are candidates for a cheaper tier, or for skipping
on small tasks.

## `cox stats tiers`

For each role and model, `cox stats tiers` shows the landed rate, the
first-try rate, and the cost per landed task.

It picks the cheapest model within 5 points of the best landed rate. Each
model needs at least 20 tasks to count. It then shows what the pick would
have saved.

The command changes nothing. To apply a pick, edit your provider profile.

## `cox stats causes`

`cox stats causes` groups quarantined attempts by cause, with sample
reasons. The causes are ticket, code, review, harness, and unknown.

## The docket

`cox route context` prints an efficiency line for today.
