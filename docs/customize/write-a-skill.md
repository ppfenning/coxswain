# Write a skill

This page settles the shape a new skill needs, where it goes, and how it
gets bound once it exists.

## The shape

A skill is a body of instructions for one job: what a builder does, what
a charter reviewer holds work to, what a validator checks. It describes
discipline and judgment, not a script: the agent filling the role still
reasons about the specific task, using the skill body as its brief. See
[Skills](skills.md) for how the body becomes the source of truth once
it's bound.

The file is `SKILL.md`, with a short front matter:

```markdown
---
name: plan-task
description: Turn one work item into an ordered, bounded plan another agent can execute without asking questions.
---

# Plan

You produce the plan for exactly one work item...
```

## Where it goes

A skill lives in a plugin: a folder with `.claude-plugin/plugin.json`
(which carries the plugin's `name`) and a `skills/` folder. Each skill is
`skills/<skill>/SKILL.md`, and its bound name is `<plugin>:<skill>`:

```text
my-skills/
  .claude-plugin/plugin.json      {"name": "my-skills", ...}
  skills/
    review-data/SKILL.md          -> my-skills:review-data
```

Keep your team's plugin in your workspace, beside your cartridge, and add
its folder (or the folder holding it) to the profile's `skills_roots`.
The harness scans each root for plugins. The skills Coxswain ships are in
`src/cartridges/skills-plugins/local-skills/`.

## How it gets bound

Bind it in your team cartridge, under `skills:` for a graph role or under
`crew:` for a seat:

```yaml
skills:
  review_charter: my-skills:review-data
```

The cartridge loader refuses a bound name that resolves to no body, or to
more than one. Resolve the cartridge to check:

```sh
cartridge --team my-team --cartridges-dir <workspace>/cartridges --skills-root <path> --json
```

Writing the skill and binding it are separate steps: the skill can exist
and be versioned before any cartridge binds it.

## What to check before writing a new one

A new skill is worth writing when no existing skill's body actually
covers the job, not when an existing skill's body could be edited to
cover it too. Editing an existing skill changes it for every role and
cartridge that already binds it; writing a new one doesn't.
