"""C6: the tick's loop chores are the housekeeping worker's (and the reasons file the launch worker's) once no tick runs.

The worker loop is the real one (`chair_queue.run`), and the chores are the real ones (`chair_worker_chores`) on the
tick's own steps (`chair_run`); only the edges of `RunDeps` (the pull, the lane hosts, the chat file, the export) are
recorded stand-ins.
"""

from __future__ import annotations

import dataclasses

from agent_tools import chair_exec, chair_queue, chair_report, chair_worker_chores, mcp_ports
from agent_tools.chair_run import ChatDeps, RunDeps
from agent_tools.chair_types import Facts
from agent_tools.notify_core import NotifyConfig
from tests.test_chair_queue_alarms import KINDS, _facts, _now

DIRTY = {
    "kind": "needs_chair", "host": "box-a", "cause": "lane_checkout_dirty",
    "reason": "lane host box-a, checkout /srv/repo: uncommitted changes",
}


class Rig:
    """The worker's `RunDeps` with every chore's edge recorded: pulls, lane syncs, chat reads, exports and records."""

    def __init__(self, tmp_path, *, pull: str = "", lanes=(DIRTY,), chat=()):
        self.tmp_path = tmp_path
        self.pulls = 0
        self.exports: list[tuple[str, ...]] = []
        self.recorded: list[dict] = []
        self.acked: list[str] = []
        self.chat_reads = 0
        self.pull, self.lanes, self.chat = pull, list(lanes), list(chat)
        self.stamp: object = ("rows", 1)
        self.head: str | None = "c0"

    def _pull(self) -> str:
        self.pulls += 1
        return self.pull

    def _read_chat(self) -> list:
        self.chat_reads += 1
        return list(self.chat)

    def _export(self, ids: tuple[str, ...]) -> str:
        self.exports.append(ids)
        return f"export: wrote {len(ids)}"

    def deps(self) -> RunDeps:
        exec_deps = chair_exec.Deps(
            run=lambda argv: (0, ""), delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda holder, host: "",
            record=self.recorded.append, run_id=lambda action: "run-1", repo_for=lambda action: "/srv/repo",
        )
        chat = ChatDeps(
            read=self._read_chat, load=lambda: {}, save=lambda state, epics: None,  # type: ignore[arg-type, return-value]
            append=lambda entry: None, ack=self.acked.append,
        )
        return RunDeps(
            facts_deps=object(), exec_deps=exec_deps, report_deps=chair_report.Deps(echo=lambda line: None, notify=None),
            beat=lambda: None, current_epoch=lambda: 7, holds=lambda: True, release=lambda: None, sleep=lambda s: None,
            now=_now, gather=lambda _d, _n: {}, plan=lambda facts, now: [],
            notify=lambda *a: None, notify_config=NotifyConfig(""), workspace=self.tmp_path, runs_dir=self.tmp_path,
            pull_workspace=self._pull, lane_checkout_actions=lambda: list(self.lanes), export_rows=self._export,
            chat=chat, meter_doc=lambda: None,
        )

    def chores(self, deps: RunDeps) -> chair_queue.Chores:
        return chair_worker_chores.housekeeping_chores(deps, lambda: self.stamp, lambda: self.head)


def _ports(facts: Facts, chores: chair_queue.Chores | None, performed: list, pushed: list, lines: list) -> chair_queue.Ports:
    clock = iter(float(n) for n in range(1000))
    return chair_queue.Ports(
        queued=lambda: frozenset(KINDS), acquire=lambda: 7, renew=lambda e: True, holds=lambda e: e == 7,
        release=lambda e: None, gather=lambda at: facts,
        perform_one=lambda a, c: performed.append(a) or [{"action": a, "status": "recorded", "reason": ""}],
        wait=lambda s: frozenset(), pause=lambda: None, clock=lambda: next(clock), now=_now, echo=lines.append,
        notify=lambda kind, f, actions: pushed.append([a.get("cause") for a in actions]) or "", chores=chores,
    )


def _pass(kind: str, facts: Facts, chores: chair_queue.Chores | None) -> tuple[list, list, list]:
    performed: list = []
    pushed: list = []
    lines: list = []
    ports = _ports(facts, chores, performed, pushed, lines)
    assert chair_queue.run(kind, ports, chair_queue.Timing(poll=60, settle=0), once=True, dry_run=False) == 0
    return performed, pushed, lines


def test_the_chores_run_on_a_live_pass_of_a_listed_kind_only_while_no_tick_holds_the_chair_lease():
    assert chair_queue.chores_due("housekeeping", _facts(frozenset(KINDS)), dry_run=False)
    assert not chair_queue.chores_due("housekeeping", _facts(frozenset(KINDS), tick=True), dry_run=False)
    assert not chair_queue.chores_due("housekeeping", _facts(frozenset(KINDS)), dry_run=True)
    assert not chair_queue.chores_due("housekeeping", _facts(frozenset(KINDS), listed=("watch",)), dry_run=False)


def test_housekeeping_pulls_records_the_lane_line_under_its_epoch_pushes_it_and_exports(tmp_path):
    rig = Rig(tmp_path)
    performed, pushed, lines = _pass("housekeeping", _facts(frozenset(KINDS)), rig.chores(rig.deps()))
    assert rig.pulls == 1
    lane = [a for a in performed if a.get("cause") == "lane_checkout_dirty: /srv/repo"]
    assert [a["epoch"] for a in lane] == [7]
    assert "lane_checkout_dirty: /srv/repo" in pushed[0]
    assert len(rig.exports) == 1 and "export: wrote" in lines[-1]


