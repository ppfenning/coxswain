# Skills

This page settles how a skill gets attached to the crew: a skill binds
to a seat and a role, and the skill's own body — not the seat, not the
binding — is the source of truth for what an agent in that seat actually
does.

## Binding a skill to a seat and a role

A binding says which skill fills which seat for which role: a builder
seat on a Python repo binds a different skill body than a builder seat
writing docs, even though both are "builder". The binding lives in the
cartridge's `crew` key (see [Cartridges](../methodology/cartridges.md)),
not in the skill itself — the same skill can bind to more than one seat
across different cartridges.

## The skill body is the source of truth

Everything a bound seat does for its role comes from the skill's own
body: its instructions, its discipline, its description of what "good"
looks like for that job. The seat and the binding are just plumbing that
gets the right body in front of the right agent for the right run. If a
skill's behavior needs to change, the fix is in the skill body, not in a
seat-specific patch layered on top of it.

## Where bindings and bodies live

Skill bindings are a cartridge concern, layered the same way any other
cartridge content is: base ships default bindings, local and team can
add or override them, tighten-only where the binding governs something
risk related. Skill bodies themselves are versioned independently, the
way any other pinned component is — see
[Cartridges](../methodology/cartridges.md) for how provenance works for
a cartridge's pinned dependencies.

## Writing a new one

If no existing skill does the job, see [Write a skill](write-a-skill.md)
for the shape a new one needs and how it gets bound once it exists.
