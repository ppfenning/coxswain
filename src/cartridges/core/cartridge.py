"""Cartridge loader and validator: resolve base + team into one merged config.

CONTRACT (implement against this; see docs/CLEAN-ROOM.md — write it fresh)

    load(team, cartridges_dir) -> dict

Merge semantics:

1.  A team cartridge declares `extends: <parent>`. Resolve the chain to the
    root, then deep-merge parent-under-child. Child wins on scalar conflicts.
2.  `context` lists CONCATENATE, base-first. A team pack refines a base
    principle; it does not replace it. Order is the reading order.
3.  Resolved `context` entries are ABSOLUTE paths. Graph scripts have no
    filesystem access — they pass paths to agent nodes, which read them.
4.  Emit `cartridge_dir` and `cartridge_sha` on the resolved dict. The sha
    covers the merged config AND every context pack's content: changing a
    charter changes the hash, which resets autonomy streaks.

Validation — refuse to resolve, loudly, when:

-   a REQUIRED role from the base is unbound
-   a team TIGHTENS nothing but LOOSENS a risk or ramp the base declared
-   a bound skill name does not resolve to exactly one skill body
-   a context path does not exist
-   a write kind names an apply_arm role that is not bound

Fail at load, never at run. A graph that discovers a missing binding halfway
through a production sweep has already done half the damage.

CLI: `python -m core.cartridge --team <name> --json` prints the resolved
cartridge, which is what a shell injects into a graph's `args.cartridge`.
There is deliberately no inline fallback anywhere in a graph — a fallback means
the seam never gets exercised and quietly rots.
"""

from __future__ import annotations

__all__ = ["load"]


def load(*args, **kwargs):  # noqa: D401 - contract stub
    raise NotImplementedError(
        "Implement from the contract in this module's docstring. "
        "Do not port a prior employer's implementation — see docs/CLEAN-ROOM.md."
    )
