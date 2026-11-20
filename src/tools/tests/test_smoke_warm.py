"""The warm smoke Postgres: the container's argv, whether a container is ours, and the `smoke postgres` row and the
`--fix` commands for each state docker can report."""

import json

import pytest

from agent_tools import smoke_warm

# The label `smoke/plan.py` lists by in `docker_list_argv`, which `stale_containers` then filters.
REAPER_LABEL = "coxswain-smoke=1"
TARGET = smoke_warm.DEFAULT
OURS = {"coxswain-smoke-warm": "1"}
ON_URL = [{"HostIp": "127.0.0.1", "HostPort": "55432"}]


def _inspect(status: str, image: str = smoke_warm.IMAGE, *, name: str = f"/{smoke_warm.CONTAINER}",
             labels: dict | None = None, bindings: list | None = None) -> str:
    return json.dumps([{
        "Name": name,
        "State": {"Status": status},
        "Config": {"Image": image, "Labels": OURS if labels is None else labels},
        "HostConfig": {"PortBindings": {"5432/tcp": ON_URL if bindings is None else bindings}},
    }])


def _found(status: str, image: str = smoke_warm.IMAGE, **kw) -> smoke_warm.Inspected | None:
    return smoke_warm.parse_inspect(_inspect(status, image, **kw))


RUN = smoke_warm.run_argv()
RM = ["docker", "rm", "--force", smoke_warm.CONTAINER]


def test_the_container_publishes_on_loopback_only_with_data_on_tmpfs():
    assert RUN[RUN.index("--publish") + 1] == "127.0.0.1:55432:5432"
    assert RUN[RUN.index("--tmpfs") + 1] == "/var/lib/postgresql/data"
    assert "POSTGRES_INITDB_ARGS=--no-sync" in RUN
    assert RUN[RUN.index(smoke_warm.IMAGE) + 1 :] == [
        "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off",
    ]


def test_the_container_stays_and_restarts_where_the_throwaway_one_goes():
    assert "--rm" not in RUN
    assert RUN[RUN.index("--restart") + 1] == "unless-stopped"


def test_the_label_is_not_the_one_the_reaper_lists_by():
    label = RUN[RUN.index("--label") + 1]
    assert label == smoke_warm.LABEL
    assert label != REAPER_LABEL


def test_a_target_names_the_container_and_its_port_everywhere():
    target = smoke_warm.Target("throwaway", 61000)
    argv = smoke_warm.run_argv(target)
    assert argv[argv.index("--name") + 1] == "throwaway"
    assert argv[argv.index("--publish") + 1] == "127.0.0.1:61000:5432"
    assert smoke_warm.inspect_argv(target)[-1] == "throwaway"
    assert smoke_warm.ready_argv(target)[:3] == ["docker", "exec", "throwaway"]
    assert smoke_warm.url(target) == "postgresql://postgres:smoke@127.0.0.1:61000/postgres"


def test_the_target_comes_from_the_environment_and_defaults_to_the_warm_container():
    assert smoke_warm.target_from({}) == smoke_warm.DEFAULT
    env = {smoke_warm.CONTAINER_ENV: "throwaway", smoke_warm.PORT_ENV: "61000"}
    assert smoke_warm.target_from(env) == smoke_warm.Target("throwaway", 61000)


@pytest.mark.parametrize("port", ["0", "65536", "abc", "-1"])
def test_a_port_that_is_not_a_port_is_a_reason(port):
    reason = smoke_warm.target_from({smoke_warm.PORT_ENV: port})
    assert isinstance(reason, str) and smoke_warm.PORT_ENV in reason
    assert smoke_warm.refused_row(reason)["ok"] is True


def test_the_url_is_the_one_the_suite_builds():
    assert smoke_warm.url() == "postgresql://postgres:smoke@127.0.0.1:55432/postgres"


def test_the_pinned_image_is_digest_pinned():
    assert "@sha256:" in smoke_warm.IMAGE


def test_a_container_parses_to_its_state_image_name_labels_and_bindings():
    assert _found("running") == smoke_warm.Inspected(
        "running", smoke_warm.IMAGE, f"/{smoke_warm.CONTAINER}", (("coxswain-smoke-warm", "1"),), (("127.0.0.1", "55432"),),
    )


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
    # Our running container holds the port itself, so the port probe reads busy: that must not matter.
    row = smoke_warm.build_row(_found("running"), True, port_free=False)
    assert row == {
        "check": "smoke postgres",
        "ok": True,
        "detail": "server up on the pinned image; set COX_SMOKE_POSTGRES_URL="
        "postgresql://postgres:smoke@127.0.0.1:55432/postgres in the worker's unit environment",
    }


