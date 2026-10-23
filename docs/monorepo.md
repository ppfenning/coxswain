# From 0.47 to the monorepo

0.47.0 was the last component release. 2026.10.0 is the first monorepo
release. This page says what changed for you and how to move a
machine across.

## What changed

**One repository.** Everything lives in
[ppfenning/coxswain](https://github.com/ppfenning/coxswain). The tools, the
graphs and harness, the cartridges and the crew are folders in it:

| Folder | Was |
| --- | --- |
| `src/tools` | `coxswain-tools` |
| `src/graphs` | `coxswain-graphs` |
| `src/cartridges` | `coxswain-cartridges` |
| `crew` | `coxswain-crew` |

The component repositories get no further releases. coxswain-dash
(`towpath`, Rust) stays its own repository and is still the optional
`--with dash`.

**One package.** PyPI has one distribution, `coxswain`. It replaces
`coxswain-tools` and the `agent-cartridges` uv tool. The command is still
`cox`. The Homebrew formula is still `cox` in the `ppfenning/coxswain` tap.

**One checkout per machine.** A 0.47 machine had a checkout per component
under `~/.local/share/coxswain/installs/` (`coxswain-tools`,
`coxswain-graphs`, `coxswain-cartridges`, `coxswain-crew`) beside the
`coxswain` checkout. Now there is one checkout,
`~/.local/share/coxswain/installs/coxswain`, with one venv at
`<checkout>/.venv`. `cox` is installed editable from that checkout.

**Team provider profiles live outside the checkout.** The checkout ships
the default provider profiles under `src/cartridges/providers/`. Your team's
own profile, the one with its store, notifier and operator, lives in the
workspace or in `<home>/providers/`, and the machine profile's
`provider_profile` names it. See [Install](install/index.md#the-provider-profile).

**Crew is always linked.** `cox install` and `cox upgrade` link the crew
seats every time. There is no `--with crew` any more.

**CalVer.** Versions are `YYYY.M.N`: the year, the month, and a count of
releases in that month, starting at 0. 2026.10.0 is the first; a November
release would be 2026.11.0. The number says when a release was cut, not
whether it is compatible. Compatibility is carried by the store and workspace
schema and by `cox setup migrate`.

## Move a machine

Do this on each machine: the chair's machine and every lane host. Stop the
chair loop and let the lanes on a machine finish before you move it.

1. Drain the host, if it is in the hosts table and active.

    ```sh
    cox host drain <host>
    ```

    `cox setup migrate --apply` refuses an active host with
    `migrate: refusing, host <host> holds active state, run cox host drain <host> first`.

2. Move a team provider profile out of the old cartridges checkout. If the
   profile's `provider_profile` names your team's own file inside
   `installs/coxswain-cartridges`, copy it (and `catalog.yaml` beside it)
   to `<home>/providers/`, then point `provider_profile` at the copy.

    ```sh
    mkdir -p <home>/providers
    cp <home>/installs/coxswain-cartridges/providers/<team>.yaml <home>/providers/
    cp <home>/installs/coxswain-cartridges/providers/catalog.yaml <home>/providers/
    ```

    Since 2026.10.1, migrate does the profile part itself when the cartridges
    checkout does not track your team's file: it copies the file to
    `<home>/providers/` and points `provider_profile` at the copy. It does not
    copy `catalog.yaml`. A file it could not check against the checkout is
    left for you to move by hand. A profile outside the component checkouts
    is kept as it is.

3. Update `cox` to the new package.

    ```sh
    brew upgrade cox
    ```

    Or, if you installed with uv, remove both old tools first. The
    `agent-cartridges` tool owns a `cartridge` executable, and the new
    package refuses to install over it.

    ```sh
    uv tool uninstall coxswain-tools agent-cartridges
    uv tool install --python 3.14 'coxswain[postgres,traces,parquet,lake]'
    ```

    Name the extras this machine uses. A Postgres store needs `postgres`;
    the extras are `traces`, `postgres`, `parquet`, `lake`, `analytics` and
    `live`. `--python 3.14` is needed when uv's default Python is older.

4. See the plan. This changes nothing.

    ```sh
    cox setup migrate
    ```

    It prints one numbered line per step and ends with
    `no changes made; run with --apply to migrate`.

5. Apply it.

    ```sh
    cox setup migrate --apply
    ```

6. Edit your own scripts by hand. Migrate does not rewrite operator
   scripts, systemd units, drop-ins or launch agents that name an old
   checkout path. When this machine's state folder holds a `migrate.json`
   marker naming an old root, the apply lists them, one line each:
   `migrate: <file> still names <path>; edit it by hand`. It looks only in
   the home folder, `~/.config/systemd/user` and its `*.d` drop-ins,
   `~/Library/LaunchAgents` and the state folders, at `.sh`, `.py`, `.zsh`,
   `.service`, `.timer`, `.plist` and `.conf` files. Search anywhere else
   yourself, for example:

    ```sh
    grep -rlE 'installs/coxswain-(tools|graphs|cartridges|crew)' ~/bin ~/.config 2>/dev/null
    ```

7. Check the machine, then put the host back to work.

    ```sh
    cox setup doctor
    cox host activate <host>
    ```

8. After every lane host has migrated, finish.

    ```sh
    cox setup migrate --finish
    cox setup migrate --finish --apply
    ```

On the chair's machine, `cox setup migrate --apply --hosts` also runs
`cox setup migrate --apply` on each lane host over ssh. Update `cox` on those
hosts first. Without `--hosts`, the apply prints
`hosts: not touched; rerun with --apply --hosts on the chair's machine`.

## Known issues in 2026.10.0

All three are fixed in 2026.10.1; they apply only to a migrate run with
2026.10.0.

- **`repo_map` written back in a form the profile reader rejects.** The
  profile reader takes `repo_map` as one line of JSON. If a command then
  fails on that line, rewrite it as JSON:
  `repo_map: {"<name>": "<path>"}`.
- **Migration leases not always released.** The apply takes a lease on each
  old checkout path. One left behind makes the next apply refuse with
  `migrate: refusing, <holder> holds <path>`. It expires an hour after it
  was taken; wait, then run the apply again.
- **A shallow 0.47 umbrella clone.** If the `coxswain` checkout was cloned
  shallow, migrate may not find the release tag or may fail to sync. Fetch
  the tags and sync the checkout first, then run migrate again:

    ```sh
    git -C <home>/installs/coxswain fetch --tags
    (cd <home>/installs/coxswain && uv sync)
    ```

## What `cox setup migrate` does

The plan comes from the machine as it is. In order:

1. The `coxswain` checkout at `installs/coxswain` moves to the newest release
   tag (or `--to VERSION`). If it is missing, it is cloned at that tag.
2. The checkout's venv is synced.
3. Profile keys that name a component checkout move into the checkout:
   `harness_dir` to `<checkout>/src/graphs`, `skills_roots` and
   `provider_profile` under `<checkout>/src/cartridges`. A provider profile
   outside the component checkouts is your team's own and is kept.
   `umbrella_dir` is set to the checkout. A `repo_map` value that names a
   component checkout follows it to its retired folder.
4. The workspace's `cartridges/base` and `cartridges/local` links are
   re-pointed at `<checkout>/src/cartridges/cartridges/<name>`.
5. The Claude plugin marketplace is registered again at
   `<checkout>/src/cartridges` and its plugins reinstalled. When `claude` is
   not on `PATH`, this step is skipped and the plan says so.
6. The `coxswain-tools` and `agent-cartridges` uv tools are uninstalled.
   `coxswain` is installed editable from the checkout with the extras the old
   tools had.
7. A chair systemd unit, or a drop-in, whose `PATH` names a component
   checkout is rewritten.
8. Each component checkout moves to `installs/.retired-<date>/<name>`, and its
   worktrees are repaired there. Nothing is deleted. Remove the retired folder
   yourself once you no longer need it.

After the steps, the apply prints a line for each of your own files that
still names an old checkout (`migrate: <file> still names <path>; edit it by
hand`), records a host beat, and ends with `verify with: cox setup doctor`.

A machine that has already moved plans one `skip` step, so running it again
is safe.

### When it refuses

The plan refuses, and plans nothing else, when:

- `installs/coxswain` is a file or a link, not a checkout;
- the `coxswain` checkout has uncommitted changes (commit or stash first);
- no release tag can be found to move to (pass `--to`);
- the workspace cartridge links, or a component checkout's worktrees, could
  not be read.

`--apply` also takes a lease on each old checkout path, and refuses while
this machine is busy:

- another holder has one of those leases
  (`migrate: refusing, <holder> holds <path>`);
- a live lane holds a run here;
- the chair holds its lease here;
- this host's row in the hosts table is active. Run `cox host drain <name>`
  first, and `cox host activate <name>` after the move.

Stop the loop and the lanes on a machine before you migrate it.

### What `--finish` does

`--finish` waits until this machine's plan is a single `skip`, its home
checks pass, and every active host has reported a beat from the new layout.
Until then it prints `finish: waiting on ...` lines.

It then finishes a move from an old checkout root outside the home, the
layout installs used before 0.44.0. It rewrites the queue's repository paths
under that root, removes the compatibility links left there, and deletes the
migration marker. Without `--apply` it only prints what it would do.

If this machine never lived under such a root, there is nothing to finish:
it prints `finish: no old root known; pass --old-root`.

## After the move

- Upgrade with `brew upgrade cox` (or `uv tool upgrade coxswain`), then
  `cox upgrade`.
- `cox versions` shows the checkout's release and dash's tag against
  `manifest.toml`.
- Each folder runs its own tests: `cd src/tools && uv run pytest -q`, and the
  same for `src/graphs` and `src/cartridges`.

See [Install](install/index.md) for a fresh machine and
[Releases](releases/index.md) for the version list.
