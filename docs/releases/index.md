# Releases

What changes between versions and how the lockstep tag scheme works.

## How a release works

A release is cut with:

```
cox release <version>
```

That one command tags every component repository and this repository at
`<version>`, bumps `manifest.toml` to match, and publishes both the docs
site at that version (via `mike`) and the `cox` package to PyPI (via
trusted publishing — no long-lived token in this repository's secrets).
Every component in a release carries the same tag, so `manifest.toml`
always tells you exactly what a given release installs.

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