@pytest.mark.parametrize("kw", [{"labels": {}}, {"labels": {"other": "1"}}, {"name": "/someone-elses"}])
def test_a_container_that_is_not_ours_gets_no_url_and_no_fix(kw):
    found = _found("running", **kw)
    assert not smoke_warm.is_ours(found, TARGET)
    assert smoke_warm.fix_argvs(found) == []
    assert smoke_warm.fix_argvs(_found("exited", "postgres:15", **kw)) == []
    row = smoke_warm.build_row(found, True)
    assert row["ok"] is True
    assert "did not create" in row["detail"] and "COX_SMOKE_POSTGRES_URL" not in row["detail"]


@pytest.mark.parametrize("bindings", [[], [{"HostIp": "0.0.0.0", "HostPort": "55432"}], [{"HostIp": "127.0.0.1", "HostPort": "5432"}],
                                      [*ON_URL, {"HostIp": "0.0.0.0", "HostPort": "55432"}]])
def test_ours_published_anywhere_but_the_url_is_replaced(bindings):
    found = _found("running", bindings=bindings)
    assert smoke_warm.fix_argvs(found) == [RM, RUN]
    assert "not 127.0.0.1:55432" in smoke_warm.build_row(found, True)["detail"]


def test_stopped_says_the_doctor_starts_it_on_fix():
    row = smoke_warm.build_row(_found("exited"), True)
    assert row["ok"] is True
    assert row["detail"] == (
        "present but exited; `cox setup doctor --fix` starts it (without --fix the doctor starts nothing)"
    )


def test_a_different_image_says_fix_replaces_it():
    row = smoke_warm.build_row(_found("running", "postgres:15"), True)
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
    assert smoke_warm.fix_argvs(None) == [RUN]


def test_fix_starts_a_stopped_container():
    assert smoke_warm.fix_argvs(_found("exited")) == [["docker", "start", smoke_warm.CONTAINER]]


def test_fix_replaces_a_container_on_another_image():
    assert smoke_warm.fix_argvs(_found("running", "postgres:15")) == [RM, RUN]


def test_fix_leaves_a_running_container_on_the_pinned_image_alone():
    assert smoke_warm.fix_argvs(_found("running")) == []


@pytest.mark.parametrize(
    ("status", "argvs"),
    [
        ("created", [["docker", "start", smoke_warm.CONTAINER]]),
        ("paused", [["docker", "unpause", smoke_warm.CONTAINER]]),
        ("dead", [RM, RUN]),
        ("restarting", []),
        ("removing", []),
    ],
)
def test_fix_follows_the_state_docker_reports(status, argvs):
    assert smoke_warm.fix_argvs(_found(status)) == argvs


@pytest.mark.parametrize("found", [None, _found("exited"), _found("created"), _found("dead"), _found("exited", "postgres:15")])
def test_nothing_that_binds_the_port_runs_while_something_else_holds_it(found):
    assert smoke_warm.fix_argvs(found, port_free=True) != []
    assert smoke_warm.fix_argvs(found, port_free=False) == []
    detail = smoke_warm.build_row(found, True, port_free=False)["detail"]
    assert detail.startswith("port 127.0.0.1:55432 is held by something else")


def test_ours_running_on_the_port_is_replaced_even_though_it_holds_the_port():
    # Removing it frees the port, so the busy probe is ours and does not block the replacement.
    assert smoke_warm.fix_argvs(_found("running", "postgres:15"), port_free=False) == [RM, RUN]


def test_a_failed_run_leaves_a_created_container_that_is_removed_only_when_ours():
    assert smoke_warm.cleanup_argvs(_found("created")) == [RM]
    assert smoke_warm.cleanup_argvs(_found("created", labels={})) == []
    assert smoke_warm.cleanup_argvs(_found("running")) == []
    assert smoke_warm.cleanup_argvs(None) == []
    assert smoke_warm.cleanup_argvs(smoke_warm.UNREADABLE) == []


@pytest.mark.parametrize(
    ("status", "start"),
    [
        ("paused", "present but paused; `cox setup doctor --fix` unpauses it"),
        ("dead", "present but dead; `cox setup doctor --fix` replaces it"),
        ("restarting", "present and restarting; docker is bringing it up or down, so --fix does nothing"),
    ],
)
def test_the_row_says_what_fix_does_for_each_state(status, start):
    row = smoke_warm.build_row(_found(status), True)
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
