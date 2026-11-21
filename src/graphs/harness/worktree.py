"""Patch application, in a worktree the harness owns."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import NamedTuple

from harness import bounded_proc, repo_cache

__all__ = [
    "Cloned",
    "apply_patch",
    "clone_worktree",
    "create_worktree",
    "keep_worktree",
    "link_venv",
    "normalise_patch",
    "prune_registrations",
    "read_lock",
    "refresh_mirror",
    "registry_lock",
    "release_stale_holders",
    "remove_worktree",
    "reset_worktree",
    "stale_holders",
    "sync_env",
]

# One lock for every `git worktree` add, remove and prune this process runs. Lanes run phases on threads over
# one repository, and a `prune` in one lane can delete the `.git/worktrees/<name>` entry another lane's `add` has
# just made and not yet checked out ("gitfile does not point to a valid repository"). Re-entrant, so
# `remove_worktree` can prune under the lock it already holds.
_REGISTRY_LOCK = threading.RLock()
# Processes share the repository too (two runs, the lander, warm pool leases): an `add` reads every other
# registration, and one made by another process and not yet written fails it ("failed to read
# .git/worktrees/<name>/commondir"). So the thread lock is joined by an `flock` on a file in the repository's
# common git directory. An `flock` is per open file, so this process takes it once per repository, under the thread
# lock; a nested hold on the same thread takes nothing more.
_REGISTRY_FILE = "coxswain-worktree-registry.lock"
_held: set[str] = set()  # the lock files this process holds; read and written only under _REGISTRY_LOCK


def _registry_lock_path(repo: Path | str) -> Path | None:
    """The registry lock file of `repo`'s common git directory; None when `repo` is not a git repository."""
    found = bounded_proc.completed(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True
    )
    return Path(found.stdout.strip()) / _REGISTRY_FILE if found.returncode == 0 and found.stdout.strip() else None


@contextlib.contextmanager
def registry_lock(repo: Path | str) -> Iterator[None]:
    """Hold around any `git worktree add`, `remove` or `prune` on `repo`, so no two of them overlap in any process."""
    with _REGISTRY_LOCK:
        path = _registry_lock_path(repo)
        if path is None or str(path) in _held:
            # Nothing to lock across processes, or this thread already holds the file lock further up its stack.
            yield
            return
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _held.add(str(path))
            yield
        finally:
            _held.discard(str(path))
            os.close(fd)  # the only descriptor of this open file: closing it drops the flock


_TRAILING_MARKERS = ("</patch>", "</diff>", "</code>", "```")
_MIDDLE_TAGS = ("</patch>", "</diff>", "<patch>", "<diff>")


def normalise_patch(text: str) -> tuple[str, str | None]:
    """Strip one trailing markup marker a structured-output builder can leak.

    A build node's patch field is prose to whatever produced it, and prose
    sometimes arrives wrapped in the tags or fences the model used to talk
    about it — a closing `</patch>` glued to the last content line, or a
    stray ``` fence. `git apply` does not know that convention and fails on
    it, or worse, applies it and leaves the tag in the file. This strips
    exactly one such marker from the end, then refuses rather than guesses
    if markup is still present anywhere else: a tag mid-patch means the
    whole shape is suspect, not just its last line.
    """
    if not text:
        return text, None
    rstripped = text.rstrip("\n")
    marker = next((m for m in _TRAILING_MARKERS if rstripped.endswith(m)), None)
    # The wrong belief this trades on: that a trailing fence or tag is always
    # leaked markup and never content a hunk actually meant to add. It is
    # stripped unconditionally on that belief, same shape as the doctor
    # incident this exists for — the cost of the trade is a hunk whose true
    # last line ends the same way, which this cannot tell from a leak.
    body = rstripped[: -len(marker)].rstrip(" \t\n") if marker else rstripped
    if marker and not body:
        # A fence or tag with nothing under it is not an empty change, it is a
        # builder that returned markup instead of a patch; letting it through
        # would take the --allow-empty path and record "applied" over an
        # unchanged tree.
        return text, "builder output was markup only: no patch under the marker"
    cleaned = body + "\n" if body else ""
    for lineno, line in enumerate(cleaned.splitlines(), start=1):
        # Column zero, not substring: every diff line carries a `+`, `-`, or
        # space prefix, and every header starts with a diff keyword, so only
        # an actual tag or fence line — never a hunk that mentions one as
        # content — starts at column zero with `<` or a backtick fence.
        is_tag = line.startswith("<") and line.rstrip() in _MIDDLE_TAGS
        is_fence = line.startswith("```") and (line.rstrip() == "```" or line.startswith("```diff"))
        if is_tag or is_fence:
            return text, f"builder output contained markup at line {lineno}: {line}"
    return cleaned, None


