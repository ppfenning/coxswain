# Dash

Dash is towpath, a terminal dashboard for the fleet: lanes, the chair, the
queue and spend on one screen, with a drill-down into any run. It reads
`cox dash --feed`, so it shows what the store shows. coxtop was its name
through 0.34, and the coxtop alias lasts until 0.37.

## What it owns

The dash repository owns the `towpath` binary (Rust and ratatui), its themes
and its keymap. Every action it offers is a `cox` verb; it holds no state of
its own beyond the view you left it on.

## Installing it

Dash is optional: nothing else needs it. It joined the release in 0.26.0 as
a beta preview. Changed components are tagged `v<version>`; unchanged ones
keep their tag. Install it one of three ways, in this order.

**Release asset.** Each dash release ships prebuilt binaries named
`towpath-<tag>-<target>.tar.gz`, since 0.31. Download the one for your target
from the release page of
[coxswain-dash](https://github.com/ppfenning/coxswain-dash/releases) and put
`towpath` on your `PATH`.

**Homebrew.** From 0.36 the formula in the Coxswain tap installs `towpath`
along with `cox`:

```sh
brew tap ppfenning/coxswain
brew trust ppfenning/coxswain
brew install cox
```

**Cargo, as the fallback.** If neither of those fits your platform, build from
source at the tag of your Coxswain release (the crate is `coxswain-dash`; it
installs `towpath`):

```sh
cargo install --git https://github.com/ppfenning/coxswain-dash --tag <release tag>
```

## Its own docs

The dash repository's own README lives at
[github.com/ppfenning/coxswain-dash](https://github.com/ppfenning/coxswain-dash).

## Reference

The component's README at the pinned tag is included below when the site is
built from a release.
