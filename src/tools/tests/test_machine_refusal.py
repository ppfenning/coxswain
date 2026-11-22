"""A worker's launch refused by its machine, not its phase (E1, 2026-11-21: a stray untracked dir in a checkout spent
all three attempts of a phase another machine could have built): the claim goes back at once with its attempt
refunded, the repository is set aside until its checkout reads clean, and the chair is told once."""

from __future__ import annotations

from agent_tools import machine_refusal, worker
from agent_tools.machine_refusal import Refusal, SetAside
from harness import phase_pick

DIRTY = Refusal("checkout", "~/repos/coxswain has uncommitted changes: ?? origin/")


def _launch_output(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_the_dirty_checkout_of_e1_is_this_machine_s_refusal_and_costs_the_phase_nothing():
    # What box-b's launch printed, with the marked line a worker's launch now adds after it.
    output = _launch_output("routing: ~/repos/coxswain has uncommitted changes:", "?? origin/",
                            machine_refusal.marked("checkout", "~/repos/coxswain has uncommitted changes: ?? origin/\n"))
    found, refusal = worker.launch_verdict(2, output, attempts=3)
    assert refusal == DIRTY
    assert (found.kind, found.refund, found.wait_s) == ("retry", True, 0)
    assert found.reason == ("this machine cannot launch it (checkout): ~/repos/coxswain has uncommitted changes: ?? origin/"
                            " [machine refusals refunded: 1]")


def test_a_gate_refusal_still_waits_out_its_backoff_and_a_plain_failure_counts_its_attempt():
    gate, aside = worker.launch_verdict(2, "routing: usage stop: weekly ceiling\n", attempts=2)
    assert aside is None and gate.refund and gate.wait_s > 0
    failed, aside = worker.launch_verdict(2, "routing: ship has no phase p9; phases: p1\n", attempts=1)
    assert aside is None and not failed.refund
    assert failed.reason.endswith("the launch exited 2: routing: ship has no phase p9; phases: p1")


def test_only_an_exit_2_with_a_marked_line_is_the_machine_s_and_the_last_marked_line_names_it():
    marked = machine_refusal.marked("machine", "the harness venv is missing at /opt/h")
    assert machine_refusal.of(1, marked) is None
    assert machine_refusal.of(2, "routing: has uncommitted changes\n") is None
    assert machine_refusal.of(2, f"{machine_refusal.MARK} (weather): rain") is None
    both = _launch_output(machine_refusal.marked("checkout", "dirty"), marked)
    assert machine_refusal.of(2, both) == Refusal("machine", "the harness venv is missing at /opt/h")


def test_a_marked_reason_is_one_bounded_line():
    line = machine_refusal.marked("checkout", "a\nb   c" + "x" * 400)
    assert "\n" not in line
    assert machine_refusal.of(2, line) == Refusal("checkout", ("a b c" + "x" * 400)[:300])


def test_a_repository_set_aside_again_keeps_its_first_time_and_takes_the_latest_refusal():
    first = machine_refusal.set_aside({}, "~/repos/coxswain", DIRTY, now=10.0)
    again = machine_refusal.set_aside(first, "~/repos/coxswain", Refusal("checkout", "still dirty"), now=50.0)
    assert again["~/repos/coxswain"] == SetAside("~/repos/coxswain", Refusal("checkout", "still dirty"), 10.0)


NO_TOOL = Refusal("machine", "no host has the tool cargo")


def _round(prior: str, attempts: int):
    """One claim of a phase no machine can launch: the claim counts an attempt, the refusal's verdict says the rest,
    and the release refunds it or not (`store_phases.release`). The row's reason and attempts after."""
    found, _ = worker.launch_verdict(2, machine_refusal.marked(NO_TOOL.kind, NO_TOOL.reason), attempts + 1, prior)
    return found, attempts + (0 if found.refund else 1)


def test_a_phase_no_machine_can_launch_is_refunded_three_times_an_episode_then_spends_its_attempts_and_fails():
    reason, attempts, kinds = "", 0, []
    for _ in range(12):
        found, attempts = _round(reason, attempts)
        kinds.append((found.kind, found.refund))
        if found.kind != "retry":
            break
        reason = found.reason
    assert kinds[:3] == [("retry", True)] * machine_refusal.REFUNDS
    assert kinds[3:] == [("retry", False), ("retry", False), ("quarantine", False)]
    assert found.outcome == "failed" and "this machine cannot launch it (machine): no host has the tool cargo" in found.reason
    assert "counted, after 3 refunded this episode" in found.reason


def test_the_chair_is_told_once_an_episode_for_each_reason():
    assert machine_refusal.tell("", NO_TOOL)
    first = machine_refusal.verdict(NO_TOOL, 1, "").reason
    assert not machine_refusal.tell(first, NO_TOOL)  # another worker, the same refusal: not news
    assert machine_refusal.tell(first, DIRTY)  # a new reason is
    spent = machine_refusal.with_refunds(machine_refusal.line(NO_TOOL), machine_refusal.REFUNDS)
    assert not machine_refusal.tell(machine_refusal.verdict(NO_TOOL, 3, spent).reason, NO_TOOL)


def test_the_count_rides_along_any_other_retry_and_sits_before_a_lost_lane_tag():
    prior = machine_refusal.verdict(NO_TOOL, 1, "").reason
    later = phase_pick.carry_refunds(prior, machine_refusal.carry_refunds(prior, phase_pick.with_refunds("the lane gave its claim up", 1)))
    assert later == "the lane gave its claim up [machine refusals refunded: 1] [lost lanes refunded: 1]"
    assert (machine_refusal.refunds_of(later), phase_pick.refunds_of(later)) == (1, (1, False))
    assert machine_refusal.with_refunds(later, 0) == "the lane gave its claim up [lost lanes refunded: 1]"


def test_a_checkout_is_readmitted_once_it_reads_clean_and_anything_else_once_its_retry_time_has_passed():
    checkout = SetAside("~/repos/app", DIRTY, since=0.0)
    assert not machine_refusal.cleared(checkout, checkout_clean=False, now=10_000.0)
    assert not machine_refusal.cleared(checkout, checkout_clean=None, now=10_000.0)
    assert machine_refusal.cleared(checkout, checkout_clean=True, now=1.0)
    venv = SetAside("~/repos/app", Refusal("machine", "no venv"), since=100.0)
    assert not machine_refusal.cleared(venv, None, now=100.0 + machine_refusal.RETRY_S - 1)
    assert machine_refusal.cleared(venv, None, now=100.0 + machine_refusal.RETRY_S)


def test_a_repository_set_aside_is_not_advertised_and_its_token_goes_with_it():
    present = machine_refusal.advertised(["~/repos/coxswain", "~/src/app"], {"~/repos/coxswain"})
    assert present == ["~/src/app"]
    assert worker.repo_tokens(["~/repos/coxswain", "~/src/app"], present) == ["repo:app"]


def test_the_chair_note_names_the_worker_the_repo_the_phase_and_when_it_is_advertised_again():
    note = machine_refusal.chair_note("box-b", "ship", "p1", SetAside("~/repos/coxswain", DIRTY, 0.0))
    assert note.startswith("needs_attention: worker box-b cannot launch repo:coxswain (checkout): ")
    assert "ship/p1 went back with no attempt counted" in note
    assert note.endswith("until its checkout reads clean")
