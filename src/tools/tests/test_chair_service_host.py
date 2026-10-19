from __future__ import annotations

from agent_tools import chair_service_host
from agent_tools.host_cmd import ChairHead


def test_install_steps_gives_the_exact_argv_and_run_flags_in_order() -> None:
    repos = {"coxswain": "git@example.com:coxswain.git"}
    assert chair_service_host.install_steps("host-a", "/r", repos, "git@example.com:workspace.git", "chair") == [
        (["ssh", "host-a", "if test -d /r/coxswain; then git -C /r/coxswain pull --ff-only; "
                            "else git clone git@example.com:coxswain.git /r/coxswain; fi"], True),
        (["ssh", "host-a", "if test -d /r/workspace; then git -C /r/workspace pull --ff-only; "
                            "else git clone git@example.com:workspace.git /r/workspace; fi"], True),
        (["ssh", "host-a", "cd /r/coxswain && uv sync --frozen --all-extras --inexact -q"], True),
        (["ssh", "host-a", "uv tool install --force --editable '/r/coxswain[traces,parquet,lake,postgres]'"], True),
        (["ssh", "host-a", "cox chair service --install --label chair"], True),
        (["sudo", "loginctl", "enable-linger", "<user>"], False),
        (["ssh", "host-a", "systemctl --user enable --now coxswain-chair.service"], False),
    ]


def test_the_linger_and_enable_steps_are_never_marked_to_run() -> None:
    steps = chair_service_host.install_steps("host-a", "/r", {"coxswain": "u"}, "wu", "chair")
    assert [run for _argv, run in steps[-2:]] == [False, False]


def test_a_checkout_follows_the_chairs_detached_commit_or_is_cloned() -> None:
    assert chair_service_host.sync_or_clone("/r/coxswain", "u", ChairHead("abc")) == (
        "if test -d /r/coxswain; then git -C /r/coxswain fetch --tags origin && git -C /r/coxswain checkout --detach abc; "
        "else git clone u /r/coxswain; fi")


def test_status_line_with_every_value_present() -> None:
    assert chair_service_host.status_line("host-a", "active", "tick 12 ok", "2026-09-28T00:00Z") == (
        "host-a: active; tick 12 ok; 2026-09-28T00:00Z"
    )


def test_status_line_with_the_unit_missing() -> None:
    assert chair_service_host.status_line("host-a", None, "no chair line", None) == (
        "host-a: no unit; no chair line; no tick"
    )
