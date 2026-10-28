# Releases

Coxswain is one repository, [ppfenning/coxswain](https://github.com/ppfenning/coxswain),
and one package, `coxswain`. A release is one tag on that repository, one
PyPI publish, one GitHub release and one Homebrew formula update.

## Versions

From 2026.10.0, versions are CalVer: `YYYY.M.N`, tagged `vYYYY.M.N`.

- `YYYY` is the year and `M` the month, without a leading zero.
- `N` counts the releases cut in that month, from 0.
- The first monorepo release is 2026.10.0. The next in October would be
  2026.10.1. The first in November would be 2026.11.0, not 2026.11.00.

The number says when a release was cut. It does not say whether it is
compatible with the one before. Compatibility is carried by the store and
workspace schema, which `cox setup doctor` checks, and by
`cox setup migrate`, which moves a machine across a change in layout.

0.47.0 was the last component release. Releases before it used SemVer-style
`0.x` numbers across several repositories; they are listed below under
[Component releases (0.x)](#component-releases-0x). See
[From 0.47 to the monorepo](../monorepo.md) for what changed and how to move.

## How a release works

A release is cut from the coxswain checkout:

```
uv run --frozen python -m devtools release <version>
```

Add `--dry-run` first to print the plan without running it. `--frozen` keeps `uv run` from rewriting `uv.lock`, since a dirty checkout
refuses the cut. `--root` is where the dash and `homebrew-coxswain` checkouts
sit; it defaults to the checkout's parent.

The cut runs these gates first. Any one refuses it:

1. The notes page `docs/releases/<version>.md` exists and has no blank left
   in it.
2. Every tree the release tags passes the clean check
   (`.github/check_clean.py`).
3. A [release rehearsal](../release-rehearsal.md) passed on the checkout's
   `origin/main` commit. `--allow-no-rehearsal REASON` waives it, and the
   notes must carry the same reason.
4. `release-check` finds no drift between the CLI, the manifest, the docs
   and the notes. `--allow-doc-drift REASON` waives it.
5. Every checkout the release tags is clean, on its default branch, and
   level with `origin/<default>`.

Freeze the chair's lands for the cut, so nothing merges under it:
`cox chair freeze-lands --for 60 --reason "cutting <version>"`.

Then the steps:

1. When `<version>` is above the manifest's version, bump `pyproject.toml`,
   `uv.lock` and `manifest.toml` on `release/<version>`, open a pull request,
   wait for its checks and merge it.
2. Tag `v<version>` on the repository. The tag push runs `publish.yml`,
   which publishes `coxswain` to PyPI by trusted publishing. It refuses a tag
   that is not `v` plus the CalVer version in `pyproject.toml`. The release
   waits for that workflow.
3. Create the GitHub release `coxswain <version>` from the notes page.
4. Open the tap formula pull request, and run the install proof against it
   (below).

After a cut, or a failed one, the command sends a `release_cut` or
`release_failed` push to the profile's `notify.ntfy` topic, when one is
set.

Dash lives in its own repository. A release tags it at `v<version>` only when
it has commits since the tag `manifest.toml` names; otherwise dash keeps its
tag. Nothing else is pinned: the checkout is the release.

The docs site is deployed by `docs.yml` on every push to `main`, under the
manifest's version and `latest`.

### The tap pull request and the install proof

Every cut ends with the `tap_formula_pr` step. It bumps `Formula/cox.rb` in
the `ppfenning/homebrew-coxswain` tap to the new PyPI sdist on a release
branch and opens a pull request there. The tap is what `brew install` reads,
so this pull request is the last gate between a tag and a Homebrew user.
`--tap-only` runs this step alone, for a version already tagged.

Before anyone merges it, the step dispatches `.github/workflows/install-proof.yml`
against that branch as `tap_ref` and waits for the one run it started. The
proof is the clean-machine rehearsal. It runs on two platforms: macOS arm64
on `macos-14`, and a Linux Homebrew container, `homebrew/brew` on
`ubuntu-latest`. On each it runs `brew install ppfenning/coxswain/cox` to
install the previous release and checks that the installed version is the one
tagged just below the candidate. It stages the candidate formula from the tap
branch, then runs `brew upgrade cox` and fails if the version did not move to
the candidate. On a temporary profile, with `HOME` and the `XDG_*`
directories pointed at a fresh directory, it runs `cox --version` and
`cox setup doctor --json` and checks both outputs. Then it runs
`brew uninstall cox` and `brew install ppfenning/coxswain/cox` again, and
repeats the same two checks on the clean install.

The release flow never merges the tap pull request. If the proof's conclusion
is anything other than `success`, which is what `tap_may_merge` tests, the
command prints the conclusion and the run URL, leaves the pull request open
and exits non-zero. After a pass, you merge the tap pull request by hand.

A proof that cannot fail proves nothing, so
`.github/workflows/install-proof-negative.yml` runs weekly and on dispatch. It
feeds the proof the deliberately broken formula `tests/devtools/broken_cox.rb`.
Its own run is green only when the proof failed, so a green run there means
the proof failed on that one formula. It does not show the proof catches
every broken formula.

A green proof means that formula installs and upgrades on those two platforms
today. It does not mean the release is otherwise good.

## `2026.10.4`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| dash | `ppfenning/coxswain-dash` | `v2026.10.2` | flag: `dash`, provides `towpath` (pinned) |

See the [2026.10.4 release notes](2026.10.4.md) for what landed in each component.

## `2026.10.3`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| dash | `ppfenning/coxswain-dash` | `v2026.10.2` | flag: `dash`, provides `towpath` (pinned) |

See the [2026.10.3 release notes](2026.10.3.md) for what landed in each component.

## `2026.10.2`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| dash | `ppfenning/coxswain-dash` | `v2026.10.2` | flag: `dash`, provides `towpath` |

See the [2026.10.2 release notes](2026.10.2.md) for what landed in each component.

## `2026.10.1`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| dash | `ppfenning/coxswain-dash` | `v2026.10.1` | flag: `dash`, provides `towpath` |

See the [2026.10.1 release notes](2026.10.1.md) for what landed in each component.

## `2026.10.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| dash | `ppfenning/coxswain-dash` | `v0.42.0` | flag: `dash`, provides `towpath` (pinned) |

See the [2026.10.0 release notes](2026.10.0.md) for what landed in each component.

## Component releases (0.x)

From 0.1.0-beta.1 to 0.47.0, Coxswain shipped as separate repositories —
cartridges, graphs, tools, crew and dash — tagged together from the
`coxswain` repository and pinned by `manifest.toml`. Each table below lists
the tag every component was pinned to in that release. A component marked
"pinned" kept an earlier tag because it had not changed.

## `0.47.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.40.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.43.0` | required (pinned) |
| tools | `ppfenning/coxswain-tools` | `v0.47.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.38.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.42.0` | flag: `dash`, provides `towpath` (pinned) |

See the [0.47.0 release notes](0.47.0.md) for what landed in each component.

## `0.46.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.40.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.43.0` | required (pinned) |
| tools | `ppfenning/coxswain-tools` | `v0.46.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.38.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.42.0` | flag: `dash`, provides `towpath` (pinned) |

See the [0.46.0 release notes](0.46.0.md) for what landed in each component.

## `0.45.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.40.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.43.0` | required (pinned) |
| tools | `ppfenning/coxswain-tools` | `v0.45.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.38.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.42.0` | flag: `dash`, provides `towpath` (pinned) |

See the [0.45.0 release notes](0.45.0.md) for what landed in each component.

## `0.44.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.40.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.43.0` | required (pinned) |
| tools | `ppfenning/coxswain-tools` | `v0.44.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.38.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.42.0` | flag: `dash`, provides `towpath` (pinned) |

See the [0.44.0 release notes](0.44.0.md) for what landed in each component.

## `0.43.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.40.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.43.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.43.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.38.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.42.0` | flag: `dash`, provides `towpath` (pinned) |

See the [0.43.0 release notes](0.43.0.md) for what landed in each component.

## `0.42.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.40.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.42.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.42.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.38.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.42.0` | flag: `dash`, provides `towpath` |

See the [0.42.0 release notes](0.42.0.md) for what landed in each component.

## `0.41.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.40.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.40.0` | required (pinned) |
| tools | `ppfenning/coxswain-tools` | `v0.41.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.38.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.41.0` | flag: `dash`, provides `towpath` |

See the [0.41.0 release notes](0.41.0.md) for what landed in each component.

## `0.40.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.40.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.40.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.40.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.38.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.40.0` | flag: `dash`, provides `towpath` |

See the [0.40.0 release notes](0.40.0.md) for what landed in each component.

## `0.39.1`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.39.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.39.0` | required (pinned) |
| tools | `ppfenning/coxswain-tools` | `v0.39.1` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.38.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.38.0` | flag: `dash`, provides `towpath` (pinned) |

See the [0.39.1 release notes](0.39.1.md) for what landed in each component.

## `0.39.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.39.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.39.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.39.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.38.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.38.0` | flag: `dash`, provides `towpath` (pinned) |

See the [0.39.0 release notes](0.39.0.md) for what landed in each component.

## `0.38.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.37.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.37.0` | required (pinned) |
| tools | `ppfenning/coxswain-tools` | `v0.38.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.38.0` | flag: `crew` |
| dash | `ppfenning/coxswain-dash` | `v0.38.0` | flag: `dash`, provides `towpath` |

See the [0.38.0 release notes](0.38.0.md) for what landed in each component.

## `0.37.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.37.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.37.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.37.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.32.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.37.0` | flag: `dash`, provides `towpath` |

See the [0.37.0 release notes](0.37.0.md) for what landed in each component.

## `0.36.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.36.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.36.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.36.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.32.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.36.0` | flag: `dash`, provides `towpath` |

See the [0.36.0 release notes](0.36.0.md) for what landed in each component.

## `0.35.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.35.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.35.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.35.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.32.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.35.0` | flag: `dash`, provides `towpath` |

See the [0.35.0 release notes](0.35.0.md) for what landed in each component.

## `0.34.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.32.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.33.0` | required (pinned) |
| tools | `ppfenning/coxswain-tools` | `v0.34.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.32.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.34.0` | flag: `dash`, provides `towpath` |

See the [0.34.0 release notes](0.34.0.md) for what landed in each component.

## `0.33.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.32.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.33.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.33.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.32.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.33.0` | flag: `dash`, provides `coxtop` |

See the [0.33.0 release notes](0.33.0.md) for what landed in each component.

## `0.32.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.32.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.32.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.32.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.32.0` | flag: `crew` |
| dash | `ppfenning/coxswain-dash` | `v0.32.0` | flag: `dash`, provides `coxtop` |

See the [0.32.0 release notes](0.32.0.md) for what landed in each component.

## `0.31.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.31.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.31.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.31.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.31.0` | flag: `crew` |
| dash | `ppfenning/coxswain-dash` | `v0.31.0` | flag: `dash`, provides `coxtop` |

See the [0.31.0 release notes](0.31.0.md) for what landed in each component.

## `0.30.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.29.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.30.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.30.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.28.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.30.0` | flag: `dash`, provides `coxtop` |

See the [0.30.0 release notes](0.30.0.md) for what landed in each component.

## `0.29.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.29.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.29.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.29.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.28.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.28.0` | flag: `dash`, provides `coxtop` (pinned) |

See the [0.29.0 release notes](0.29.0.md) for what landed in each component.

## `0.28.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.23.1` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.28.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.28.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.28.0` | flag: `crew` |
| dash | `ppfenning/coxswain-dash` | `v0.28.0` | flag: `dash`, provides `coxtop` |

See the [0.28.0 release notes](0.28.0.md) for what landed in each component.

## `0.27.1`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.23.1` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.27.1` | required |
| tools | `ppfenning/coxswain-tools` | `v0.27.1` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.27.1` | flag: `dash`, provides `coxtop` |

See the [0.27.1 release notes](0.27.1.md) for what landed in each component.

## `0.27.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.23.1` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.26.0` | required (pinned) |
| tools | `ppfenning/coxswain-tools` | `v0.27.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.26.0` | flag: `dash`, provides `coxtop` (pinned) |

See the [0.27.0 release notes](0.27.0.md) for what landed in each component.

## `0.26.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.23.1` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.26.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.26.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |
| dash | `ppfenning/coxswain-dash` | `v0.26.0` | flag: `dash`, provides `coxtop` |

See the [0.26.0 release notes](0.26.0.md) for what landed in each component.

## `0.25.1`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.23.1` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.25.1` | required |
| tools | `ppfenning/coxswain-tools` | `v0.25.1` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.25.1 release notes](0.25.1.md) for what landed in each component.

## `0.25.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.23.1` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.25.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.25.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.25.0 release notes](0.25.0.md) for what landed in each component.

## `0.24.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.23.1` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.24.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.24.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.24.0 release notes](0.24.0.md) for what landed in each component.

## `0.23.1`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.23.1` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.23.1` | required |
| tools | `ppfenning/coxswain-tools` | `v0.23.1` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.23.1 release notes](0.23.1.md) for what landed in each component.

