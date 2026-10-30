"""The branch a repo lands on, resolved once for every caller.

Order: the `--base` flag, then the repo's own `base:` setting, then the branch `origin/HEAD` names, then `main`.

The `base:` setting lives in the repo's cartridge overlay, `<repo>/.agent/cartridge.yaml`, as a top-level `base:` or as
`repos.<directory name>.base`. A profile `repo_map` value is a path string that `review_pr` and `source_folder` read, and
the team cartridge needs a `team` and `cartridges_dir` that `runs stranded` and the carry-forward do not have.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

Run = Callable[[list[str]], tuple[int, str]]

DEFAULT_BASE = "main"
OVERLAY = Path(".agent") / "cartridge.yaml"


class BaseError(Exception):
    """The base cannot be used: origin is unreachable or lacks `origin/<base>`."""


def resolve_base(flag: str | None, configured: str | None, origin_head: str | None, default: str = DEFAULT_BASE) -> str:
    """The first non-empty of flag, configured and origin_head, an `origin/` prefix dropped, else `default`."""
    named = (str(c).strip().removeprefix("origin/") for c in (flag, configured, origin_head) if c)
    return next((name for name in named if name), default)


def base_setting(overlay: Mapping[str, Any] | None, repo_name: str) -> str | None:
    """`repos.<repo_name>.base`, else the top-level `base`, of a parsed overlay; None when neither is a non-empty string."""
    overlay = overlay or {}
    repos = overlay.get("repos")
    entry = repos.get(repo_name) if isinstance(repos, Mapping) else None
    for value in (entry.get("base") if isinstance(entry, Mapping) else None, overlay.get("base")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def run_git(argv: list[str]) -> tuple[int, str]:
    """Edge. Run `argv`; its exit code and stdout then stderr, 127 when it cannot start."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return done.returncode, done.stdout + done.stderr


def configured_base(repo: str) -> str | None:
    """Edge. The repo's `base:` setting from its overlay file; None when the file is absent, unreadable or silent."""
    try:
        overlay = yaml.safe_load((Path(repo).expanduser() / OVERLAY).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return base_setting(overlay if isinstance(overlay, Mapping) else None, Path(repo).expanduser().name)


def origin_head(repo: str, run: Run = run_git) -> str | None:
    """Edge. The branch `origin/HEAD` names in `repo`; None when it names nothing."""
    code, out = run(["git", "-C", repo, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"])
    return out.strip().removeprefix("origin/") or None if code == 0 else None


def has_origin(repo: str, run: Run = run_git) -> bool:
    """Edge. True when `repo` has a remote named origin."""
    code, out = run(["git", "-C", repo, "remote"])
    return code == 0 and "origin" in out.split()


def base_name(repo: str, flag: str | None = None, configured: str | None = None, run: Run = run_git) -> str:
    """Edge. The bare base branch name for `repo`; no fetch."""
    return resolve_base(flag, configured if configured is not None else configured_base(repo), origin_head(repo, run))


def base_ref(repo: str, flag: str | None = None, configured: str | None = None, run: Run = run_git) -> str:
    """Edge. `origin/<base>` after `git fetch origin`; the bare name when `repo` has no origin remote.

    Raises BaseError when the fetch fails or `origin/<base>` does not resolve: a land onto a base nobody chose is worse than none."""
    name = base_name(repo, flag, configured, run)
    if not has_origin(repo, run):
        return name
    code, out = run(["git", "-C", repo, "fetch", "origin"])
    if code != 0:
        raise BaseError(f"git fetch origin in {repo} exited {code}: {out.strip()[:120]}")
    ref = f"origin/{name}"
    if run(["git", "-C", repo, "rev-parse", "--verify", "-q", f"refs/remotes/{ref}"])[0] != 0:
        raise BaseError(f"{ref} does not resolve in {repo}; pass --base or set base: in {OVERLAY}")
    return ref
