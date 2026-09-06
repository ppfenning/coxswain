---
name: release-sync
description: Decide whether the umbrella's CLI docs, manifest, release notes, and package pages match what the components actually ship, and report each mismatch as a Drift.
---

# Release sync

`cox dev release` and `cox dev release-check` both call you to compare the
umbrella's claims — CLI docs, `manifest.toml`, release notes, package pages —
against facts an edge already gathered from the components. You decide; you
never gather. A finding is a `Drift`, never a raw assertion that something is
wrong.

## Discipline

- **A check measures, never asserts.** Each of the four checks below is a
  pure function over facts already gathered (plain dicts, sets, strings) and
  returns `list[Drift]`. A check does not read a file, run a command, or
  touch the network — that is the edge's job, done before the check runs.
- **`Drift` is the only finding shape.** `Drift(check: str, a_file: str,
  a_line: int | None, b_file: str, b_line: int | None, correction: str)` —
  the file and line on each side of the mismatch, and the smallest edit that
  resolves it, in the shape the update-docs arm applies without reshaping.
- **Check 1, CLI surface.** Every `cox <group> <cmd>` reachable by walking
  `cox --help` recursively must appear in the umbrella's
  `docs/reference/cli/*` and in the README Commands section of the component
  that provides it. A command missing from either doc, or a documented
  command the CLI no longer has, is a drift — either direction is a finding.
- **Check 2, manifest.** The umbrella `manifest.toml` (`[coxswain] version`,
  `[components] <name> = { repo, tag, ... }`) must agree with
  `docs/components/<name>.md` and the release-notes page
  `docs/releases/<version>.md` for the version being cut. A component with no
  docs page, a docs page stating a different version, or a component absent
  from the release notes is a drift.
- **Check 3, notes citation.** Every bullet on the release-notes page for the
  version being cut must name a component and cite a landed PR (`#N`) or
  commit sha in that component. An uncited bullet, or a citation that does
  not resolve in that component's history, is a drift.
- **Check 4, package pages.** For every component with a `publish.yml`: its
  `pyproject.toml` declares `readme`, and its locally built sdist's
  `PKG-INFO` (`uv build`, read back, never uploaded) carries a non-empty
  `text/markdown` description; the README's H1 is the component's
  post-rename name; a deprecated alias (`agent-tools`, `cast`) appears in the
  README or the `description` field only inside a sentence that says it is
  deprecated; the `description` field is one sentence under 160 characters.
  Any one of these unmet is a drift.
- **Two callers, two postures.** `cox dev release` refuses to tag while any
  `Drift` stands, unless invoked with `--allow-doc-drift <reason>`, in which
  case it tags and records the reason alongside the run. `cox dev
  release-check` reports whatever drift exists on demand and never blocks
  anything — it has no tag to refuse.

## Failure modes

- A check that shells out or opens a file itself instead of taking gathered
  facts as arguments.
- A finding reported as prose instead of a `Drift` with both sides located.
- `release-check` treated as a gate, or `release` treated as advisory.
