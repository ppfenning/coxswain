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

## `0.1.0-beta.1`

The manifest for the first release, transcribed by hand from
`manifest.toml`:

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| cartridges | `ppfenning/coxswain-cartridges` | `v0.1.0-beta.1` | required |
| graphs | `ppfenning/coxswain-graphs` | `v0.1.0-beta.1` | required |
| tools | `ppfenning/coxswain-tools` | `v0.1.0-beta.1` | required, provides `cox` |
| crew | `ppfenning/coxswain-crew` | `v0.1.0-beta.1` | flag: `crew` |
| hud | `ppfenning/coxswain-hud` | `v0.1.0-beta.1` | flag: `hud` |
| desktop | `desktop/` (this repository) | — | flag: `desktop` |

See the [0.1.0-beta.1 release notes](0.1.0-beta.1.md) for what landed in
each component.
