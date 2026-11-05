import dataclasses
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from runner.decision_log import CallDecision
from runner.protocol import NodeResult
from runner.shadow_tier import ShadowConfig
from runner.shadow_tier_runner import ShadowTierRunner, shadow_facts, shadow_fields

CHEAP = "cheap"
HANDOFF_ON = ShadowConfig((("handoff", CHEAP),))
BOTH_ON = ShadowConfig((("handoff", CHEAP), ("validate_chunk", CHEAP)))
SHADOW_FIELDS = [f.name for f in dataclasses.fields(CallDecision) if f.name.startswith("shadow_")]


class FakeInner:
    """Counts calls. Answers by the tier it is asked at, so acting and shadow answers can differ."""

    def __init__(self, answers, *, hook=True, record=True, raises_at=None, barrier=None):
        self.answers = answers
        self.raises_at = raises_at
        self.barrier = barrier
        self.kwargs = []
        self.hook_seen = []
        self._hooked = hook
        self._local = threading.local()
        if record:
            self.calls = []

    @property
    def tag_decision(self):
        if not self._hooked:
            raise AttributeError("tag_decision")
        return getattr(self._local, "hook", None)

    @tag_decision.setter
    def tag_decision(self, value):
        self._local.hook = value

    def run(self, **kwargs):
        tier = kwargs.get("tier")
        self.kwargs.append(kwargs)
        self.hook_seen.append(getattr(self, "tag_decision", None))
        if tier == self.raises_at:
            raise RuntimeError("shadow boom")
        if self.barrier is not None and tier != CHEAP:
            self.barrier.wait(timeout=5)
        answer = self.answers[tier]
        if callable(answer):
            answer = answer(kwargs)
        result = NodeResult(answer)
        result.decision = CallDecision(
            role=kwargs["role"],
            requested_tier=str(tier),
            chosen_tier=str(tier),
            model_id=f"model-{tier}",
            reason="r",
            ticket_key="",
            outcome_key="",
        )
        hook = getattr(self, "tag_decision", None)
        if hook is not None:
            result.decision = hook(result) or result.decision
        if hasattr(self, "calls"):
            self.calls.append(
                {
                    "role": kwargs["role"],
                    "tier": tier,
                    "model": f"call-{tier}",
                    "cost_usd": 0.5 if tier == CHEAP else 2.0,
                }
            )
        return result


def ask(runner, role="handoff", tier="strong", **extra):
    return runner.run(role=role, tier=tier, schema={"type": "object"}, prompt="p", context=("c",), task="t-1", **extra)


def answers(acting, shadow):
    return {"strong": acting, CHEAP: shadow, None: acting}


def test_an_empty_config_makes_one_call_and_returns_its_result():
    inner = FakeInner(answers({"complete": True}, {"complete": True}))
    result = ask(ShadowTierRunner(inner, ShadowConfig()), hints=None)
    assert len(inner.kwargs) == 1
    assert inner.kwargs[0]["tier"] == "strong" and inner.kwargs[0]["task"] == "t-1"
    assert "router_decision" not in inner.kwargs[0]
    assert result == {"complete": True} and result.decision.shadow_tier is None


def test_a_configured_role_makes_a_shadow_call_then_the_acting_call_and_returns_the_acting_result():
    inner = FakeInner(answers({"complete": True}, {"complete": False}))
    result = ask(ShadowTierRunner(inner, HANDOFF_ON), thread="th-1", hints=object())
    assert [k["tier"] for k in inner.kwargs] == [CHEAP, "strong"]
    shadow, acting = inner.kwargs
    assert shadow["hints"] is None and shadow["thread"] is None
    assert {k: shadow[k] for k in ("role", "schema", "prompt", "context", "task")} == {
        k: acting[k] for k in ("role", "schema", "prompt", "context", "task")
    }
    assert acting["thread"] == "th-1" and acting["hints"] is not None
    assert "router_decision" not in shadow and "wait_if_paused" not in shadow
    assert result == {"complete": True}
    assert result.decision.chosen_tier == "strong"


