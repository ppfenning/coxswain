"""Literal-input tests for epic_steward.decide. Run: python3 -m unittest scripts/test_epic_steward.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import epic_steward as s


def T(i, lv, st):
    return {"id": i, "level": lv, "state": st}


def state(tasks, merged=None, alive=False, sent=(), last_fp=None, head="abc", gated=None):
    levels = {t["level"] for t in tasks}
    return {"tasks": tasks, "merged": merged if merged is not None else {lv: False for lv in levels},
            "run_alive": alive, "sent": set(sent), "last_relaunch_fp": last_fp, "head": head, "gated_head": gated}


def kinds(acts):
    return [a["do"] if a["do"] != "notify" else "notify:" + a["event"] for a in acts]


class Decide(unittest.TestCase):
    def test_approved_level_is_merged_before_anything_is_relaunched(self):
        acts = s.decide(state([T("a", "schema", "approved"), T("b", "readers", "ready")]))
        self.assertEqual(kinds(acts), ["merge"])
        self.assertEqual(acts[0]["levels"], ["schema"])

    def test_level_with_an_unapproved_task_is_not_merged(self):
        acts = s.decide(state([T("a", "readers", "approved"), T("b", "readers", "ready")], alive=True))
        self.assertEqual(acts, [])

    def test_already_merged_level_is_left_alone(self):
        acts = s.decide(state([T("a", "schema", "approved"), T("b", "readers", "ready")], merged={"schema": True, "readers": False}))
        self.assertEqual(kinds(acts), ["relaunch"])

    def test_exited_run_with_ready_tasks_relaunches_once_per_state(self):
        tasks = [T("a", "schema", "approved"), T("b", "readers", "ready")]
        merged = {"schema": True, "readers": False}
        acts = s.decide(state(tasks, merged=merged))
        self.assertEqual(kinds(acts), ["relaunch"])
        again = s.decide(state(tasks, merged=merged, last_fp=acts[0]["fp"]))
        self.assertEqual(kinds(again), ["notify:stuck"])

    def test_live_run_is_never_relaunched(self):
        acts = s.decide(state([T("a", "schema", "approved"), T("b", "readers", "ready")], merged={"schema": True, "readers": False}, alive=True))
        self.assertEqual(acts, [])

    def test_quarantine_is_announced_once_and_blocks_relaunch(self):
        tasks = [T("a", "schema", "quarantined"), T("b", "readers", "ready")]
        first = s.decide(state(tasks))
        self.assertEqual(kinds(first), ["notify:needs_attention"])
        second = s.decide(state(tasks, sent=[first[0]["key"]]))
        self.assertEqual(second, [])

    def test_complete_epic_runs_the_gate_once_then_announces(self):
        tasks = [T("a", "schema", "done"), T("b", "readers", "approved"), T("c", "readers", "dropped")]
        merged = {"schema": True, "readers": True}
        gate = s.decide(state(tasks, merged=merged))
        self.assertEqual(kinds(gate), ["gate"])
        done = s.decide(state(tasks, merged=merged, gated="abc"))
        self.assertEqual(kinds(done), ["notify:epic_complete"])
        self.assertEqual(s.decide(state(tasks, merged=merged, gated="abc", sent=[done[0]["key"]])), [])

    def test_a_moved_ticket_head_reruns_the_gate(self):
        tasks = [T("a", "schema", "done")]
        acts = s.decide(state(tasks, merged={"schema": True}, head="def", gated="abc"))
        self.assertEqual(kinds(acts), ["gate"])

    def test_empty_epic_does_nothing(self):
        self.assertEqual(s.decide(state([])), [])

    def test_fingerprint_changes_when_a_state_changes(self):
        a = s.fingerprint([T("a", "x", "ready")])
        self.assertNotEqual(a, s.fingerprint([T("a", "x", "approved")]))
        self.assertEqual(a, s.fingerprint([T("a", "x", "ready")]))


if __name__ == "__main__":
    unittest.main()
