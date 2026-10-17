# Release rehearsal gate

A release is cut only after a rehearsal has passed on the exact commits it will tag. The rehearsal is a GitHub Actions workflow. The release command only looks up its result. This page decides how the pieces connect.

The rehearsal runs three scenarios. Each scenario is a job in the workflow.

- **fresh**: install the candidate commits on a clean machine by the documented install command and check that `cox doctor` passes. The candidate installs fresh into the default coxswain home, with no root or workspace flag. It does so after the candidate tools are installed at the pinned sha.
- **upgrade**: install the previous release, populate a workspace with real work items, upgrade to the candidate commits and check that the workspace still reads. The older install is migrated into that home by the apply form of the setup migrate command. The migration runs after the pin step and before lint.
- **migrate**: run each migration the release declares against a populated workspace and check that each one leaves it valid.

The migrate scenario may not skip when migrations are declared. It may skip only when the release declares none.

## 1. How the candidate refs reach the workflow

The workflow is `.github/workflows/release-rehearsal.yml`. It runs on `workflow_dispatch` only. Everything it needs arrives as a dispatch input. The inputs are:

- `cartridges_sha`
- `graphs_sha`
- `tools_sha`
- `crew_sha`
- `dash_sha`
- `previous_tag`
- `migrations`

Each `*_sha` input is a full 40 character commit SHA. There is one per component in `manifest.toml`. A branch name or a short SHA is refused, because a branch can move between the rehearsal and the tag. `previous_tag` is the last release tag, such as `v0.40.0`, and feeds the upgrade scenario. `migrations` is the declared migration list from section 2, as a comma separated string. It is empty when the release declares none.

The workflow names its run after these inputs, as section 3 shows. The workflow imports no release-command code. It does not read the manifest, the release notes or `devtools`. The release command resolves the refs and dispatches the workflow with them. We chose inputs over a shared module so that the rehearsal tests what a user gets, not what the release tooling believes. It also means the workflow can be dispatched by hand with the same inputs.

## 2. Where a release declares its migrations

A release declares its migrations in its release notes, not in the manifest. The notes are written once per release and read by the person who cuts it. The manifest pins versions and outlives any one release, so a migration list there would go stale after the release ships.

The declaration is one line in `docs/releases/<version>.md`:

```
Migrations: <id>, <id>
```

Each id is a single token with no spaces. Ids are separated by a comma and one space. For example, in `docs/releases/0.41.0.md`:

```
Migrations: split-lake-store, rename-slot-keys
```

The gate passes `split-lake-store,rename-slot-keys` as the `migrations` input.

An absent `Migrations:` line means the release has no migrations. The migrate scenario then skips. The line `Migrations: none` says the same thing out loud and is the preferred form. A `Migrations:` line with nothing after it is an unfinished edit, not a declaration. The gate refuses it, the same way it refuses an unwritten notes paragraph.

## 3. How the gate finds a passing run

The workflow sets its run name from every input that changes what is rehearsed: the five SHAs in manifest order, the previous tag and the migration list.

```
rehearsal cartridges=<sha> graphs=<sha> tools=<sha> crew=<sha> dash=<sha> previous=<tag> migrations=<list>
```

Every `<sha>` is the full 40 character SHA. `<tag>` is the `previous_tag` input. `<list>` is the `migrations` input as passed, or `none` when that input is empty. The gate builds the same string from the commits it is about to tag, the previous release tag and the `Migrations:` line in the notes. A run dispatched with a different previous tag or a different migration list therefore has a different title and never matches. The gate then lists dispatched runs of the workflow:

```
gh run list --workflow release-rehearsal.yml --event workflow_dispatch --limit 200 --json databaseId,displayTitle,conclusion,createdAt
```

The listing asks for 200 runs, because `gh run list` returns only 20 by default and a passing run could fall out of a short window. If the listing returns exactly 200 runs and none match, the gate says the window may be full instead of saying no rehearsal exists.

A run matches when its `displayTitle` equals the built string exactly. No prefix or substring match is used, so a run for one different input never matches. When several runs match, the gate uses the newest by `createdAt`. An older pass does not rescue a newer failure, and an older failure does not block a newer pass.

A run passes when its conclusion is `success` and the three scenario jobs `fresh`, `upgrade` and `migrate` are all present in its job list. The gate reads the jobs with `gh run view <id> --json jobs`. A missing scenario job fails the run. The `fresh` and `upgrade` jobs always apply, so each must have conclusion `success`. A skipped `fresh` or `upgrade` fails the gate, even when the run as a whole concludes `success`. Only `migrate` may be reported as a job with conclusion `skipped`, and a skipped `migrate` counts as present only when the release notes declare no migrations. When migrations are declared, `migrate` must have conclusion `success`.

If no run matches, or the newest match does not pass, the gate refuses and prints the run title it looked for.

## 4. How an override is recorded

An override lets a release go out without a passing rehearsal. It needs a named reason, given to the release command as `--allow-no-rehearsal REASON`. It is recorded in the release notes, in the `## Known gaps` section, where the existing `--allow-doc-drift` override is recorded.

The existing override is free prose that the author writes by hand, and nothing in the release command reads the notes for it. The rehearsal override is stricter. It is one fixed line that the gate can match. Building that matching is work for the gate ticket, not for this page. The line is:

```
Cut with `--allow-no-rehearsal`: <reason>.
```

The reason says what was not rehearsed and why. For example:

```
Cut with `--allow-no-rehearsal`: the runner image was unavailable and the install path is unchanged since 0.40.0.
```

The line starts with `Cut with ` and the backtick-quoted flag, then a colon and one space, then the reason, then a final full stop. To compare reasons, the gate takes the text after the colon and space and before the final full stop, trims leading and trailing spaces from it, and tests it for equality with the trimmed command-line reason. A reason that itself ends in a full stop is written with that full stop followed by the line's own full stop.

The gate refuses an empty reason, and a reason that is only spaces. It also refuses when the notes lack the line, or carry a reason that differs from the one passed on the command line under that comparison. An override never waives a failed rehearsal silently. It replaces the lookup in section 3 and leaves a line in the notes that a reader can find.