def apply_patch(patch: str, worktree: Path) -> tuple[bool, str]:
    """Apply the build node's diff inside the worktree the harness owns.

    The one place anything in this system writes to a working tree, and it is
    the harness doing it — not the node that produced the patch. git apply
    needs a work tree, so the harness makes one it owns rather than applying
    anywhere near a real checkout: an agent that owns a worktree can be wrong
    destructively without costing anything.
    """
    cleaned, refusal = normalise_patch(patch)
    if refusal is not None:
        return False, refusal
    worktree.mkdir(parents=True, exist_ok=True)
    if not (worktree / ".git").exists():
        bounded_proc.completed(["git", "init", "-q"], cwd=worktree, capture_output=True, text=True)
    # A diff that arrives through a JSON field can lose its final newline,
    # and git rejects such a patch as corrupt at its last line even though
    # every hunk is intact. Restoring the newline changes no hunk; it only
    # lets git read the one it already has. --recount covers the sibling
    # case: a hunk header whose line count is off by one still applies,
    # because git recomputes the count from the hunk body instead of
    # trusting the header. It does not relax context matching, so a hunk
    # whose content does not fit the file still fails with git's own message.
    normalised = cleaned if cleaned.endswith("\n") or not cleaned else cleaned + "\n"
    result = bounded_proc.completed(
        ["git", "apply", "--verbose", "--allow-empty", "--recount", "-"],
        input=normalised,
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def create_worktree(repo: Path, worktree: Path, *, branch: str, base: str | None = None) -> tuple[bool, str]:
    """Check out a real worktree OF the project, not a scratch dir beside it.

    The scratch `git init` directory `apply_patch` falls back to can hold a
    diff, but it has no source tree behind it — no dependencies installed, no
    project config, nothing a check command could run against. Checks need the
    project, so they need a worktree of the actual repo, and the harness owns
    this one for the same reason it owns the scratch one: the one place
    anything here writes to a working tree is the harness, never the node that
    produced the patch.

    If `branch` already exists, this fails with git's own message rather than
    guessing a suffix — the caller is responsible for choosing a unique name,
    because silently renaming it would attach the work to the wrong branch
    without anyone deciding that.
    """
    cmd = ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree)]
    if base is not None:
        cmd.append(base)
    with registry_lock(repo):
        result = bounded_proc.completed(cmd, capture_output=True, text=True)
    return result.returncode == 0, (result.stderr or result.stdout).strip()


class Cloned(NamedTuple):
    """What `clone_worktree` did: whether `dest` holds the checkout, git's last word, and what the clone cost.

    `mirror` is the mirror's state: created, rebuilt, refreshed, stale (its fetch failed; it was borrowed as it was),
    unavailable (none could be made), or present (`dest` already held the clone, so nothing ran). `fetched` counts the
    objects the mirror took from the remote, `received` those the clone took itself."""

    ok: bool
    detail: str
    mirror: str
    fetched: int
    received: int
    borrowed: bool

    def trace(self, url: str) -> str:
        return repo_cache.trace_line(url, self.mirror, self.fetched, self.received, self.borrowed)


def _git(argv: Sequence[str], timeout: float | None = None) -> tuple[bool, str, str]:
    """Run one git argv: (succeeded, stdout, git's last word)."""
    done = bounded_proc.completed(list(argv), timeout, capture_output=True, text=True)
    return done.returncode == 0, done.stdout.strip(), (done.stderr or done.stdout).strip()


