# Write a skill

This page settles the shape a new skill needs, where it goes, and how it
gets bound once it exists. Anything the design hasn't named yet is
marked **planned** below rather than guessed at.

## The shape

A skill is a body of instructions for one job: what a builder does, what
a charter reviewer holds work to, what a validator checks. It describes
discipline and judgment, not a script — the agent filling the seat still
reasons about the specific task, using the skill body as its brief for
the role. See [Skills](skills.md) for how the body becomes the source of
truth once it's bound.

## Where it goes

**Planned.** The design has not yet named the directory layout or
naming convention a new skill file follows, or which cartridge layer
ships a new skill by default.

## How it gets bound

Once a skill exists, it's attached to a seat and a role through a
cartridge's `crew` key, the same mechanism any existing skill uses — see
[Skills](skills.md) and [Cartridges](../methodology/cartridges.md).
Writing the skill and binding it are separate steps: the skill can exist
and be versioned before any cartridge chooses to bind it to anything.

## What to check before writing a new one

A new skill is worth writing when no existing skill's body actually
covers the job — not when an existing skill's body could be edited to
cover it too. Editing an existing skill changes it for every seat and
cartridge that already binds it; writing a new one doesn't.
