# Customize

This page settles what is yours to change in Coxswain and where each
piece of it lives: your team's cartridge, its fragments, the skills bound
to your seats, your provider profile, and the policy knobs that shape how
runs dispatch.

## What's yours

- **Your cartridge** — the team layer on top of base and local. See [Team
  cartridge](team-cartridge.md).
- **Your fragments** — the individual pieces of context inside your
  cartridge's `cartridge.d/`. See [Fragments](fragments.md).
- **Your skill bindings** — which skills fill which seats and roles for
  your runs. See [Skills](skills.md).
- **Your provider profile** — which provider, which tier, which tools
  per role. See [Providers](providers.md).
- **Your policy** — the knobs that change review tier, plan competition,
  build budgets, and how many runs are in flight at once. See
  [Policy](policy.md).

## What isn't

The graph's own nodes and edges, the base conventions, and the crew and
seat mechanism are the platform's concern — see
[Methodology](../methodology/index.md). You extend them; you don't fork
them. Every layer you add can tighten what came before it and never
loosen it — see [Cartridges](../methodology/cartridges.md) for why.

## Writing something new

If what you need isn't a cartridge, a fragment, a binding, or a policy
value, but an actual new skill or graph, see [Write a
skill](write-a-skill.md) and [Write a graph](write-a-graph.md).
