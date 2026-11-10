"""The `remote_unfetched` source of a chair tick: initiatives with a run that sits on another machine,
unfetched.

`is_remote_unfetched`, `remote_unfetched_facts` and `remote_unfetched_decompose_facts` are pure.
`read_remote_unfetched` is the edge: it finds every run of each initiative through `run_store.run_ids`
and reads the four facts that decide membership through the runs directory, mirroring the run-store and
remote-record pattern `chair_read_approved.read_fetch_facts` already uses with `remote_lane.remote_record_path`.
It also reads the remote record of each newest run, to say which initiatives are only a decompose.
"""
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_tools import run_store
from agent_tools.chair_facts import run_initiative
from agent_tools.chair_types import RemoteUnfetched
from agent_tools.remote_lane import fetched_record_path, is_decompose_record, parse_remote_record, remote_record_path

Booleans = tuple[bool, bool, bool, bool]  # has_remote_record, has_local_run_dir, has_local_log, has_fetched_marker


def is_remote_unfetched(
    has_remote_record: bool, has_local_run_dir: bool, has_local_log: bool, has_fetched_marker: bool
) -> bool:
    """A run counts as remote and unfetched only with a remote record, no fetched marker, and neither
    local trace of it having run here."""
    return has_remote_record and not has_local_run_dir and not has_local_log and not has_fetched_marker


def remote_unfetched_facts(
    runs_by_initiative: Mapping[str, Sequence[str]], booleans: Mapping[str, Booleans]
) -> dict[str, str]:
    """initiative -> newest matching run id, over every run of that initiative whose booleans (from
    `booleans`, missing is all-False) pass `is_remote_unfetched`; an initiative with no match is left out."""
    matches = {
        initiative: [run for run in runs if is_remote_unfetched(*booleans.get(run, (False, False, False, False)))]
        for initiative, runs in runs_by_initiative.items()
    }
    return {
        initiative: max(runs, key=_run_order)
        for initiative, runs in matches.items()
        if runs
    }


def remote_unfetched_decompose_facts(unfetched: Mapping[str, str], is_decompose: Mapping[str, bool]) -> list[str]:
    """Sorted initiatives whose newest unfetched run (the value in `unfetched`) is a decompose; a run missing from `is_decompose` is a build."""
    return sorted(initiative for initiative, run in unfetched.items() if is_decompose.get(run, False))


def _run_order(run: str) -> tuple[int, str]:
    """`x-10` sorts after `x-2`: by the numeric suffix, then the name. Mirrors `chair_read_approved._run_order`."""
    tail = run.rsplit("-", 1)[-1]
    return (int(tail) if tail.isdigit() else -1, run)


def runs_per_initiative(runs_dir: Path, initiatives: Sequence[str]) -> dict[str, list[str]]:
    """Edge. Every run id of each initiative, from the store's `run_ids`; an initiative with no run is left out."""
    by_initiative: dict[str, list[str]] = {initiative: [] for initiative in initiatives}
    for run in run_store.run_ids(runs_dir):
        initiative = run_initiative(run)
        if initiative in by_initiative:
            by_initiative[initiative].append(run)
    return {initiative: runs for initiative, runs in by_initiative.items() if runs}


def _is_decompose_run(runs_dir: Path, run: str) -> bool:
    """Edge. True when the run's remote record parses and names the decompose graph; unreadable is a build."""
    try:
        text = remote_record_path(runs_dir, run).read_text(encoding="utf-8")
    except OSError:
        return False
    record = parse_remote_record(text)
    return record is not None and is_decompose_record(record)


def read_remote_unfetched(runs_dir: Path, initiatives: Sequence[str]) -> RemoteUnfetched:
    """Edge. `gather_facts`'s `remote_unfetched` source: initiative id to its newest matching run id, for
    each initiative with any run that has a `<run>.remote.json` beside `runs_dir` and no local run
    directory or log for that run id. Its `decompose` lists the initiatives whose newest such run is a decompose."""
    runs_by_initiative = runs_per_initiative(runs_dir, initiatives)
    booleans = {
        run: (
            remote_record_path(runs_dir, run).exists(),
            (runs_dir / run).is_dir(),
            (runs_dir / f"{run}.log").exists(),
            fetched_record_path(runs_dir, run).exists(),
        )
        for runs in runs_by_initiative.values()
        for run in runs
    }
    unfetched = remote_unfetched_facts(runs_by_initiative, booleans)
    is_decompose = {run: _is_decompose_run(runs_dir, run) for run in unfetched.values()}
    return RemoteUnfetched(unfetched, remote_unfetched_decompose_facts(unfetched, is_decompose))
