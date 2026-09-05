# Crew and seats

This page settles who does the work: the crew are the agents, each one
filling a seat with a role and a set of skills bound to it, and the
coxswain is the one component in the loop that dispatches without ever
building.

## The crew rows, the coxswain steers

The crew are the agents that plan, build, review, and validate a task. The
coxswain dispatches work to them and enforces the in-flight bound — the cap
on how many runs are active at once — but it never fills a seat itself. If
the coxswain built, reviewed, or validated, it would be marking its own
work, and the review that follows would have nothing independent left to
check against.

## A seat is one role, one job, one run

A seat is one role in the crew filled by one agent doing one job on one
run: builder, charter reviewer, adversary, arbiter, validator. A seat does
not carry state between runs — the next run starts a fresh seat, even if
the same provider profile fills it.

## Skills are bound, not baked in

A seat's behavior comes from the skills bound to it for that role, not
from anything hardcoded into the seat. The skill body is the source of
truth for what an agent in that seat actually does; the seat is just the
slot. See [Skills](../customize/skills.md) for how a skill gets bound to a
seat and a role.

## One writer per board

Exactly one component is the writer of any given board or state machine.
If two seats could move the same ticket, the board's state would be a
race, and nobody could reason about what it means. Every seat other than
the single writer proposes; the writer applies.

## Where a seat's context comes from

A seat's prompt is assembled from fragments — pieces of context text
layered in by [cartridge](cartridges.md), not written into the seat
itself. Change the fragment and every seat that reads it changes with it;
change the seat and something structural has changed instead.
