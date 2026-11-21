"""D1's merge agent: a new-model phase merge that conflicts is put to L5's `merge_tasks` seat, through the local harness.

The lander merges a phase's branch into its initiative branch (`landing_git.merge_into`). When that merge conflicts on
the new model, the conflicted merge is left in its scratch worktree and `resolve` is asked for it:

1. no `merge_tasks` seat is bound (`Seat` is None): `lander_hold.NO_MERGE_AGENT`, the retryable "no merge agent yet"
   hold, as before L5;
2. else the seat gets L5's brief (`lane_merge.brief`): the phase, the initiative branch it merges into, the phase's
   whole change since the two parted, and each conflicted file with both sides between markers. Its answer is read
   the way L5 reads it (`lane_merge.resolution`): each conflicted file's whole merged text, or why it is not a merge;
3. the call is bounded (`AGENT_TIMEOUT_S`): the agent, then the guards and the level checks, all run under the
   initiative's branch lease (`landing_ops.CLAIM_SECONDS`, 900 s). An agent that runs past its bound raises
   `TimeoutError`, which `landing_git._settle` records as a resolver error: retried, a bounded number of times.

L5's guards (`has_markers`, diff3 included, and `lost_lines` against the index stages) and D1's own index check run on
the answer in `landing_git._settle`, then the level checks in `merge_into`: this module only asks.

`brief` and `answer` are pure; `resolve` and `bounded` are the edge, given the seat as a callable.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agent_tools import lander, lander_hold
from harness import landing_git as lg
from harness import lane_merge

# The merge agent's whole answer. The branch lease is 900 s (`landing_ops.CLAIM_SECONDS`) and holds the scratch merge,
# this call, the guards, the level checks and the push: the agent gets about half of it.
AGENT_TIMEOUT_S = 450.0
# One call to the `merge_tasks` seat: (its prompt, the label the answer is keyed by) -> its answer.
Seat = Callable[[str, str], Mapping[str, object]]

__all__ = ["AGENT_TIMEOUT_S", "Seat", "answer", "bounded", "brief", "label", "resolve"]


def label(ask: lander.ConflictAsk) -> str:
    """Pure. The task label the seat's call carries (a scripted runner's `merge_tasks@<label>`): `<initiative>/<phase>`."""
    return f"{ask.initiative}/{ask.phase}"


def brief(ask: lander.ConflictAsk, sides: Mapping[str, str], change: str) -> str:
    """Pure. L5's brief for one phase merge: the phase is the later side, the initiative branch (holding the phases
    merged before it) the side already landed, which the conflict markers' first half holds."""
    return lane_merge.brief(f"phase {ask.phase}", f"phase {ask.phase} of {ask.initiative}", ask.branch,
                            f"the initiative branch, holding the phases of {ask.initiative} merged before it", sides,
                            change)


def answer(reply: Mapping[str, object], paths: Sequence[str]) -> lg.Resolved | str:
    """Pure. The seat's reply as the merge it answers (`lane_merge.resolution`), or why it is not one."""
    merged = lane_merge.resolution(reply, paths)
    if isinstance(merged, str):
        return merged
    summary = " ".join(str(reply.get("summary") or "").split())[:200]
    return lg.Resolved(merged, f"the {lane_merge.ROLE} seat" + (f" ({summary})" if summary else ""))


def bounded[T](call: Callable[[], T], timeout_s: float) -> T:
    """Edge. `call()`'s result, or `TimeoutError` once `timeout_s` has passed. What `call` raises is raised here. A call
    still running at the bound is left on its daemon thread; the runner's own timeout is set to the same bound, so a
    live runner's subprocess is killed by the runner itself."""
    done: list[T] = []
    failed: list[BaseException] = []

    def run() -> None:
        try:
            done.append(call())
        except BaseException as exc:  # handed to the caller's thread, raised there
            failed.append(exc)

    worker = threading.Thread(target=run, name="lander-merge-agent", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise TimeoutError(f"the {lane_merge.ROLE} seat did not answer within {timeout_s:g}s")
    if failed:
        raise failed[0]
    return done[0]


def _change(scratch: Path) -> str:
    """Edge. The phase's whole change since it parted from the initiative branch: the merge base to MERGE_HEAD."""
    base_ok, base = lg.git(scratch, "merge-base", "HEAD", "MERGE_HEAD")
    if not base_ok:
        return ""
    ok, diff = lg.git(scratch, "diff", "--no-renames", base, "MERGE_HEAD")
    return diff if ok else ""


def resolve(ask: lander.ConflictAsk, seat: Seat | None, *, timeout_s: float = AGENT_TIMEOUT_S) -> lg.Resolved | str:
    """Edge. Ask the `merge_tasks` seat to merge the conflicted files of `ask`, within `timeout_s`. No seat is the
    retryable "no merge agent yet". The seat raising, or running past the bound, raises: a resolver error."""
    if seat is None:
        return lander_hold.NO_MERGE_AGENT
    sides = {p: (ask.scratch / p).read_text(encoding="utf-8", errors="replace") if (ask.scratch / p).is_file() else ""
             for p in ask.paths}
    prompt = brief(ask, sides, _change(ask.scratch))
    return answer(bounded(lambda: seat(prompt, label(ask)), timeout_s), ask.paths)
