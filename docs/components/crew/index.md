# Crew

The crew is the agents that do the rowing: they plan, build, review, and
validate. The coxswain steers; the crew rows.

## What it owns

The `crew` folder holds eight seat definitions, each named by its function —
recon, triage, reviewer, builder, ops, planner, writer and retro — each one
role filled by one agent doing one job. The old person names (sarah, lewis,
nova, ...) still resolve in a cartridge's `crew:` block for one release, as
deprecated aliases.

## Installing it

`cox install` and `cox upgrade` link every seat from `<checkout>/crew` into
`~/.claude/agents`. Nothing to pass.

## Its own docs

The folder's README lives at
[crew](https://github.com/ppfenning/coxswain/tree/main/crew).