## `0.23.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.21.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.23.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.23.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.23.0 release notes](0.23.0.md) for what landed in each component.

## `0.22.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.21.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.21.0` | required (pinned) |
| tools | `ppfenning/coxswain-tools` | `v0.22.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.22.0 release notes](0.22.0.md) for what landed in each component.

## `0.21.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.21.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.21.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.21.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.21.0 release notes](0.21.0.md) for what landed in each component.

## `0.20.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.20.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.20.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.20.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.20.0 release notes](0.20.0.md) for what landed in each component.

## `0.19.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.18.0` | required (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.19.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.19.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.19.0 release notes](0.19.0.md) for what landed in each component.

## `0.18.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.18.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.18.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.18.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.18.0 release notes](0.18.0.md) for what landed in each component.

## `0.17.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.17.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.17.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.17.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.17.0 release notes](0.17.0.md) for what landed in each component.

## `0.16.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.16.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.16.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.16.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.16.0 release notes](0.16.0.md) for what landed in each component.

## `0.15.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.15.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.15.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.15.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.15.0 release notes](0.15.0.md) for what landed in each component.

## `0.14.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.14.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.14.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.14.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.14.0 release notes](0.14.0.md) for what landed in each component.

## `0.13.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.13.0` | required |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |
| graphs | `ppfenning/coxswain-graphs` | `v0.13.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.13.0` | required, provides `cox` |

