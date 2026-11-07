from __future__ import annotations

import signal

import pytest

import harness.cli as cli
from harness import termination


@pytest.fixture(autouse=True)
def _reset_flag():
    termination._TERMINATING.clear()
    yield
    termination._TERMINATING.clear()


@pytest.fixture
def calls(monkeypatch):
    """Record `signal.signal` and `_terminate_children` calls so the handler never touches the real process."""
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(cli.signal, "signal", lambda *_: seen.append(("ignore", termination.is_terminating())))
    monkeypatch.setattr(cli, "_terminate_children", lambda: seen.append(("children", termination.is_terminating())))
    return seen


def test_flag_is_clear_before_the_handler_runs():
    assert termination.is_terminating() is False


def test_handler_exits_143_and_leaves_the_flag_set(calls):
    with pytest.raises(SystemExit) as exc:
        cli._exit_on_sigterm(signal.SIGTERM, None)
    assert exc.value.code == 143
    assert termination.is_terminating() is True


def test_flag_is_set_before_sigterm_is_ignored_and_children_are_stopped(calls):
    with pytest.raises(SystemExit):
        cli._exit_on_sigterm(signal.SIGTERM, None)
    assert calls == [("ignore", True), ("children", True)]