@contextlib.contextmanager
def _flock(path: Path, mode: int) -> Iterator[None]:
    """Hold an `flock` on `path` (made if missing). Each call opens the file anew, so threads exclude each other too."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, mode)
        yield
    finally:
        os.close(fd)  # the only descriptor of this open file: closing it drops the flock


def _objects(git_dir: Path) -> int:
    """The objects `git_dir` holds itself (`repo_cache.object_count`); 0 when it cannot be read."""
    ok, out, _ = _git(["git", "--git-dir", str(git_dir), "count-objects", "-v"])
    return repo_cache.object_count(out) if ok else 0


def _is_bare(path: Path) -> bool:
    """Whether `path` is itself a bare repository. `--git-dir` stops git searching the folders above it."""
    ok, out, _ = _git(["git", "--git-dir", str(path), "rev-parse", "--is-bare-repository"])
    return ok and out == "true"


def _discard(path: Path) -> None:
    """Remove `path`, whatever it is; a missing one is fine."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)


def refresh_mirror(url: str, root: Path) -> tuple[str, int, str]:
    """Edge. Bring `url`'s bare mirror under the cache `root` up to date, under its exclusive lock, and return
    (state, objects fetched, git's last word). State is one of `Cloned.mirror`'s, never present.

    Idempotent: a second call fetches only what the remote gained. A mirror is made in a scratch folder and renamed
    into place, so a crash leaves no half-made mirror where a clone would borrow it; a scratch folder a crash left is
    removed by the next holder of the lock. A path that is not a bare repository is rebuilt from nothing."""
    mirror = repo_cache.mirror_dir(root, url)
    try:
        with _flock(repo_cache.lock_file(root, url), fcntl.LOCK_EX):
            for scratch in mirror.parent.glob(f"{mirror.name}.tmp-*"):
                _discard(scratch)
            step = repo_cache.mirror_step(mirror.exists() or mirror.is_symlink(), _is_bare(mirror))
            if step == "refresh":
                before = _objects(mirror)
                ok, _, detail = _git(repo_cache.mirror_fetch_argv(mirror, url))
                return ("refreshed", max(0, _objects(mirror) - before), detail) if ok else ("stale", 0, detail)
            if step == "rebuild":
                _discard(mirror)
            scratch = mirror.with_name(f"{mirror.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
            ok, _, detail = _git(["git", "init", "--quiet", "--bare", str(scratch)])
            if ok:
                ok, _, detail = _git(repo_cache.mirror_fetch_argv(scratch, url))
            if not ok:
                _discard(scratch)
                return "unavailable", 0, detail
            fetched = _objects(scratch)
            os.rename(scratch, mirror)
            return ("created" if step == "create" else "rebuilt"), fetched, detail
    except OSError as exc:  # an unwritable or vanished cache root: the clone goes on without a mirror
        return "unavailable", 0, f"the mirror could not be used: {type(exc).__name__}: {exc}"


def _holds_clone(dest: Path, url: str) -> bool:
    """Whether `dest` is already the top of a checkout whose origin is `url`."""
    if not (dest / ".git").exists():
        return False
    ok, top, _ = _git(["git", "-C", str(dest), "rev-parse", "--show-toplevel"])
    same, origin, _ = _git(["git", "-C", str(dest), "config", "--get", "remote.origin.url"])
    return ok and same and Path(top).resolve() == dest.resolve() and origin == url


def _borrowing_clone(url: str, dest: Path, mirror: Path, branch: str | None, base: str | None) -> tuple[bool, int, str]:
    """Clone `url` into `dest` borrowing `mirror`'s objects, then stop borrowing: (ok, objects received, detail).
    Run under the mirror's shared lock, so no refresh or gc of the mirror overlaps the borrow."""
    ok, _, detail = _git(repo_cache.clone_argv(url, dest, mirror))
    if not ok:
        return False, 0, detail
    git_dir = dest / ".git"
    received = _objects(git_dir)  # before the repack: only what the remote sent, none of the mirror's
    # `repack -a` without `-l` copies every borrowed object it reaches into the clone's own pack, as `--dissociate`
    # does; only then is the alternates file dropped, and the clone no longer needs the mirror.
    ok, _, detail = _git(["git", "-C", str(dest), "repack", "-a", "-d", "-q"], bounded_proc.STEP_TIMEOUT_SECONDS)
    if not ok:
        return False, received, detail
    (git_dir / "objects" / "info" / "alternates").unlink(missing_ok=True)
    ok, _, detail = _git(repo_cache.checkout_argv(dest, branch, base))
    return ok, received, detail


def clone_worktree(url: str, dest: Path, *, cache_root: Path, branch: str | None = None,
                   base: str | None = None) -> Cloned:
    """Edge. Clone `url` into `dest` and check it out (`repo_cache.checkout_argv`), borrowing objects from the
    worker's mirror so only what the mirror lacks comes from the remote.

    The mirror is refreshed first (`refresh_mirror`), then borrowed under a shared lock, so lanes on one machine clone
    at once while a refresh waits for them. The clone never keeps the borrow (`repo_cache`'s gc note). If the mirror is
    missing, corrupt or unusable, `dest` is cleared and cloned plainly; a mirror that failed while the remote worked is
    removed, and the next call rebuilds it. A `dest` that already holds a clone of `url` is left as it is."""
    if _holds_clone(dest, url):
        return Cloned(True, f"{dest} already holds a clone of {url}", "present", 0, 0, False)
    if dest.is_symlink() or dest.is_file() or (dest.is_dir() and any(dest.iterdir())):
        return Cloned(False, f"{dest} exists and is not a clone of {url}", "unused", 0, 0, False)
    state, fetched, mirror_detail = refresh_mirror(url, cache_root)
    mirror = repo_cache.mirror_dir(cache_root, url)
    lock = repo_cache.lock_file(cache_root, url)
    borrowed = False
    if state != "unavailable":
        try:
            with _flock(lock, fcntl.LOCK_SH):
                borrowed, received, detail = _borrowing_clone(url, dest, mirror, branch, base)
        except OSError as exc:
            borrowed, received, detail = False, 0, f"{type(exc).__name__}: {exc}"
        if borrowed:
            return Cloned(True, detail, state, fetched, received, True)
        mirror_detail = detail
    _discard(dest)
    ok, _, detail = _git(repo_cache.clone_argv(url, dest, None))
    received = _objects(dest / ".git") if ok else 0
    if ok:
        ok, _, detail = _git(repo_cache.checkout_argv(dest, branch, base))
    if state != "unavailable" and repo_cache.discard_mirror(borrowed, ok):
        with contextlib.suppress(OSError), _flock(lock, fcntl.LOCK_EX):
            _discard(mirror)
    why = f"{detail}\n(mirror not used: {mirror_detail})" if mirror_detail else detail
    return Cloned(ok, why.strip(), state, fetched, received, False)


def link_venv(repo: Path, worktree: Path) -> bool:
    """Link `worktree/.venv` to the repo's `.venv` so `uv run --frozen` finds it; True if linked.

    A `.venv/` ignore pattern matches directories only, and git sees this link
    as a file, so `git add -A` would stage it into every patch. `/.venv` goes
    into the shared `info/exclude` first; the main checkout ignores it too.
    """
    venv, link = Path(repo) / ".venv", Path(worktree) / ".venv"
    if not venv.is_dir() or link.exists() or link.is_symlink():
        return False
    common = bounded_proc.completed(["git", "-C", str(worktree), "rev-parse", "--git-common-dir"], capture_output=True, text=True)
    if common.returncode != 0:
        return False
    exclude = Path(worktree) / common.stdout.strip() / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if "/.venv" not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join([*lines, "/.venv"]) + "\n", encoding="utf-8")
    link.symlink_to(venv.resolve())
    return True


