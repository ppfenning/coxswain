# Epic model (context pack)

How work decomposes, independent of which tracker you use.

## The threshold

Most work is not an epic. Making everything an epic is how a board becomes
unreadable. The bar is in `cartridge.yaml` (`epic_threshold`): genuinely
multi-phase, or three-plus tickets, or coordinated across repositories.

Below it:

- one unit of work → one ticket
- two related but unordered units → a parent ticket with subtasks
- related to an existing epic → attach to it rather than starting a new one

## The shape

An epic is a container with one section per phase. Phases are ordered; tickets
within a phase are not necessarily. One ticket, one pull request — if a ticket
needs three PRs it was really three tickets, and the reviewers will tell you so
by the second one.

Dependency edges go between tickets where order genuinely matters, and nowhere
else. A dependency that exists because "it feels sequential" blocks work that
could have run in parallel.

Phase prefixes stay short: three or four characters, stable for the life of the
epic. They show up in branch names, PR titles, and standup, and they get typed
by hand a lot.

## Scoping is a separate act from filing

A ticket that has not been scoped goes to the future-work landing area, not
onto the active board. Scoping is what promotes it to planned. Filing an
unscoped ticket directly onto the board is how a board fills with work nobody
has thought about, and the board stops meaning anything.

## Estimates

Estimate in the second pass, not the first. The first pass captures what the
work is; the second, once the shape is clear, says how big. Estimating during
capture produces numbers that reflect how well the writer understood the
problem in that moment.

Custom fields for estimates frequently differ between projects in the same
tracker. Resolve them at runtime; never assume a field ID carries across.

## Descriptions are prose

Topical headers and paragraphs, not a template with Summary / Details /
Acceptance Criteria boxes to fill. Templates get filled in dutifully and read
by nobody. Write what someone picking this up cold needs to know, in the order
they need it.

## Worktree discipline

Feature work happens in a dedicated worktree per ticket, under one flat root,
never in the main checkout. An agent that owns a worktree can be wrong
destructively without costing anything; an agent loose in the main checkout
cannot.
