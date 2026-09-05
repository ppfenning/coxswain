# Providers

A provider profile tells `cox` which CLI to drive, which plugin to load, and
what each crew tier is allowed to spend. `--provider` at install time
selects one from `manifest.toml`'s `[providers]` table.

| Provider | Status | What the profile supplies |
| --- | --- | --- |
| Claude Code | supported | The `claude` CLI on `PATH`, the Coxswain plugin, and the per-tier model choice and budget ceiling for each seat. |
| Codex | planned | A profile still needs to name the Codex CLI on `PATH` and map Coxswain's seat tiers to Codex's own model and budget controls. |
| Gemini | planned | A profile still needs to name the Gemini CLI on `PATH` and map Coxswain's seat tiers to Gemini's model and budget controls. |
| local | planned | A profile still needs to name a local model runner on `PATH` and define what a budget ceiling even means without a metered API. |

Only Claude Code is installable today; the `--provider` flag accepts the
others, but `cox doctor` will report their profile as unconfigured.