def test_the_shadow_call_does_not_get_router_decision_or_wait_if_paused_but_the_acting_call_does():
    inner = FakeInner(answers({"complete": True}, {"complete": True}))
    ask(ShadowTierRunner(inner, HANDOFF_ON), router_decision="rd", wait_if_paused=print)
    shadow, acting = inner.kwargs
    assert "router_decision" not in shadow and "wait_if_paused" not in shadow
    assert acting["router_decision"] == "rd" and acting["wait_if_paused"] is print


def test_a_different_shadow_answer_never_changes_what_is_returned():
    inner = FakeInner(answers({"complete": True, "note": "a"}, {"complete": False, "note": "b"}))
    result = ask(ShadowTierRunner(inner, HANDOFF_ON))
    assert dict(result) == {"complete": True, "note": "a"}


def test_the_shadow_hook_is_not_set_on_the_shadow_call():
    inner = FakeInner(answers({"complete": True}, {"complete": True}))
    ask(ShadowTierRunner(inner, HANDOFF_ON))
    assert inner.hook_seen[0] is None and inner.hook_seen[1] is not None
    assert inner.tag_decision is None


def test_a_raising_shadow_call_is_logged_and_the_acting_result_has_no_shadow_fields(caplog):
    inner = FakeInner(answers({"complete": True}, {}), raises_at=CHEAP)
    with caplog.at_level(logging.WARNING):
        result = ask(ShadowTierRunner(inner, HANDOFF_ON))
    assert result == {"complete": True}
    assert [getattr(result.decision, f) for f in SHADOW_FIELDS] == [None] * len(SHADOW_FIELDS)
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert inner.hook_seen[-1] is None


def test_a_stopped_acting_verdict_with_a_satisfied_shadow_verdict_is_costly():
    inner = FakeInner(answers({"satisfied": False}, {"satisfied": True}))
    decision = ask(ShadowTierRunner(inner, BOTH_ON), role="validate_chunk").decision
    assert (decision.shadow_tier, decision.shadow_verdict, decision.shadow_agreed, decision.shadow_costly) == (
        CHEAP,
        True,
        False,
        True,
    )


@pytest.mark.parametrize("role,key", [("handoff", "complete"), ("validate_chunk", "satisfied")])
def test_both_judging_roles_are_tagged_alike(role, key):
    inner = FakeInner(answers({key: True}, {key: True}))
    decision = ask(ShadowTierRunner(inner, BOTH_ON), role=role).decision
    assert (decision.shadow_verdict, decision.shadow_agreed, decision.shadow_costly) == (True, True, False)
    assert len(inner.kwargs) == 2


def test_a_role_outside_the_config_gets_no_extra_call_and_no_tags():
    inner = FakeInner(answers({"patch": "x"}, {"patch": "y"}))
    result = ask(ShadowTierRunner(inner, BOTH_ON), role="build")
    assert len(inner.kwargs) == 1 and result.decision.shadow_tier is None


def test_the_shadow_model_and_cost_come_from_the_shadow_calls_own_record():
    inner = FakeInner(answers({"complete": True}, {"complete": True}))
    decision = ask(ShadowTierRunner(inner, HANDOFF_ON)).decision
    assert (decision.shadow_model_id, decision.shadow_cost_usd) == (f"model-{CHEAP}", 0.5)


def test_without_a_calls_list_the_cost_is_none_not_a_guess():
    inner = FakeInner(answers({"complete": True}, {"complete": True}), record=False)
    decision = ask(ShadowTierRunner(inner, HANDOFF_ON)).decision
    assert (decision.shadow_model_id, decision.shadow_cost_usd) == (f"model-{CHEAP}", None)