def prune_registrations(repo: Path) -> tuple[bool, str]:
    """Drop any `git worktree` administrative entry whose directory is gone.

    A crashed run can leave `.git/worktrees/<name>` behind after the worktree
    directory itself is deleted or moved out from under it; git's own registry
    then blocks the next `worktree add` on the same branch or path with a
    phantom "already exists" until this runs.
    """
    with registry_lock(repo):
        result = bounded_proc.completed(
            ["git", "-C", str(repo), "worktree", "prune", "--verbose"],
            capture_output=True,
            text=True,
        )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def remove_worktree(repo: Path, worktree: Path) -> tuple[bool, str]:
    """Delete a worktree and clear its registration, for every exit path.

    Tries `git worktree remove` first so the registry and the directory come
    off together; falls back to a plain `rmtree` for a directory that was
    never registered — the scratch `git init` dir `apply_patch` falls back to
    when there is no real repo behind a patch. Either way, `prune_registrations`
    runs last so a stale entry never survives this call.
    """
    with registry_lock(repo):
        result = bounded_proc.completed(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
            text=True,
        )
    detail = (result.stderr or result.stdout).strip()
    if result.returncode != 0 and worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
        detail = f"not a registered worktree, removed directly: {detail}" if detail else "removed directly"
    prune_ok, prune_detail = prune_registrations(repo)
    combined = f"{detail}\n{prune_detail}".strip() if prune_detail else detail
    return (not worktree.exists()) and prune_ok, combined


