# Changelog

## 1.0 — 2026-09-16

`core.SCHEMA_VERSION` begins tracking four contracts:

- `core.workstore.STATES`: `("todo", "ready", "in_progress", "approved", "blocked", "done", "dropped")`
- the item frontmatter field whitelist that `read_item` and `write_item` use: `id`, `phase`, `state`, `needs`, `surfaces`, `patterns`, `title`, `attempts`, `budget_usd`, `priority`
- `core.workstore.ATTEMPT_KINDS`, the closed set `record_attempt` validates: `("refused", "no_work", "unverified", "infra")`
- `core.ledger.REQUIRED_FIELDS`, the ledger row's required fields: `("run_id", "ts", "principal", "kind", "risk", "outcome", "cartridge_sha", "provider_profile")`