def test_shadow_facts_with_two_new_records_gives_no_cost_and_falls_back_to_the_record_model():
    bare = NodeResult({})
    assert shadow_facts(bare, [{"model": "m", "cost_usd": 1.0}]) == ("m", 1.0)
    assert shadow_facts(bare, [{"model": "m", "cost_usd": 1.0}, {"model": "n", "cost_usd": 2.0}]) == (None, None)
    assert shadow_facts(bare, [{"model": "m", "cost_usd": True}]) == ("m", None)
    assert shadow_facts(bare, None) == (None, None)


def test_shadow_fields_with_an_unreadable_verdict_leave_agreement_unset():
    got = shadow_fields("handoff", CHEAP, {"complete": True}, {}, None, None)
    assert (got["shadow_verdict"], got["shadow_agreed"], got["shadow_costly"]) == (None, None, None)


def test_an_outer_hook_and_the_wrappers_own_both_land_on_one_decision():
    inner = FakeInner(answers({"complete": False}, {"complete": True}))
    runner = ShadowTierRunner(inner, HANDOFF_ON)
    runner.tag_decision = lambda result: dataclasses.replace(result.decision, system_one_backend="outer")
    decision = ask(runner).decision
    assert decision.system_one_backend == "outer" and decision.shadow_costly is True
    assert inner.hook_seen[0] is None
    assert runner.tag_decision is not None and inner.tag_decision is None


def test_an_outer_hook_reaches_the_inner_runner_for_a_role_outside_the_config():
    inner = FakeInner(answers({"patch": "x"}, {}))
    runner = ShadowTierRunner(inner, HANDOFF_ON)
    runner.tag_decision = lambda result: dataclasses.replace(result.decision, system_one_backend="outer")
    assert ask(runner, role="build").decision.system_one_backend == "outer"
    assert len(inner.kwargs) == 1


def test_with_an_inner_runner_that_has_no_hook_the_decision_is_tagged_on_the_acting_result():
    inner = FakeInner(answers({"complete": False}, {"complete": True}), hook=False)
    assert not hasattr(inner, "tag_decision")
    runner = ShadowTierRunner(inner, HANDOFF_ON)
    runner.tag_decision = lambda result: dataclasses.replace(result.decision, system_one_backend="outer")
    result = ask(runner)
    assert result == {"complete": False}
    assert (result.decision.shadow_costly, result.decision.system_one_backend) == (True, "outer")
    assert not hasattr(inner, "tag_decision")


def test_public_attributes_read_and_write_through_to_the_inner_runner():
    inner = FakeInner(answers({}, {}))
    runner = ShadowTierRunner(inner, ShadowConfig())
    for name, value in (("run_id", "r:p"), ("node_cap_usd", 1.5), ("repo_dir", "/tmp/x"), ("check_commands", ["ls"])):
        assert not hasattr(inner, name) and not hasattr(runner, name)
        setattr(runner, name, value)
        assert getattr(inner, name) == value and getattr(runner, name) == value
    assert runner.calls is inner.calls
    runner._private = 1
    assert not hasattr(inner, "_private") and runner._private == 1


def test_one_wrapper_shared_across_threads_keeps_each_calls_tags_apart():
    barrier = threading.Barrier(2)
    inner = FakeInner(answers(lambda kw: {"complete": kw["prompt"] == "yes"}, {"complete": True}), barrier=barrier)
    runner = ShadowTierRunner(inner, HANDOFF_ON)

    def one(prompt):
        runner.tag_decision = lambda result: dataclasses.replace(result.decision, system_one_answer=prompt)
        return runner.run(role="handoff", tier="strong", schema={}, prompt=prompt)

    with ThreadPoolExecutor(2) as pool:
        yes, no = pool.map(one, ["yes", "no"])
    assert (yes.decision.shadow_agreed, yes.decision.shadow_costly, yes.decision.system_one_answer) == (
        True,
        False,
        "yes",
    )
    assert (no.decision.shadow_agreed, no.decision.shadow_costly, no.decision.system_one_answer) == (False, True, "no")
