# Dash

Dash is towpath, a terminal dashboard for the fleet: lanes, the chair, the
queue and spend on one screen, with a drill-down into any run. It reads
`cox dash --feed`, so it shows what the store shows. Its name through 0.34
was coxtop.

## What it owns

The dash repository owns the `towpath` binary (Rust and ratatui), its themes
and its keymap. Every action it offers is a `cox` verb; it holds no state of
its own beyond the view you left it on.

## Installing it

Dash is optional: nothing else needs it. It lives in its own repository and
is tagged on its own: a Coxswain release tags dash only when dash changed,
and `manifest.toml` names the tag a release uses. Install it one of these
ways.

**With `cox install`.** Pass `--with dash`.

**Homebrew.** The `cox` formula installs `towpath` along with `cox`:

```sh
brew install ppfenning/coxswain/cox
```

**Release asset.** Each dash release ships prebuilt binaries named
`towpath-<tag>-<target>.tar.gz`. Download the one for your target from the
release page of
[coxswain-dash](https://github.com/ppfenning/coxswain-dash/releases) and put
`towpath` on your `PATH`.

**Cargo, as the fallback.** Build from source at the tag `manifest.toml`
names (the crate is `coxswain-dash`; it installs `towpath`):

```sh
cargo install --git https://github.com/ppfenning/coxswain-dash --tag <dash tag>
```

## Its own docs

The dash repository's own README lives at
[github.com/ppfenning/coxswain-dash](https://github.com/ppfenning/coxswain-dash).
