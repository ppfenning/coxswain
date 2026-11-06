"""The queue driver, run against fake effects: collect, open, bisect, defer, and the chair's line protocol."""

from agent_tools.land_batch_run import (
    Effects,
    Outcome,
    ParsedQueue,
    QueueResult,
    parse_lines,
    render_lines,
    run_queue,
)
from agent_tools.land_batch_settings import MergeQueue


def _src(initiative, files, repo="r", phase="p"):
    return {
        "initiative": initiative,
        "phase": phase,
        "run": f"run-{initiative}",
        "repo": repo,
        "files": files,
        "tasks": [],
    }


class Fake:
    """Fake effects. `reads` is a list of source lists, one per read; the last repeats. The clock moves only on sleep."""

    def __init__(self, reads, bad=()):
        self.reads, self.bad = reads, set(bad)
        self.read_count, self.slept, self.batches, self.clock = 0, [], [], 0.0

    def read_sources(self):
        index = min(self.read_count, len(self.reads) - 1)
        self.read_count += 1
        return self.reads[index]

    def land(self, batch):
        self.batches.append([s["initiative"] for s in batch])
        bad = [s["initiative"] for s in batch if s["initiative"] in self.bad]
        if bad:
            return Outcome(f"ci failed for {bad[0]}\nsecond line", None)
        return Outcome(None, f"pr-{len(self.batches)}")

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.clock += seconds

    def now(self):
        return self.clock

    def effects(self):
        return Effects(self.read_sources, self.land, self.sleep, self.now)


def test_three_disjoint_sources_open_as_one_batch():
    a, b, c = _src("a", ["x"]), _src("b", ["y"]), _src("c", ["z"])
    fake = Fake([[a, b, c]])
    result = run_queue(MergeQueue(3, 0), fake.effects(), 5)
    assert fake.batches == [["a", "b", "c"]]
    assert result == QueueResult([(a, "pr-1"), (b, "pr-1"), (c, "pr-1")], [], [], 1)


def test_second_of_three_failing_is_returned_and_the_rest_land():
    a, b, c = _src("a", ["x"]), _src("b", ["y"]), _src("c", ["z"])
    fake = Fake([[a, b, c]], bad={"b"})
    result = run_queue(MergeQueue(3, 0), fake.effects(), 5)
    assert fake.batches == [["a", "b", "c"], ["a", "b"], ["a"], ["b"], ["c"]]
    assert [s for s, _ in result.landed] == [a, c]
    assert result.returned == [(b, "ci failed for b\nsecond line")]
    assert result.deferred == []
    assert result.ci_runs == len(fake.batches) == 5


def test_overlapping_source_is_deferred_and_never_lands_with_its_twin():
    a, b, c = _src("a", ["x"]), _src("b", ["x", "w"]), _src("c", ["z"])
    fake = Fake([[a, b, c]])
    result = run_queue(MergeQueue(3, 0), fake.effects(), 5)
    assert fake.batches == [["a", "c"]]
    assert [s for s, _ in result.landed] == [a, c]
    assert result.deferred == [b]


def test_driver_waits_for_a_third_source_then_opens():
    a, b, c = _src("a", ["x"]), _src("b", ["y"]), _src("c", ["z"])
    fake = Fake([[a, b], [a, b], [a, b, c]])
    result = run_queue(MergeQueue(3, 60), fake.effects(), 10)
    assert fake.slept == [10, 10]
    assert fake.read_count == 3
    assert fake.batches == [["a", "b", "c"]]
    assert len(result.landed) == 3


def test_driver_opens_a_short_batch_when_the_wait_runs_out():
    a, b = _src("a", ["x"]), _src("b", ["y"])
    fake = Fake([[a, b]])
    result = run_queue(MergeQueue(3, 60), fake.effects(), 20)
    assert fake.slept == [20, 20, 20]
    assert fake.batches == [["a", "b"]]
    assert [s for s, _ in result.landed] == [a, b]


def test_no_sources_returns_an_empty_result_without_landing_or_sleeping():
    fake = Fake([[]])
    result = run_queue(MergeQueue(3, 60), fake.effects(), 5)
    assert result == QueueResult([], [], [], 0)
    assert fake.batches == [] and fake.slept == []


def test_render_then_parse_round_trips_a_result():
    a, b, c = _src("a", ["x"]), _src("b", ["y"]), _src("c", ["z"])
    result = QueueResult([(a, "pr-1")], [(b, "boom\nsecond line")], [c], 3)
    text = render_lines(result)
    assert text == (
        "batch landed a/p pr=pr-1\n"
        "batch returned b/p: boom\n"
        "batch deferred c/p\n"
        "batch summary: ci_runs=3 landed=1 returned=1 deferred=1\n"
    )
    assert parse_lines(text) == ParsedQueue([("a/p", "pr-1")], [("b/p", "boom")], ["c/p"], 3)


def test_parse_ignores_other_lines():
    text = "chair: starting\nbatch landed nopr\n\nbatch deferred c/p\nbatch summary: ci_runs=x\n"
    assert parse_lines(text) == ParsedQueue([], [], ["c/p"], 0)
