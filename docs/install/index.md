# Install

How to install the components pinned by `manifest.toml` with the `cox`
command.

```sh
uv tool install --python 3.14 coxswain-tools
git clone --branch vX.Y.Z https://github.com/ppfenning/coxswain <home>/installs/coxswain
cox install --team my-team --with crew
mkdir -p <workspace>/work <workspace>/runs <workspace>/intake <workspace>/cartridges
git -C <workspace> init
cartridge init my-team --cartridges-dir <workspace>/cartridges
cox setup doctor
```

Here `vX.Y.Z` is a release tag and `<home>` is the coxswain home: `--home`, else `COXSWAIN_HOME`, else
`$XDG_DATA_HOME/coxswain`, else `~/.local/share/coxswain`. Components are installed under `<home>/installs`, and
the workspace is `<workspace>` = `<home>/workspaces/main` unless you pass `--workspace`. The doctor that
`cox install` runs at its close may report the missing workspace and cartridge until the `mkdir`, `git init` and
`cartridge init` steps are done.

To install somewhere else instead, pass `--root <root>` and `--workspace <workspace>` (both are then required) and
clone the umbrella to `<root>/coxswain`. An install made before 0.44.0 under `--root` keeps working; to move it under
the home, see [Moving an install under the home](#moving-an-install-under-the-home).

The `--python 3.14` flag makes the installer fetch a 3.14 interpreter when none is present, so you do not silently get a release from before the Python 3.14 floor. If your shell cannot find the installed `cox` command, run `uv tool update-shell` and then open a new shell.

`cox install` reads the manifest at `<home>/installs/coxswain/manifest.toml` (or `<root>/coxswain/manifest.toml`
with `--root`), which is why the umbrella repository is cloned there first.

The first command needs `uv` already on your `PATH`; `cox install` does not
install it for you. Get `uv` from <https://docs.astral.sh/uv/> before you
start. On macOS you can install `cox` from the Homebrew tap instead; see
[macOS](#macos). Either way, `cox install` is the step that fetches the rest.

## What `cox install` does

1. Reads `manifest.toml` from the pinned release to find out which
   components exist and which tag each one is pinned to.
2. Fetches the required components (cartridges, graphs, `cox` itself) at
   their pinned tag into `<home>/installs`, or into `--root` when given.
3. Fetches whichever optional components `--with` named (crew) at their
   pinned tags. A release tags only the components that changed, as
   `v<version>`; unchanged components keep their earlier tag. Crew, for
   example, is pinned at `v0.32.0`.
4. Writes the provider config for `--provider`, so `cox` knows which CLI,
   plugin, and model tiers to use.

## Channels

`manifest.toml` pins each component to one of two channels, never a mix.

| Channel | Pin | For | Command |
| --- | --- | --- | --- |
| release | `tag = "vX.Y.Z"` | a fresh install, stable | `cox install` |
| edge | `ref = "main"` | tracking main daily | `cox install --edge` |

## macOS

```sh
brew tap ppfenning/coxswain
brew trust ppfenning/coxswain
brew install cox
cox setup doctor
```

Current Homebrew refuses a formula from a third-party tap until you trust the tap, once.
The formula installs `cox` alone; `cox install` fetches the rest of the
pinned components from `manifest.toml`.

## Flags

Run `cox install --help` for the flags your installed version accepts. The
table below lists the ones this page documents.

| Flag | Meaning |
| --- | --- |
| `--root` | Where to install components instead of `<home>/installs`. The umbrella is cloned to `<root>/coxswain` first. |
| `--home` | Absolute path of the coxswain home, used when `--root` is omitted. |
| `--manifest` | Path to the `manifest.toml` to read instead of the umbrella's. |
| `--provider` | Which provider profile to configure (see [Providers](providers.md)). |
| `--with crew` | Optional components to install alongside the required ones. Repeatable. |
| `--team` | Which team's cartridge to pull in. |
| `--workspace` | The workspace root the crew will operate on; `<home>/workspaces/main` by default, required with `--root`. |
| `--edge` | Track `main` instead of the pinned release tags. |
| `--dry-run` | Print what `cox install` would do without doing it. |

## Verify

```sh
cox setup doctor
```

`cox setup doctor` checks that every required component is present, pinned to the
version `manifest.toml` expects, and that the configured provider's CLI is
reachable on `PATH`.

## What you'll see

<img alt="cox versions --manifest manifest.toml listing each pinned component next to its installed tag" src="../assets/shots/versions.svg">

`cox versions` lists every component next to the tag `manifest.toml` pins, so drift is visible before you upgrade.

<img alt="cox setup doctor confirming every required component and provider CLI is reachable" src="../assets/shots/doctor.svg">

`cox setup doctor` is what the checks above look like against a real install.

## Where personal setup goes

Personal setup never goes in a shipped repository. It lives in one of two homes.

- **The team cartridge** holds the cast, the policy, the inline repos and the context packs. A cartridge named `example-team` is one such home.
- **The provider profile** holds the operator, the store and the notifier. A profile named `example-provider` is one such home.

## Upgrade

```sh
cox upgrade
```

`cox upgrade` re-reads the manifest for the latest release and moves each
component to the tag that manifest pins. Components that changed in the
release are tagged `v<version>`; unchanged components keep their tag.

## Moving an install under the home

`cox setup migrate` prints the plan for moving an existing install under the home and changes nothing.
`cox setup migrate --apply` moves this machine; run `cox setup doctor` afterwards. On the chair's machine,
`cox setup migrate --apply --hosts` also moves each lane host. Until a machine is migrated, `cox setup doctor` warns
on the home rows with `not migrated: run cox setup migrate`.

## Uninstall

There is no `cox uninstall` yet. Removing an install means deleting
the coxswain home (or `--root`) by hand; a dedicated uninstall command is planned.

See [Supported machines](machines.md) for prerequisites on specific
platforms.
