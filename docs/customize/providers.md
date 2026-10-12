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
local       = { status = "experimental", profile = "providers/local-oss.yaml" }
```

Only `claude-code` is `status = "supported"` today. `local` is
`status = "experimental"`: its profile, `providers/local-oss.yaml`, declares
its capabilities and puts the `cheap` tier on a local OpenAI-compatible
endpoint, but no runner drives it yet. The others are `status = "planned"`
and have no profile to point at.

## The operator

The provider-profile key `operator` names the human the courier and
decisions address. Its default is `operator`, so a profile that omits it
addresses "operator". Set it to a team role or a handle:

```yaml
name: claude-code
operator: reviewer-on-call
```

The settings command flags operator-only rows with the field
`operator_only`. The older field name `pat_only` is accepted as an alias
for one release and then removed, so use `operator_only` in anything new.

## Where the detail lives

The full shape of a provider profile — the file format, every field a
tier or role can set — is in [Install:
Providers](../install/providers.md). This page is about what a profile
is for; that one is about how to write one.
