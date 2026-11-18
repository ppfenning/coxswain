"""`cox dash`: the versioned JSON feed towpath reads, `--once` and `--feed`, over a real run and a filed task.

towpath runs `cox dash --feed` and reads one JSON snapshot per line. Several towpath windows on one workspace share
one computing process: the first `--feed` takes the lock under `runs/.dash-feed`, computes and publishes each
snapshot, and the others print what it published (`dash_feed_share`). These tests run the processes and read their
stdout the way towpath does.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from contextlib import contextmanager

from smoke import plan

LINE_TIMEOUT_S = 60
TASK = {"id": "t1", "title": "Add the note", "state": "ready", "phase": "p1", "repo": "app"}


def _filed_and_ran(launchable, repo) -> None:
    sandbox = launchable.sandbox
    sandbox.write("work/ship/p1/t1.md", plan.work_item_text(TASK, "Add the note."))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    ran = launchable.lifecycle(repo.path, "life-1")
    assert ran.returncode == 0, ran.stdout[-2000:] + ran.stderr[-2000:]


class _Feed:
    """A `cox dash --feed` process, its stdout lines read on a thread so a wait for the next one can time out."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put("")

    def snapshot(self) -> dict:
        """The next snapshot printed, as JSON; fails after LINE_TIMEOUT_S, or when the process exits, with its stderr."""
        try:
            line = self.lines.get(timeout=LINE_TIMEOUT_S)
        except queue.Empty:
            self.proc.kill()
            raise AssertionError(f"no feed line in {LINE_TIMEOUT_S}s: {self.proc.stderr.read()[-2000:]}") from None
        assert line, f"the feed exited {self.proc.wait()}: {self.proc.stderr.read()[-2000:]}"
        return json.loads(line)

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


@contextmanager
def _feed(sandbox):
    """A running `cox dash --feed`, stopped (terminate, then kill) on the way out."""
    feed = _Feed(subprocess.Popen([str(sandbox.install.cox), "dash", "--feed", "--interval", "0.2"], env=sandbox.env,
                                  cwd=sandbox.home, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    try:
        yield feed
    finally:
        feed.stop()


def test_one_snapshot_carries_the_run_the_queue_and_the_board(launchable, repo):
    sandbox = launchable.sandbox
    _filed_and_ran(launchable, repo)
    done = sandbox.cox("dash", "--once")
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    feed = json.loads(done.stdout)
    assert feed["schema"] == 1
    assert [(r["run"], r["status"]) for r in feed["runs"]] == [("life-1", "exited")]
    assert [h["run"] for h in feed["history"]] == ["life-1"]
    assert feed["queue_total"] == 1
    [queued] = feed["queue"]
    assert (queued["initiative"], queued["current_phase"]) == ("ship", "p1")
    assert queued["phases"][0]["tasks"] == [{"id": "t1", "state": "ready"}]
    [initiative] = [i for epic in feed["board"]["epics"] for i in epic["initiatives"]]
    assert (initiative["id"], initiative["counts"]["ready"]) == ("ship", 1)


def test_feed_and_once_together_are_refused(sandbox):
    done = sandbox.cox("dash", "--feed", "--once")
    assert (done.returncode, done.stderr.strip()) == (2, "cox dash: --feed and --once are mutually exclusive")


def test_a_second_feed_prints_what_the_first_computes_and_takes_over_when_it_exits(sandbox):
    with _feed(sandbox) as owner:
        first = owner.snapshot()
        assert first["schema"] == 1
        with _feed(sandbox) as follower:
            shared = follower.snapshot()
            # the follower computes nothing while the owner holds the lock: each line it prints is one the owner published
            published = [first] + [owner.snapshot() for _ in range(3)]
            assert shared["at"] in {s["at"] for s in published}, (shared["at"], [s["at"] for s in published])
            owner.stop()
            started = time.time()
            later = follower.snapshot()
            while later["at"] <= published[-1]["at"] and time.time() - started < LINE_TIMEOUT_S:
                later = follower.snapshot()
            assert later["at"] > published[-1]["at"], "the follower never computed a snapshot of its own"
    assert sorted(p.name for p in (sandbox.workspace / "runs" / ".dash-feed").iterdir()) == ["feed.lock", "feed.snapshot"]
