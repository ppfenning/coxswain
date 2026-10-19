# cartridges

Who a run works for. This folder is the `core` package and the cartridge data
inside the coxswain repository; it ships as part of the one `coxswain`
package. To install Coxswain, see the [root README](../../README.md).

Graphs reference abstract **roles** (`plan`, `build`, `review_charter`). A
**cartridge** binds those roles to real skills, names where writes land, and
carries the team's own written conventions. The same graph runs for any team
that can fill the contract: swapping trackers, style charters or model
providers is an edit to one file, not a rewrite.

## What is here

```
core/                pure substrate: merge, policy, manifest, ledger, work store, intake
cartridges/base/     the contract: roles, write-kind taxonomy, autonomy policy
cartridges/local/    the filesystem-only cartridge every install starts from
cartridges/<name>/   example-team, and the comfort presets local-comfort0 and local-comfort1
providers/           tier -> model, one profile per provider (claude-code.yaml is the default)
skills-plugins/      the reference `local-skills` plugin and the `coxswain` session plugin
context-templates/   starting points for a team's own context packs
.claude-plugin/      the Claude Code plugin marketplace, named coxswain-cartridges
docs/                design notes and the clean-room rule
tests/               this folder's suite
```

A workspace's `cartridges/base` and `cartridges/local` are links into
`cartridges/` here, so they move with the checkout's release.

## Tests

From this folder:

```sh
cd src/cartridges && uv run pytest -q
```

Fixtures are synthetic. The suite leans hardest on the ways autonomy must
*fail* to be granted: streaks that do not transfer across kinds, risks,
cartridge hashes or provider profiles, and a single reversal resetting the
streak and doubling the bar.

## Working with a cartridge

Resolve and validate the `local` cartridge from this folder:

```sh
uv run python -m core.cartridge --team local --json --skills-root skills-plugins
```

`--skills-root` checks that every bound skill name resolves to exactly one
skill body; pass it once per plugin root.

Start your own team's cartridge in your workspace:

```sh
cartridge init my-team --cartridges-dir <workspace>/cartridges
```

It creates `my-team/`, its `context/` tree, links to `base` and `local`, and a
`cartridge.yaml` naming the team. `cox install` registers the marketplace at
`<checkout>/src/cartridges` and installs `local-skills@coxswain-cartridges`
when `claude` is on `PATH`.

The docs site explains the rest: [cartridges](https://ppfenning.github.io/coxswain/latest/methodology/cartridges/),
[team cartridge](https://ppfenning.github.io/coxswain/latest/customize/team-cartridge/)
and [policy](https://ppfenning.github.io/coxswain/latest/customize/policy/),
including how a write kind earns autonomy.

## License

MIT. See [LICENSE](LICENSE).
