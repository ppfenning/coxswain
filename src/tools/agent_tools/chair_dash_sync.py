"""Pure planner for the towpath rebuild after a land in the dash checkout.

No file, process, network, clock or environment access. Callers pass in what they observed
and the runner that executes the argvs.
"""

from collections.abc import Callable

from agent_tools import dash_binary


def _leaf(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def same_checkout(landed_repo: str, dash_dir: str | None) -> bool:
    """Compares last path components only; no realpath, no filesystem."""
    return dash_dir is not None and _leaf(landed_repo) == _leaf(dash_dir)


def rebuild_argvs(landed_repo: str, dash_dir: str | None, bin_dir: str, toolchain_found: bool) -> list[list[str]]:
    """Pull, build, link; the pull is safe to repeat because the land already updated the default branch."""
    if dash_dir is None or not toolchain_found or not same_checkout(landed_repo, dash_dir):
        return []
    return [
        ["git", "-C", dash_dir, "pull", "--ff-only"],
        dash_binary.dev_build_argv(dash_dir),
        *dash_binary.dev_link_argvs(dash_dir, bin_dir),
    ]


def run_argvs(argvs: list[list[str]], run: Callable[[list[str]], tuple[int, str]]) -> int:
    """Runs in order and stops at the first non-zero exit, returning it; 0 when all succeed."""
    for argv in argvs:
        code, _ = run(argv)
        if code != 0:
            return code
    return 0