def test_a_quiet_pass_does_not_export_again_but_a_pull_that_moved_head_or_a_store_change_does(tmp_path):
    rig = Rig(tmp_path, lanes=())
    deps = rig.deps()
    chores = rig.chores(deps)
    facts = _facts(frozenset(KINDS))
    _pass("housekeeping", facts, chores)
    _pass("housekeeping", facts, chores)
    assert len(rig.exports) == 1  # idempotent: nothing moved, nothing to write
    rig.head = "c1"  # the pull brought an intake file in
    _pass("housekeeping", facts, chores)
    rig.stamp = ("rows", 2)  # another worker changed a row
    _pass("housekeeping", facts, chores)
    assert len(rig.exports) == 3


def test_a_lane_line_already_recorded_is_not_recorded_again_but_is_still_pushed(tmp_path):
    rig = Rig(tmp_path)
    chores = rig.chores(rig.deps())
    facts = _facts(frozenset(KINDS))
    _pass("housekeeping", facts, chores)
    performed, pushed, _ = _pass("housekeeping", facts, chores)
    assert not [a for a in performed if str(a.get("cause", "")).startswith("lane_checkout_dirty")]
    assert "lane_checkout_dirty: /srv/repo" in pushed[0]


def test_with_a_tick_up_the_worker_leaves_every_chore_to_it(tmp_path):
    rig = Rig(tmp_path)
    performed, _, _ = _pass("housekeeping", _facts(frozenset(KINDS), tick=True), rig.chores(rig.deps()))
    assert rig.pulls == 0 and rig.exports == []
    assert not [a for a in performed if str(a.get("cause", "")).startswith("lane_checkout")]


def test_a_failed_pull_is_a_note_and_one_record_per_reason(tmp_path):
    rig = Rig(tmp_path, pull="git pull: could not reach origin", lanes=())
    chores = rig.chores(rig.deps())
    _, _, lines = _pass("housekeeping", _facts(frozenset(KINDS)), chores)
    _pass("housekeeping", _facts(frozenset(KINDS)), chores)
    assert "workspace pull failed: git pull: could not reach origin" in lines[-1]
    assert [a["kind"] for a in rig.recorded].count("pull_workspace") == 1


def test_the_chat_directives_are_read_each_pass_and_left_to_the_tick_while_one_runs(tmp_path):
    rig = Rig(tmp_path, lanes=())
    _pass("housekeeping", _facts(frozenset(KINDS)), rig.chores(rig.deps()))
    assert rig.chat_reads == 1
    _pass("housekeeping", _facts(frozenset(KINDS), tick=True), rig.chores(rig.deps()))
    assert rig.chat_reads == 1


def test_a_chore_that_raises_is_a_note_not_a_failed_pass(tmp_path):
    def boom(*_a):
        raise OSError("disk full")

    chores = chair_queue.Chores(before=boom, after=boom)
    performed, pushed, lines = _pass("housekeeping", _facts(frozenset(KINDS)), chores)
    assert pushed and "chores failed: OSError: disk full" in lines[-1]


def test_the_ci_gate_pause_line_is_recorded_once_when_the_gate_first_pauses(tmp_path):
    from agent_tools.ci_gate import CiGate

    rig = Rig(tmp_path, lanes=())
    deps = rig.deps()
    paused = CiGate(paused=True, reason="main is red")
    chores = rig.chores(dataclasses.replace(deps, status_reader=object()))  # type: ignore[arg-type]
    import agent_tools.chair_run as chair_run

    real = chair_run.gate_now
    chair_run.gate_now = lambda _deps, _now: paused  # type: ignore[assignment]
    try:
        first, _, _ = _pass("housekeeping", _facts(frozenset(KINDS)), chores)
        again, _, _ = _pass("housekeeping", _facts(frozenset(KINDS)), chores)
    finally:
        chair_run.gate_now = real
    assert [a["cause"] for a in first if a.get("initiative") == "ci-gate"] == ["ci_gate_paused: main is red"]
    assert not [a for a in again if a.get("initiative") == "ci-gate"]


def test_the_launch_worker_leaves_its_why_not_launched_reasons_beside_the_runs(tmp_path):
    rig = Rig(tmp_path)
    chores = chair_worker_chores.launch_chores(rig.deps())
    assert chores.after([{"kind": "unlaunched", "initiative": "a", "reason": "no free lane"}], []) == ""
    assert "no free lane" in (tmp_path / mcp_ports.UNLAUNCHED_FILE).read_text()


def test_the_export_is_due_on_the_first_pass_a_changed_row_or_a_move():
    state = chair_worker_chores.ExportState()
    assert chair_worker_chores.export_due(state, "s", "h", ())
    state = chair_worker_chores.ExportState("s", "h", True)
    assert not chair_worker_chores.export_due(state, "s", "h", ())
    assert chair_worker_chores.export_due(state, "s", "h", ("t1",))
    assert chair_worker_chores.export_due(state, "s2", "h", ())
    assert chair_worker_chores.export_due(state, "s", "h2", ())
    assert chair_worker_chores.export_due(state, None, "h", ())