See the [0.13.0 release notes](0.13.0.md) for what landed in each component.

## `0.12.1`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.12.1` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.12.1` | required |
| tools | `ppfenning/coxswain-tools` | `v0.12.1` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` (pinned) |

See the [0.12.1 release notes](0.12.1.md) for what landed in each component.

## `0.12.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.12.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.12.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.12.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.12.0` | flag: `crew` |

See the [0.12.0 release notes](0.12.0.md) for what landed in each component.

## `0.11.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.11.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.11.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.11.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.7.0` | flag: `crew` (pinned) |

See the [0.11.0 release notes](0.11.0.md) for what landed in each component.

## `0.10.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.10.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.10.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.10.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.7.0` | flag: `crew` (pinned) |

See the [0.10.0 release notes](0.10.0.md) for what landed in each component.

## `0.9.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.9.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.9.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.9.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.7.0` | flag: `crew` (pinned) |

See the [0.9.0 release notes](0.9.0.md) for what landed in each component.

## `0.8.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.8.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.8.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.8.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.7.0` | flag: `crew` (pinned) |

See the [0.8.0 release notes](0.8.0.md) for what landed in each component.

## `0.7.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.7.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.7.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.7.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.7.0` | flag: `crew` |

See the [0.7.0 release notes](0.7.0.md) for what landed in each component.

## `0.6.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.6.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.6.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.6.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.6.0` | flag: `crew` |

See the [0.6.0 release notes](0.6.0.md) for what landed in each component.

## `0.5.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.5.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.5.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.5.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.5.0` | flag: `crew` |

See the [0.5.0 release notes](0.5.0.md) for what landed in each component.

## `0.4.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.4.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.4.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.4.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.4.0` | flag: `crew` |

See the [0.4.0 release notes](0.4.0.md) for what landed in each component.

## `0.3.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.3.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.3.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.3.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.3.0` | flag: `crew` |

See the [0.3.0 release notes](0.3.0.md) for what landed in each component.

## `0.2.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.2.0` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.2.0` | required |
| tools | `ppfenning/coxswain-tools` | `v0.2.0` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.2.0` | flag: `crew` |

See the [0.2.0 release notes](0.2.0.md) for what landed in each component.

## `0.1.0-beta.1`

The manifest for the first release, transcribed by hand from
`manifest.toml`:

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.1.0-beta.1` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.1.0-beta.1` | required |
| tools | `ppfenning/coxswain-tools` | `v0.1.0-beta.1` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.1.0-beta.1` | flag: `crew` |

See the [0.1.0-beta.1 release notes](0.1.0-beta.1.md) for what landed in
each component.
