"""Pure. A worker's git mirrors: where they live, how one is fetched, and the decisions about a mirror and a clone.

A worker may keep caches, never state, so losing one costs only time. Under the cache root, `git/<name>.git` is a
bare mirror per repository. A new clone borrows its objects (`git clone --reference`), so it fetches from the remote
only what the mirror lacks; `harness.worktree.clone_worktree` is the edge that uses it. There is no uv cache of our
own: uv's default, `~/.cache/uv`, is already one cache every lane of the user shares, and a second would only start
cold.

**gc safety.** A clone that keeps borrowing through `objects/info/alternates` breaks when the mirror prunes an
object it needs, or when the cache directory is deleted under it (the caveat in `git help gitrepository-layout`).
So no clone keeps the borrow: under the mirror's shared lock it repacks every object it can reach into its own pack
(`git repack -a -d`) and drops the alternates file, which is what `git clone --dissociate` does. Splitting the two
lets the edge count the objects the remote sent first. A clone is then independent of the mirror, and the mirror is
free to prune and gc. It does so only under its exclusive lock, never detached, so no repack runs while a clone
borrows. Deleting the cache directory changes nothing but the next clone's time.

Every function here is pure: the edge (`harness.worktree`) runs the argv this module returns.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

__all__ = [
    "CACHE_ENV",
    "LANE_CLONE",
    "MIRROR_REFSPECS",
    "CloneState",
    "MirrorStep",
    "absolute_url",
    "cache_root",
    "checkout_argv",
    "clone_argv",
    "clone_state",
    "dead_clones",
    "discard_mirror",
    "lane_clone_dir",
    "lock_file",
    "mirror_dir",
    "mirror_fetch_argv",
    "mirror_name",
    "mirror_step",
    "object_count",
    "trace_line",
]

# The folder of a lane's own clone inside its run folder under the worktree root (`lane_clone_dir`).
LANE_CLONE = "_repo"
# An explicit cache root, for an operator who keeps it off the home disk; a lane inherits it from its worker.
CACHE_ENV = "COX_CACHE_DIR"
# What a mirror keeps of its remote: every branch and tag, forced, so a rewound branch is followed rather than refused.
# Nothing else: `refs/pull/*` on a forge would bring every pull request's objects, which no lane is cut from.
MIRROR_REFSPECS = ("+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
# Any gc or maintenance a fetch starts finishes before the fetch returns, inside the exclusive lock. Detached, it would
# repack while a clone borrows under the shared lock. An unknown key is ignored by an older git.
_IN_LOCK_GC = ("-c", "gc.autoDetach=false", "-c", "maintenance.autoDetach=false")

MirrorStep = Literal["create", "rebuild", "refresh"]


def cache_root(env: Mapping[str, str], home: str) -> Path:
    """The worker's cache directory: `COX_CACHE_DIR`, else `$XDG_CACHE_HOME/coxswain`, else `~/.cache/coxswain`."""
    if env.get(CACHE_ENV):
        return Path(env[CACHE_ENV])
    base = env.get("XDG_CACHE_HOME") or str(Path(home) / ".cache")
    return Path(base) / "coxswain"


def _normalised(url: str) -> str:
    """`url` without trailing slashes or `.git`, so `…/repo`, `…/repo/` and `…/repo.git` share one mirror."""
    trimmed = url.strip().rstrip("/")
    return trimmed[: -len(".git")] if trimmed.endswith(".git") else trimmed


def mirror_name(url: str) -> str:
    """A directory name for `url`'s mirror: its last path part, made safe, and a hash that keeps distinct URLs apart."""
    normal = _normalised(url)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", re.split(r"[/:]", normal)[-1]).strip("-.") or "repo"
    return f"{stem[:40]}-{hashlib.sha256(normal.encode()).hexdigest()[:12]}"


def mirror_dir(root: Path, url: str) -> Path:
    """The bare mirror of `url` under the cache root."""
    return root / "git" / f"{mirror_name(url)}.git"


def lock_file(root: Path, url: str) -> Path:
    """The `flock` file of `url`'s mirror, beside it, so removing the mirror never removes the lock it is removed under."""
    return root / "git" / f"{mirror_name(url)}.lock"



def lane_clone_dir(worktree_root: Path, run_id: str) -> Path:
    """A phase lane's own clone: beside its worktrees, under `<worktree_root>/<run>`, which the lane removes on every
    way out and a later lane of the phase sweeps when a killed one could not. Not under the runs folder, whose every
    file the dashboard feed reads as an input."""
    return worktree_root / run_id / LANE_CLONE


