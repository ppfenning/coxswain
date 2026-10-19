# Skills

This page settles how a skill gets attached to the work: a skill binds
to a role, and the skill's own body (not the role, not the binding) is
the source of truth for what an agent in that role actually does.

## Two keys bind skills

A cartridge binds skills in two places:

- **`skills:`** binds the roles a graph's nodes ask for (`plan`, `build`,
  `review_charter`, `work_state_arm` and so on) to one skill each. Base
  lists the roles under `roles.required` and `roles.optional`; a required
  role left unbound is refused at load.
- **`crew:`** binds the seats a person delegates to (the crew members) to
  the skills they route to. It is optional. A seat with no binding routes
  to nothing team-specific.

A skill is named `plugin:skill`. The `local` cartridge, for example,
binds:

```yaml
skills:
  plan:           local-skills:plan-task
  build:          local-skills:build-in-worktree
  review_charter: local-skills:review-charter
```

The same skill can bind to more than one role, and in more than one
cartridge.

## The skill body is the source of truth

Everything a bound role does comes from the skill's own body: its
instructions, its discipline, its description of what "good" looks like
for that job. The role and the binding are plumbing that gets the right
body in front of the right agent for the right run. If a skill's
behavior needs to change, the fix is in the skill body, not in a
role-specific patch layered on top of it.

## Where bindings and bodies live

Bindings are cartridge keys, layered like any other: base binds nothing,
`local` binds every required role, and a team overrides what it wants.
The skill bodies Coxswain ships live in the checkout under
`src/cartridges/skills-plugins/` and move with its release tag. The
harness also reads every folder named in the profile's `skills_roots`.
See [Cartridges](../methodology/cartridges.md) for how provenance works.

## Writing a new one

If no existing skill does the job, see [Write a skill](write-a-skill.md)
for the shape a new one needs and how it gets bound once it exists.
