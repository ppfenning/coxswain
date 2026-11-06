from copy import deepcopy
from dataclasses import dataclass, field

from agent_tools.chair_schedule_advice import Question, advise, requests_for

AT = "2026-11-06T00:00:00Z"
A = ("init-1", "p1-core")
B = ("init-1", "p2-feed")
C = ("init-2", "p1-docs")
A_LINE = "- init-1/p1-core: critical path 5, priority 2"
B_LINE = "- init-1/p2-feed: critical path 3, priority 1"


@dataclass
class FakeRelease:
    target: str = "2026-11-06T12:00:00Z"
    projected_finish: str = "2026-11-06T09:00:00Z"
    misses_target: bool = False


@dataclass
class FakeSchedule:
    launches: list = field(default_factory=lambda: [A, B])
    ties: tuple = ()
    critical_path: tuple = (A, B)
    critical_values: dict = field(default_factory=lambda: {A: 5, B: 3})
    priorities: dict = field(default_factory=lambda: {A: 2, B: 1})
    release: FakeRelease | None = field(default_factory=FakeRelease)


class Asker:
    def __init__(self, reply: str = "take a first") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts = [*self.prompts, prompt]
        return self.reply


def missed(**kw) -> FakeSchedule:
    release = FakeRelease(projected_finish="2026-11-06T13:00:00Z", misses_target=True)
    return FakeSchedule(release=release, **kw)


def test_tie_yields_one_request_and_one_advice_entry():
    s = FakeSchedule(ties=((A, B),))
    (request,) = requests_for(s)
    assert request.kind == "tie"
    assert request.nodes == (A, B)
    for fact in (
        "Tie group 1: init-1/p1-core, init-1/p2-feed",
        A_LINE,
        B_LINE,
        "2026-11-06T09:00:00Z",
        "2026-11-06T12:00:00Z",
    ):
        assert fact in request.prompt
    assert advise(s, Asker(), AT) == ({"kind": "tie", "text": "take a first", "at": AT},)


def test_tie_groups_stay_separate_and_nodes_are_not_repeated():
    (request,) = requests_for(FakeSchedule(ties=((A, B), (B, C))))
    assert request.nodes == (A, B, C)
    assert "Tie group 1: init-1/p1-core, init-1/p2-feed" in request.prompt
    assert "Tie group 2: init-1/p2-feed, init-2/p1-docs" in request.prompt


def test_missed_target_names_the_critical_path_and_dates():
    (request,) = requests_for(missed())
    assert request.kind == "miss"
    assert request.nodes == (A, B)
    for fact in (A_LINE, B_LINE, "2026-11-06T13:00:00Z", "2026-11-06T12:00:00Z"):
        assert fact in request.prompt


def test_clean_schedule_asks_nothing():
    asker = Asker()
    assert requests_for(FakeSchedule()) == ()
    assert advise(FakeSchedule(), asker, AT) == ()
    assert asker.prompts == []


def test_reorder_question_names_its_nodes_paths_priorities_and_dates():
    (request,) = requests_for(FakeSchedule(), Question("should p2-feed jump the queue?", (B,)))
    assert request.kind == "reorder"
    assert request.nodes == (B,)
    for fact in ("should p2-feed jump the queue?", B_LINE, "2026-11-06T09:00:00Z", "2026-11-06T12:00:00Z"):
        assert fact in request.prompt
    assert A_LINE not in request.prompt


def test_unknown_node_is_shown_as_unknown_not_zero():
    (request,) = requests_for(FakeSchedule(), Question("what about docs?", (C,)))
    assert "- init-2/p1-docs: critical path unknown, priority unknown" in request.prompt


def test_question_with_no_nodes_says_none_named():
    (request,) = requests_for(FakeSchedule(), Question("anything?", ()))
    assert "- none named" in request.prompt


def test_no_release_still_yields_tie_and_reorder_without_dates():
    s = FakeSchedule(ties=((A, B),), release=None)
    kinds = [r.kind for r in requests_for(s, Question("why?", (A,)))]
    assert kinds == ["tie", "reorder"]
    assert "Projected release finish: none" in requests_for(s)[0].prompt


def test_no_asker_returns_no_advice():
    assert advise(FakeSchedule(ties=((A, B),)), None, AT) == ()


def test_a_failing_or_blank_asker_loses_only_its_own_entry():
    s = missed(ties=((A, B),))

    def flaky(prompt: str) -> str:
        if "Judgment call: tie" in prompt:
            raise TimeoutError("model down")
        return "move p2-feed earlier"

    assert advise(s, flaky, AT) == ({"kind": "miss", "text": "move p2-feed earlier", "at": AT},)
    assert advise(s, Asker("  \n"), AT) == ()


def test_schedule_is_unchanged_by_advice():
    s = missed(ties=((A, B),))
    before = deepcopy(s)
    entries = advise(s, Asker(), AT, Question("why?", (A,)))
    assert [e["kind"] for e in entries] == ["tie", "miss", "reorder"]
    assert s == before
    assert s.launches == [A, B]
