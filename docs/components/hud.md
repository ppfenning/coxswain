# HUD

The HUD is a live view of in-flight runs and the docket: what's dispatched,
what's waiting, and what each run's ledger says so far.

## What it owns

The HUD repository owns the web view onto the docket and the ledgers — it
reads them, it never writes to them. The coxswain stays the single writer
of the docket's state.

## Installing it

HUD is optional. Install it with:

```
cox install --with hud
```

## Its own docs

The HUD repository's own README and docs live at
[github.com/ppfenning/coxswain-hud](https://github.com/ppfenning/coxswain-hud).

## Reference

The component's README at the pinned tag is included below when the site is
built from a release.
