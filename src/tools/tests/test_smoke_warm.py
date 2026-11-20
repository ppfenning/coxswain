"""The warm smoke Postgres: the container's argv, and the `smoke postgres` row for each state docker can report."""

import json

import pytest

from agent_tools import smoke_warm

# The label `smoke/plan.py` lists by in `docker_list_argv`, which `stale_containers` then filters.
REAPER_LABEL = "coxswain-smoke=1"


def _inspect(status: str, image: str) -> str:
    return json.dumps([{"Name": "/x", "State": {"Status": status}, "Config": {"Image": image}}])


RUNNING = _inspect("running", smoke_warm.IMAGE)
STOPPED = _inspect("exited", smoke_warm.IMAGE)
OTHER = _inspect("running", "postgres:15")


def test_the_container_publishes_on_loopback_only_with_data_on_tmpfs():
    argv = smoke_warm.run_argv()
    assert argv[argv.index("--publish") + 1] == "127.0.0.1:55432:5432"
    assert argv[argv.index("--tmpfs") + 1] == "/var/lib/postgresql/data"
    assert "POSTGRES_INITDB_ARGS=--no-sync" in argv
    assert argv[argv.index(smoke_warm.IMAGE) + 1 :] == [
        "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off",
    ]


def test_the_container_stays_and_restarts_where_the_throwaway_one_goes():
    argv = smoke_warm.run_argv()
    assert "--rm" not in argv
    assert argv[argv.index("--restart") + 1] == "unless-stopped"


def test_the_label_is_not_the_one_the_reaper_lists_by():
    argv = smoke_warm.run_argv()
    label = argv[argv.index("--label") + 1]
    assert label == smoke_warm.LABEL
    assert label != REAPER_LABEL


def test_the_url_is_the_one_the_suite_builds():
    assert smoke_warm.url() == "postgresql://postgres:smoke@127.0.0.1:55432/postgres"


def test_the_pinned_image_is_digest_pinned():
    assert "@sha256:" in smoke_warm.IMAGE


def test_a_running_container_parses_to_its_state_and_image():
    assert smoke_warm.parse_inspect(RUNNING) == smoke_warm.Inspected("running", smoke_warm.IMAGE)


def test_an_empty_list_is_an_absent_container():
    assert smoke_warm.parse_inspect("[]") is None


@pytest.mark.parametrize("text", ["", "not json", "{}", "[1]", '[{"State": {}, "Config": {}}]'])
def test_a_reply_that_cannot_be_read_is_not_an_absent_container(text):
    assert smoke_warm.parse_inspect(text) == smoke_warm.UNREADABLE


def test_an_unreadable_reply_gets_no_fix_and_says_so():
    assert smoke_warm.fix_argvs(smoke_warm.UNREADABLE) == []
    row = smoke_warm.build_row(smoke_warm.UNREADABLE, True)
    assert row["ok"] is True
    assert row["detail"].startswith("docker's answer about the container could not be read")


def test_up_on_the_pinned_image_names_the_url_to_set():
    row = smoke_warm.build_row(smoke_warm.parse_inspect(RUNNING), True)
    assert row == {
        "check": "smoke postgres",
        "ok": True,
        "detail": "server up on the pinned image; set COX_SMOKE_POSTGRES_URL="
        "postgresql://postgres:smoke@127.0.0.1:55432/postgres in the worker's unit environment",
    }


def test_stopped_says_the_doctor_starts_it_on_fix():
    row = smoke_warm.build_row(smoke_warm.parse_inspect(STOPPED), True)
    assert row["ok"] is True
    assert row["detail"] == (
        "present but exited; `cox setup doctor --fix` starts it (without --fix the doctor starts nothing)"
    )


def test_a_different_image_says_fix_replaces_it():
    row = smoke_warm.build_row(smoke_warm.parse_inspect(OTHER), True)
    assert row["ok"] is True
    assert row["detail"] == "present on postgres:15, not the pinned image; `cox setup doctor --fix` replaces it"


def test_absent_says_fix_creates_it():
    row = smoke_warm.build_row(smoke_warm.parse_inspect("[]"), True)
    assert row["ok"] is True
    assert row["detail"].startswith("absent; `cox setup doctor --fix` creates it")


def test_no_docker_says_the_suite_starts_a_container_per_session():
    row = smoke_warm.build_row(None, False)
    assert row == {
        "check": "smoke postgres",
        "ok": True,
        "detail": "docker is not available; the suite will start a container per session",
    }


@pytest.mark.parametrize(
    ("returncode", "stderr", "answered"),
    [
        (0, "", True),
        (1, "Error: No such object: coxswain-smoke-warm-postgres", True),
        (1, "Error response from daemon: No such container: coxswain-smoke-warm-postgres", True),
        (1, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock", False),
        (1, "error during connect: dial unix /var/run/docker.sock: connect: no such file or directory", False),
    ],
)
def test_docker_answered_tells_a_missing_container_from_a_missing_daemon(returncode, stderr, answered):
    assert smoke_warm.docker_answered(returncode, stderr) is answered


def test_fix_creates_an_absent_container():
    assert smoke_warm.fix_argvs(None) == [smoke_warm.run_argv()]


def test_fix_starts_a_stopped_container():
    assert smoke_warm.fix_argvs(smoke_warm.parse_inspect(STOPPED)) == [["docker", "start", smoke_warm.CONTAINER]]


def test_fix_replaces_a_container_on_another_image():
    assert smoke_warm.fix_argvs(smoke_warm.parse_inspect(OTHER)) == [
        ["docker", "rm", "--force", smoke_warm.CONTAINER],
        smoke_warm.run_argv(),
    ]


def test_fix_leaves_a_running_container_on_the_pinned_image_alone():
    assert smoke_warm.fix_argvs(smoke_warm.parse_inspect(RUNNING)) == []


@pytest.mark.parametrize(
    ("status", "argvs"),
    [
        ("created", [["docker", "start", smoke_warm.CONTAINER]]),
        ("paused", [["docker", "unpause", smoke_warm.CONTAINER]]),
        ("dead", [["docker", "rm", "--force", smoke_warm.CONTAINER], smoke_warm.run_argv()]),
        ("restarting", []),
        ("removing", []),
    ],
)
def test_fix_follows_the_state_docker_reports(status, argvs):
    assert smoke_warm.fix_argvs(smoke_warm.parse_inspect(_inspect(status, smoke_warm.IMAGE))) == argvs


@pytest.mark.parametrize(
    ("status", "start"),
    [
        ("paused", "present but paused; `cox setup doctor --fix` unpauses it"),
        ("dead", "present but dead; `cox setup doctor --fix` replaces it"),
        ("restarting", "present and restarting; docker is bringing it up or down, so --fix does nothing"),
    ],
)
def test_the_row_says_what_fix_does_for_each_state(status, start):
    row = smoke_warm.build_row(smoke_warm.parse_inspect(_inspect(status, smoke_warm.IMAGE)), True)
    assert row["ok"] is True
    assert row["detail"].startswith(start)


def test_the_skipped_row_is_advisory_and_names_the_variable():
    assert smoke_warm.skipped_row() == {
        "check": "smoke postgres",
        "ok": True,
        "detail": "skipped: COX_DOCTOR_SKIP_SMOKE_POSTGRES is set",
    }


def test_the_readiness_probe_runs_in_the_container_over_tcp():
    assert smoke_warm.ready_argv()[:3] == ["docker", "exec", smoke_warm.CONTAINER]
    assert smoke_warm.ready_argv()[-2:] == ["--host", "127.0.0.1"]