def absolute_url(url: str, base: Path) -> str:
    """`url` as a clone made anywhere reaches it: a relative path remote is resolved from `base`, the checkout that
    names it; a URL with a scheme, an scp-style `host:path` and an absolute path are kept as they are."""
    if "://" in url or Path(url).is_absolute() or ":" in url.split("/", 1)[0]:
        return url
    return os.path.normpath(base / url)



def mirror_step(exists: bool, bare: bool) -> MirrorStep:
    """What the mirror needs under its exclusive lock: none yet, `create`; something there that is not a bare
    repository (a crash, a stray file, a hand edit), `rebuild`; else `refresh`."""
    if not exists:
        return "create"
    return "refresh" if bare else "rebuild"


def mirror_fetch_argv(mirror: Path, url: str) -> list[str]:
    """Bring `mirror` up to `url`: every branch and tag, pruning what the remote deleted. Pruning is safe because no
    clone keeps borrowing (the module docstring), and gc runs inside the lock (`_IN_LOCK_GC`)."""
    # `-C`, not `--git-dir`: `bounded_proc.git_subcommand` skips `-C` and `-c` only, and must read `fetch` here to
    # give the slow (remote) bound; with `--git-dir` it read the path as the subcommand and gave a cold mirror 60 s.
    return ["git", "-C", str(mirror), *_IN_LOCK_GC, "fetch", "--quiet", "--prune", "--no-write-fetch-head",
            url, *MIRROR_REFSPECS]


def clone_argv(url: str, dest: Path, reference: Path | None) -> list[str]:
    """Clone `url` into `dest` without a checkout, borrowing `reference`'s objects when there is one. `--no-local`: a
    path URL goes through git's transport too, so it borrows rather than copying every object of the source."""
    borrow = ["--reference", str(reference)] if reference is not None else []
    return ["git", "clone", "--quiet", "--no-checkout", "--no-local", *borrow, url, str(dest)]


def checkout_argv(dest: Path, branch: str | None, base: str | None) -> list[str]:
    """Fill `dest`'s work tree: on a new `branch` at `base` (default HEAD); else `base` detached; else the default branch."""
    head = ["git", "-C", str(dest), "checkout", "--quiet", "--force"]
    if branch is not None:
        return [*head, "-B", branch, base or "HEAD"]
    return [*head, "--detach", base] if base is not None else head


def object_count(count_objects_v: str) -> int:
    """The objects a repository holds itself, from `git count-objects -v`: loose plus packed, never its alternates'."""
    fields = dict(line.split(": ", 1) for line in count_objects_v.splitlines() if ": " in line)
    return sum(int(fields.get(key, "0") or 0) for key in ("count", "in-pack"))


def discard_mirror(borrowed_ok: bool, plain_ok: bool, mirror_sound: bool) -> bool:
    """Whether a mirror goes after a clone: only on evidence the mirror itself is bad. The borrowing clone failed, a
    plain clone of the same URL then worked (so the remote was fine), and the mirror's own connectivity check failed.
    A clone that failed for a local reason (a repack timeout, a full disk) leaves a sound mirror in place."""
    return not borrowed_ok and plain_ok and not mirror_sound


CloneState = Literal["absent", "present", "broken", "foreign"]


def clone_state(holds_git: bool, origin_is_url: bool, borrowing: bool, head_ok: bool, has_index: bool) -> CloneState:
    """What a destination holds before a clone into it. A clone a crash interrupted (still borrowing through its
    alternates file, no valid HEAD, or never checked out, so no index) is `broken`, cleared and cloned again; only a
    finished one is `present`. A repository of another remote is `foreign` and never touched."""
    if not holds_git:
        return "absent"
    if not origin_is_url:
        return "foreign"
    return "present" if not borrowing and head_ok and has_index else "broken"


def dead_clones(runs: Mapping[str, bool]) -> list[str]:
    """Of the runs holding a lane clone, by whether each is alive, those whose clone a lane starting now removes."""
    return sorted(run for run, alive in runs.items() if not alive)


def trace_line(url: str, mirror: str, fetched: int, received: int, borrowed: bool) -> str:
    """One line of what a clone cost: the mirror's state and the objects it fetched, and what the clone took from the
    remote itself. `received` is 0 when the mirror already held everything the clone needed."""
    how = "borrowed from the mirror" if borrowed else "plain clone"
    return f"clone {url}: mirror {mirror}, mirror fetched {fetched} object(s); {how}, received {received} object(s)"
