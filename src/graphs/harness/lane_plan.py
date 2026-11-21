"""The phase lane's schedule, pure: which tasks start now, which can never start, and how many may build at once.

A task starts only when every task of its phase it needs has rolled up (its commit is on the phase branch on origin),
so a task always builds on the tip its needs left. A need outside the phase is an earlier phase's, already in the
phase's base, and never waits. Nothing here reads a clock, a store or git: `dag` maps each task the lane builds to its
needs, and the sets say where each task stands.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from harness.lane_workers import lane_workers

__all__ = ["blocked", "next_tasks", "threads"]


def _inside(dag: Mapping[str, Sequence[str]], task: str) -> list[str]:
    return [n for n in dag[task] if n in dag]


def next_tasks(
    dag: Mapping[str, Sequence[str]], rolled_up: Collection[str], building: Collection[str], threads_free: int,
    failed: Collection[str] = (),
) -> list[str]:
    """The tasks to start now, by id, at most `threads_free`: not rolled up, not building, not failed, and every need
    inside the phase rolled up. None while no thread is free."""
    if threads_free <= 0:
        return []
    done, busy, bad = set(rolled_up), set(building), set(failed)
    ready = [
        t for t in sorted(dag)
        if t not in done and t not in busy and t not in bad and all(n in done for n in _inside(dag, t))
    ]
    return ready[:threads_free]


def blocked(dag: Mapping[str, Sequence[str]], failed: Collection[str]) -> list[str]:
    """The tasks that can never start in this lane because a task they need, directly or through others, failed;
    sorted. The failed tasks themselves are not listed."""
    bad = set(failed)
    out: set[str] = set()
    changed = True
    while changed:
        changed = False
        for task in dag:
            if task not in bad and task not in out and any(n in bad or n in out for n in _inside(dag, task)):
                out.add(task)
                changed = True
    return sorted(out)


def threads(host_threads: int, cores: int, lanes: int) -> int:
    """Task builds one lane runs at once: the machine's thread budget (`hosts.threads`, where 0 means its cores)
    split evenly among the lanes it runs, never below 1."""
    budget = host_threads if host_threads > 0 else cores
    return lane_workers(budget, lanes)
