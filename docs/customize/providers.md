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

## Where the profile lives

A provider profile is team configuration, not code. The checkout ships
defaults in `<checkout>/src/cartridges/providers/`; `cox install` points a
new machine at `claude-code.yaml` there. A team keeps its own profile
outside the checkout, in `<home>/providers/` or in the workspace, with
`catalog.yaml` beside it, and the machine profile's `provider_profile`
names it:

```yaml
# ~/.config/agent-tools/profile.yaml
provider_profile: <home>/providers/my-team.yaml
```

`cox setup doctor` fails its `home paths` row when `provider_profile` sits
outside both `<home>` and the workspace. Every machine that runs lanes for
the team uses the same profile, so the store it names is the same. See
[Install](../install/index.md#the-provider-profile) for the copy commands.

## The operator

The machine profile's `operator` key names the human the courier and
decisions address. It lives in `~/.config/agent-tools/profile.yaml`, not in
the provider profile. Its default is `operator`, so a profile that omits it
addresses "operator". Set it to a team role or a handle:

```yaml
operator: reviewer-on-call
```

The settings command flags operator-only rows with the field
`operator_only`. It also writes the older name `pat_only` with the same
value, for one release, so use `operator_only` in anything new.

## Where the detail lives

The shipped profile, `src/cartridges/providers/claude-code.yaml`, is
commented key by key: `tiers`, `classes`, `effort`, `budget_usd`,
`role_budget_usd`, `role_ceiling_usd`, `tier_overrides`, `defaults` and
`tools`. Copy it and change what your team needs. [Install:
Providers](../install/providers.md) lists the providers and their status.
