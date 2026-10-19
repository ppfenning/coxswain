# Providers

A provider profile tells `cox` which CLI to drive and what each crew tier
and role may spend and call. `manifest.toml`'s `[providers]` table lists
the providers. `cox install --provider <name>` (default `claude-code`)
checks the name against that table and checks that its CLI is on `PATH`;
the profile it writes always names the shipped
`src/cartridges/providers/claude-code.yaml`.

| Provider | Status | What the profile supplies |
| --- | --- | --- |
| Claude Code | supported | The `claude` CLI on `PATH`, and the per-tier model, effort and budget ceiling for each seat. |
| Codex | planned | A profile still needs to name the Codex CLI on `PATH` and map Coxswain's seat tiers to Codex's own model and budget controls. |
| Gemini | planned | A profile still needs to name the Gemini CLI on `PATH` and map Coxswain's seat tiers to Gemini's model and budget controls. |
| local | experimental | `providers/local-oss.yaml` declares the capabilities the harness reads and maps the `cheap` tier to a local OpenAI-compatible endpoint, with `standard` and `deep` still on Claude; no runner drives it yet, so the install works only as a declaration. |

Only Claude Code is installable today. `cox install --provider` refuses any
other name: `provider 'codex' is 'planned', not supported`.

A team's own profile lives outside the checkout, in `<home>/providers/` or
the workspace; see [The provider profile](index.md#the-provider-profile).