def reset_worktree(worktree: Path, *, branch: str, base: str) -> tuple[bool, str]:
    """Put a registered worktree on a fresh `branch` at `base`, clean of untracked files; ignored paths survive."""
    steps = (
        ["checkout", "-f", "-B", branch, base],
        ["reset", "--hard", base],
        ["clean", "-fd"],
    )
    messages: list[str] = []
    for args in steps:
        result = bounded_proc.completed(["git", "-C", str(worktree), *args], capture_output=True, text=True)
        detail = (result.stderr or result.stdout).strip()
        if result.returncode != 0:
            return False, detail
        messages.append(detail)
    return True, "\n".join(m for m in messages if m)


def read_lock(worktree: Path, names: Sequence[str] = ("uv.lock",)) -> bytes | None:
    """Bytes of the first of `names` that exists in the worktree, else None."""
    found = next((p for p in (Path(worktree) / n for n in names) if p.is_file()), None)
    return None if found is None else found.read_bytes()


def sync_env(worktree: Path, argv: Sequence[str]) -> tuple[bool, str]:
    """Run the caller's environment sync `argv` with the worktree as cwd; the command is never named here."""
    result = bounded_proc.completed(list(argv), bounded_proc.STEP_TIMEOUT_SECONDS, cwd=worktree, capture_output=True, text=True)
    return result.returncode == 0, "\n".join(s for s in (result.stdout.strip(), result.stderr.strip()) if s)


def stale_holders(porcelain: str, branch: str, worktree_root: Path, alive: Callable[[str], bool]) -> list[Path]:
    """Paths under `<worktree_root>/<run_id>/<phase>` holding `branch` whose run_id fails `alive`; outside-root holders never."""
    ref = f"refs/heads/{branch}"
    records = [
        dict(line.split(" ", 1) for line in record.splitlines() if " " in line)
        for record in porcelain.split("\n\n")
    ]
    held = [Path(r["worktree"]) for r in records if r.get("branch") == ref and "worktree" in r]
    return [
        path
        for path in held
        if path.is_relative_to(worktree_root)
        and path != worktree_root
        and not alive(path.relative_to(worktree_root).parts[0])
    ]


def release_stale_holders(repo: Path, branch: str, worktree_root: Path, alive: Callable[[str], bool]) -> list[Path]:
    """Remove every worktree `stale_holders` finds for `branch`; the branch itself is kept. Returns the removed paths."""
    listing = bounded_proc.completed(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if listing.returncode != 0:
        return []
    return [
        path
        for path in stale_holders(listing.stdout, branch, worktree_root, alive)
        if remove_worktree(repo, path)[0]
    ]


def keep_worktree(repo: Path, worktree: Path, worktree_root: Path, run_id: str) -> tuple[bool, str]:
    """Move a worktree under `<worktree_root>/_kept/<run_id>` instead of deleting it.

    For `--keep-worktrees`: the directory survives for post-mortem, moved out
    from under its own registration so the branch and path it used are free
    for the next run, and `prune_registrations` clears the now-stale entry.
    """
    dest = worktree_root / "_kept" / run_id / worktree.name
    if not worktree.exists():
        return False, f"nothing to keep at {worktree}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(worktree), str(dest))
    prune_ok, prune_detail = prune_registrations(repo)
    detail = f"kept at {dest}\n{prune_detail}".strip() if prune_detail else f"kept at {dest}"
    return dest.exists() and prune_ok, detail
