# Providers

This page settles what a provider profile controls: which tier a seat
runs on, how much effort it spends, its dollar ceiling, and which tools
it's allowed to call, per role.

## What a profile sets, per role

A provider profile is not one setting for the whole run. For each role —
builder, charter reviewer, adversary, arbiter, validator — the profile
sets:

- **tier** — which model tier fills that role.
- **effort** — how much of that tier's budget the role is expected to
  spend.
- **ceilings** — the dollar limit the role can't exceed before the node
  stops (see [Budgets](../methodology/budgets.md) for what happens
  then).
- **tools** — which tools that role is allowed to call at all.

Two roles on the same run can run on different tiers with different tool
lists; an adversary doesn't need the same tools a builder does, and
running it on a cheaper tier is a profile decision, not a code change.

## Which providers exist

`manifest.toml` names which providers are available and which are ready
to use:

```
claude-code = { status = "supported", profile = "providers/claude-code.yaml" }
codex       = { status = "planned" }
gemini      = { status = "planned" }
local       = { status = "planned" }
```

Only `claude-code` is `status = "supported"` today; the others are
`status = "planned"` and have no profile to point at yet.

## Where the detail lives

The full shape of a provider profile — the file format, every field a
tier or role can set — is in [Install:
Providers](../install/providers.md). This page is about what a profile
is for; that one is about how to write one.
