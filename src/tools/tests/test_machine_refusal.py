"""A worker's launch refused by its machine, not its phase (E1, 2026-11-21: a stray untracked dir in a checkout spent
all three attempts of a phase another machine could have built): the claim goes back at once with its attempt
refunded, the repository is set aside until its checkout reads clean, and the chair is told once."""

from __future__ import annotations

from agent_tools import machine_refusal, worker
from agent_tools.machine_refusal import Refusal, SetAside

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
    assert found.reason == "this machine cannot launch it (checkout): ~/repos/coxswain has uncommitted changes: ?? origin/"


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


def test_a_repository_is_news_for_the_chair_once_however_often_it_is_refused():
    first, news = machine_refusal.set_aside({}, "~/repos/coxswain", DIRTY, now=10.0)
    again, news_again = machine_refusal.set_aside(first, "~/repos/coxswain", Refusal("checkout", "still dirty"), now=50.0)
    assert (news, news_again) == (True, False)
    assert again["~/repos/coxswain"] == SetAside("~/repos/coxswain", Refusal("checkout", "still dirty"), 10.0)


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
