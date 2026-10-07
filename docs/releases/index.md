# Releases

What changes between versions and how the lockstep tag scheme works.

## How a release works

From 0.15.0, a release is cut from the umbrella checkout with:

```
uv run --frozen python -m devtools release <version> --root ~/repos --manifest ~/repos/coxswain/manifest.toml --umbrella ~/repos/coxswain
```

0.14.x and earlier were cut with `cox dev release`. `--frozen` keeps `uv run` from rewriting
`uv.lock` when a component's version moves, since a dirty umbrella refuses the cut.

That one command tags every component repository and this repository at
`<version>`, bumps `manifest.toml` to match, and publishes both the docs
site at that version (via `mike`) and the `cox` package to PyPI (via
trusted publishing — no long-lived token in this repository's secrets).
Every component in a release carries the same tag, so `manifest.toml`
always tells you exactly what a given release installs.

### The tap pull request and the install proof

A cut that tags the tools repository ends with the `tap_formula_pr` step. It bumps
`Formula/cox.rb` in the `ppfenning/homebrew-coxswain` tap to the new PyPI sdist on a
release branch and opens a pull request there. The tap is what `brew install` reads, so
this pull request is the last gate between a tag and a Homebrew user.

Before anyone merges it, the step dispatches `.github/workflows/install-proof.yml`
against that branch as `tap_ref` and waits for the one run it started. The proof runs on
two platforms: macOS arm64 on `macos-14`, and a Linux Homebrew container, `homebrew/brew`
on `ubuntu-latest`. On each it runs `brew install ppfenning/coxswain/cox` to install the
previous release and checks that the installed version is the one tagged just below the
candidate. It stages the candidate formula from the tap branch, then runs
`brew upgrade cox` and fails if the version did not move to the candidate. On a temporary
profile, with `HOME` and the `XDG_*` directories pointed at a fresh directory, it runs
`cox --version` and `cox setup doctor --json` and checks both outputs. Then it runs
`brew uninstall cox` and `brew install ppfenning/coxswain/cox` again, and repeats the same
two checks on the clean install.

The release flow never merges the tap pull request. If the proof's conclusion is anything
other than `success`, which is what `tap_may_merge` tests, the command prints the
conclusion and the run URL, leaves the pull request open and exits non-zero. After a pass,
you merge the tap pull request by hand.

A proof that cannot fail proves nothing, so `.github/workflows/install-proof-negative.yml`
runs weekly and on dispatch. It feeds the proof the deliberately broken formula
`tests/devtools/broken_cox.rb`. Its own run is green only when the proof failed, so a
green run there means the proof still catches a broken formula.

A green proof means that formula installs and upgrades on those two platforms today. It
does not mean the release is otherwise good.

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
