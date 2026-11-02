"""The restart rule: a chair on a development install restarts when its checkout's HEAD moves on a branch.

A detached install never restarts, and an unreadable checkout is no reason to restart.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# EX_TEMPFAIL: differs from the overrun exit 3 and from 0, 1 and 2.
RESTART_EXIT_CODE = 75
STAY = "stay"
WAIT = "wait"
RESTART = "restart"
_SHORT = 12


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


def checkout_root() -> Path:
    """The editable install points at the checkout this file sits in: src/tools/agent_tools/ is three levels down."""
    return Path(__file__).resolve().parents[3]
