"""The restart rule: a chair on a development install restarts when its checkout's HEAD moves on a branch.

A detached install never restarts, and an unreadable checkout is no reason to restart.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

# EX_TEMPFAIL: differs from the overrun exit 3 and from 0, 1 and 2.
RESTART_EXIT_CODE = 75
STAY = "stay"
WAIT = "wait"
RESTART = "restart"
_SHORT = 12
_CHAIR_CODE_PREFIXES = ("src/tools/agent_tools/", "src/graphs/", "src/cartridges/core/")


@dataclass(frozen=True)
class CheckoutState:
    head: str
    on_branch: bool


@dataclass(frozen=True)
class RestartDecision:
    state: str


def restart_decision(started: CheckoutState, current: CheckoutState | None, lands_in_flight: bool) -> RestartDecision:
    """Restart or wait only when both states are on a branch and the heads differ. `None` is an unreadable checkout."""
    if current is None or not (started.on_branch and current.on_branch):
        return RestartDecision(STAY)
    if started.head == current.head:
        return RestartDecision(STAY)
    return RestartDecision(WAIT if lands_in_flight else RESTART)


def restart_line(old: str, new: str) -> str:
    return f"chair restarting because the checkout moved from {old[:_SHORT]} to {new[:_SHORT]}"


def read_checkout_state(checkout: Path, run: Callable[[list[str]], str | None]) -> CheckoutState | None:
    """Edge. `run` takes a git argument list and returns stdout, or None on failure."""
    head = run(["-C", str(checkout), "rev-parse", "HEAD"])
    if head is None or not head.strip():
        return None
    branch = run(["-C", str(checkout), "symbolic-ref", "-q", "HEAD"])
    return CheckoutState(head=head.strip(), on_branch=branch is not None and bool(branch.strip()))


@dataclass(frozen=True)
class Pending:
    head: str
    first_seen: float
    last_change: float


def chair_code_paths(paths: Sequence[str]) -> list[str]:
    """Repo-relative paths under the chair's code directories, excluding `.md` files and any `tests` directory."""
    return [
        p
        for p in paths
        if p.startswith(_CHAIR_CODE_PREFIXES) and not p.endswith(".md") and "tests" not in PurePosixPath(p).parts[:-1]
    ]


def touches_chair_code(paths: Sequence[str]) -> bool:
    return bool(chair_code_paths(paths))


def changed_paths(checkout: Path, old: str, new: str, run: Callable[[list[str]], str | None]) -> list[str] | None:
    """Edge. `run` takes a git argument list and returns stdout, or None on failure; None here means an unreadable diff."""
    out = run(["-C", str(checkout), "diff", "--name-only", old, new])
    return None if out is None else [line.strip() for line in out.splitlines() if line.strip()]


def coalesce(
    pending: Pending | None, current_head: str, now: float, window_s: float, max_defer_s: float
) -> tuple[bool, Pending | None]:
    """Answer True once `current_head` has held for `window_s`, or `max_defer_s` after `first_seen`. `now` is monotonic seconds."""
    if pending is None:
        return False, Pending(current_head, now, now)
    updated = pending if pending.head == current_head else replace(pending, head=current_head, last_change=now)
    settled = now - updated.last_change >= window_s
    overdue = now - updated.first_seen >= max_defer_s
    return settled or overdue, updated


def restart_reason(old: str, new: str, paths: Sequence[str] | None) -> str:
    heads = f"{old[:_SHORT]} to {new[:_SHORT]}"
    if paths is None:
        return f"checkout moved {heads}, diff unreadable"
    return f"checkout moved {heads}, {len(chair_code_paths(paths))} chair code files changed"


def checkout_root() -> Path:
    """The editable install points at the checkout this file sits in: src/tools/agent_tools/ is three levels down."""
    return Path(__file__).resolve().parents[3]
