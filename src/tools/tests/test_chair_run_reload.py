from dataclasses import replace

from agent_tools.chair_run import RestartState, run
from tests.test_chair_run import Rig

REASON = "profile changed: lanes"


def _deps(rig, hook, waits=3, **over):
    """Deps whose wait logs itself and interrupts on the `waits`-th call; `hook` is the reload hook."""

    def wait(_seconds):
        rig.log.append("wait")
        if rig.log.count("wait") >= waits:
            raise KeyboardInterrupt
        return False

    return replace(rig.deps(), wait=wait, reload_config=hook, **over)


def _hook(rig, result):
    calls = []

    def hook():
        rig.log.append("reload")
        calls.append(1)
        if isinstance(result, Exception):
            raise result
        return result

    return hook, calls


def test_a_hook_returning_none_leaves_the_loop_ticking():
    rig = Rig()
    hook, calls = _hook(rig, None)
    deps = _deps(rig, hook)
    run(False, 60, False, deps)
    assert (rig.log.count("gather"), len(calls), deps.restart_state.reason) == (3, 2, None)


def test_a_reason_ends_the_loop_after_the_current_tick_and_frees_the_lease():
    rig = Rig()
    hook, calls = _hook(rig, REASON)
    recorded = []
    deps = _deps(rig, hook, waits=100, record_restart=recorded.append)
    assert run(False, 60, False, deps) is None
    assert (rig.log.count("gather"), len(calls), rig.released) == (1, 1, [True])
    assert (deps.restart_state.reason, deps.restart_state.moved, deps.restart_state.restarted) == (REASON, None, True)
    assert (recorded, REASON in rig.lines) == ([REASON], True)


def test_a_broken_echo_or_record_does_not_keep_the_lease_held():
    rig = Rig()
    hook, _ = _hook(rig, REASON)

    def broken(_arg):
        raise RuntimeError("boom")

    deps = _deps(rig, hook, waits=100, record_restart=broken)
    deps = replace(deps, report_deps=replace(deps.report_deps, echo=broken))
    run(False, 60, False, deps)
    assert (deps.restart_state.reason, rig.released) == (REASON, [True])


def test_the_hook_is_never_called_on_a_dry_run():
    rig = Rig()
    hook, calls = _hook(rig, REASON)
    deps = _deps(rig, hook)
    run(False, 60, True, deps)
    assert (calls, rig.log.count("gather"), deps.restart_state.reason) == ([], 3, None)


def test_a_raising_hook_does_not_stop_the_loop():
    rig = Rig()
    hook, calls = _hook(rig, RuntimeError("unreadable profile"))
    deps = _deps(rig, hook)
    run(False, 60, False, deps)
    assert (rig.log.count("gather"), len(calls), deps.restart_state.reason) == (3, 2, None)


def test_the_hook_runs_between_ticks_and_never_during_one():
    rig = Rig()
    hook, _ = _hook(rig, None)
    run(False, 60, False, _deps(rig, hook))
    assert rig.log == ["beat", "gather", "wait", "reload", "beat", "gather", "wait", "reload", "beat", "gather", "wait"]


def test_no_hook_leaves_the_loop_as_it_was():
    rig = Rig()
    deps = _deps(rig, None)
    run(False, 60, False, deps)
    assert (rig.log.count("gather"), deps.restart_state.reason) == (3, None)


def test_restarted_is_true_exactly_when_a_reason_is_set():
    assert RestartState().restarted is False
    assert RestartState(reason=REASON).restarted is True
