"""The smoke suite's pure core (smoke/plan.py): example inputs in, outputs checked. No I/O, no mocks."""

import pytest

from smoke import plan


@pytest.mark.parametrize(("value", "expected"), [(None, False), ("", False), ("0", False), ("false", False), ("No", False), ("1", True), ("yes", True)])
def test_is_required_reads_a_flag(value, expected):
    assert plan.is_required(value) is expected


def test_a_missing_prerequisite_skips_unless_the_run_requires_the_suite():
    assert plan.unavailable("no docker", required=False) == plan.Skip("no docker")
    assert plan.unavailable("no docker", required=True) == plan.Fail("no docker")


def test_an_environment_url_on_loopback_is_used_as_is():
    url = "postgresql://postgres:pw@127.0.0.1:5432/postgres"
    assert plan.postgres_source(url, None, False, required=True) == plan.UseUrl(url)
    assert plan.postgres_source("postgres://u@localhost/db", None, False, required=False) == plan.UseUrl("postgres://u@localhost/db")


@pytest.mark.parametrize("url", ["postgresql://u:p@db.example.com:5432/x", "postgresql://u:p@10.0.0.5/x", "sqlite:///x.db", "nonsense"])
def test_an_environment_url_off_loopback_or_not_postgres_fails_even_when_not_required(url):
    decision = plan.postgres_source(url, "/usr/bin/docker", True, required=False)
    assert isinstance(decision, plan.Fail)
    assert plan.URL_ENV in decision.reason


def test_without_a_url_docker_decides():
    assert plan.postgres_source(None, "/usr/bin/docker", True, required=False) == plan.StartContainer()
    assert isinstance(plan.postgres_source(None, None, False, required=False), plan.Skip)
    assert isinstance(plan.postgres_source(None, None, False, required=True), plan.Fail)


def test_a_docker_whose_daemon_does_not_answer_fails_unless_the_host_opts_out():
    no_daemon = plan.postgres_source(None, "/usr/bin/docker", False, required=False)
    assert isinstance(no_daemon, plan.Fail) and "daemon" in no_daemon.reason and plan.BROKEN_DOCKER_OK_ENV in no_daemon.reason
    opted_out = plan.postgres_source(None, "/usr/bin/docker", False, required=False, broken_docker_ok=True)
    assert isinstance(opted_out, plan.Skip) and plan.BROKEN_DOCKER_OK_ENV in opted_out.reason
    # a required run fails even with the opt-out set
    assert isinstance(plan.postgres_source(None, "/usr/bin/docker", False, required=True, broken_docker_ok=True), plan.Fail)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u@localhost/db?host=live.example.com",
        "postgresql://u@localhost/db?HOST=live.example.com",
        "postgresql://u@localhost/db?hostaddr=10.1.2.3",
        "postgresql://u@localhost/db?sslmode=disable&host=%2Fvar%2Frun%2Fpostgresql",
        "postgresql://u@localhost/db?service=live",
        "postgresql://u@localhost:5432,live.example.com:5432/db",
        "postgresql://u@live.example.com:5432,localhost:5432/db",
        "postgresql://u@localhost,localhost/db",
        "postgresql:///db",
        "postgresql://u@[::1/db",
    ],
)
def test_the_loopback_guard_refuses_a_url_that_could_reach_another_host(url):
    assert plan.loopback_refusal(url) is not None
    assert isinstance(plan.postgres_source(url, None, False, required=False), plan.Fail)


@pytest.mark.parametrize(
    "url", ["postgresql://u:p@localhost:5432/db?sslmode=disable", "postgresql://u@[::1]:5432/db", "postgres://127.0.0.1/db"]
)
def test_the_loopback_guard_takes_a_plain_loopback_url(url):
    assert plan.loopback_refusal(url) is None


def test_the_container_is_throwaway_labelled_pinned_and_published_on_loopback_only():
    argv = plan.docker_run_argv("s1", 1000)
    assert argv[:2] == ["docker", "run"]
    assert "--rm" in argv and "127.0.0.1::5432" in argv and plan.POSTGRES_IMAGE in argv
    assert "@sha256:" in plan.POSTGRES_IMAGE
    labels = [argv[i + 1] for i, token in enumerate(argv) if token == "--label"]
    assert labels == [plan.CONTAINER_LABEL, f"{plan.SESSION_LABEL}=s1", f"{plan.STARTED_LABEL}=1000"]
    assert plan.docker_remove_argv("a", "b") == ["docker", "rm", "--force", "a", "b"]
    assert f"label={plan.CONTAINER_LABEL}" in plan.docker_list_argv()


def test_only_containers_past_the_threshold_are_stale():
    listing = "old 1000\nfresh 9000\nunlabelled \nbad notanumber\n\n"
    assert plan.stale_containers(listing, now=10_000, stale_after=3600) == ["old"]
    assert plan.stale_containers(listing, now=10_000, stale_after=10_000) == []
    assert plan.stale_containers("", now=10_000) == []


def test_package_data_patterns_follow_their_package_folders():
    dirs = {"agent_tools": ("src/tools/agent_tools", "agent_tools"), "providers": ("src/cartridges/providers", "providers")}
    data = {"providers": ["catalog.yaml"], "agent_tools": ["extras-requirements.txt", "*.json"], "elsewhere": ["x.txt"]}
    assert plan.package_data(data, dirs) == [
        ("src/tools/agent_tools", "agent_tools", "extras-requirements.txt"),
        ("src/tools/agent_tools", "agent_tools", "*.json"),
        ("src/cartridges/providers", "providers", "catalog.yaml"),
    ]
    assert plan.docker_remove_argv("abc") == ["docker", "rm", "--force", "abc"]
    assert plan.docker_ready_argv("abc")[-2:] == ["--host", "127.0.0.1"]


@pytest.mark.parametrize(("output", "port"), [("127.0.0.1:32768\n", 32768), ("0.0.0.0:5001\n[::]:5001\n", 5001)])
def test_parse_published_port(output, port):
    assert plan.parse_published_port(output) == port


def test_parse_published_port_refuses_output_with_no_port():
    with pytest.raises(ValueError, match="no published port"):
        plan.parse_published_port("")


def test_urls():
    assert plan.postgres_url(5432) == "postgresql://postgres:smoke@127.0.0.1:5432/postgres"
    assert plan.postgres_url(1, password="a/b", database="d") == "postgresql://postgres:a%2Fb@127.0.0.1:1/d"
    assert plan.with_database("postgresql://u:p@localhost:5/postgres?sslmode=disable", "other") == "postgresql://u:p@localhost:5/other?sslmode=disable"


def test_database_names_are_valid_and_distinct():
    long_id = "smoke/test_contract_route_edit.py::test_" + "x" * 200
    names = {plan.database_name(i) for i in ("a::b", "a::c", "A::B[x-y]", long_id)}
    assert len(names) == 4
    for name in names:
        assert len(name) <= 63
        assert name.replace("_", "").isalnum() and name == name.lower()


def test_the_sandbox_env_keeps_only_what_it_names():
    inherited = {"HOME": "/home/user", "PYTHONPATH": "/elsewhere", "AGENT_TOOLS_PROFILE": "/p", "VIRTUAL_ENV": "/v", "LANG": "C.UTF-8"}
    env = plan.sandbox_env(inherited, home="/tmp/sandbox/h", bin_dirs=["/tmp/i/bin"], system_path="/usr/bin:/bin", extra={"X": "1"})
    assert env["HOME"] == "/tmp/sandbox/h"
    assert env["XDG_CONFIG_HOME"] == "/tmp/sandbox/h/.config" and env["XDG_DATA_HOME"] == "/tmp/sandbox/h/.local/share"
    assert env["GIT_CONFIG_GLOBAL"] == "/tmp/sandbox/h/.gitconfig" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["PATH"] == "/tmp/i/bin:/usr/bin:/bin"
    assert env["LANG"] == "C.UTF-8" and env["X"] == "1"
    assert not {"PYTHONPATH", "AGENT_TOOLS_PROFILE", "VIRTUAL_ENV"} & env.keys()


def test_profiles_name_the_sandbox():
    text = plan.routing_profile_text(team="t", workspace="/w", harness_dir="/h", provider_profile="/p.yaml")
    assert text == "team: t\nworkspace_dir: /w\nharness_dir: /h\nprovider_profile: /p.yaml\n"
    assert plan.provider_profile_text("postgresql://x") == "storage_url: postgresql://x\n"
    assert "defaultBranch = main" in plan.gitconfig_text()


def test_a_work_item_round_trips_through_its_front_matter():
    text = plan.work_item_text({"state": "todo", "id": "t1"}, "Body\n\n")
    assert text == "---\nstate: todo\nid: t1\n---\n\nBody\n"
    assert plan.front_matter(text) == {"state": "todo", "id": "t1"}


@pytest.mark.parametrize("text", ["", "no front matter", "---\nstate: todo\n"])
def test_front_matter_of_a_file_without_one_is_empty(text):
    assert plan.front_matter(text) == {}


def test_front_matter_skips_nested_lines():
    assert plan.front_matter("---\nneeds:\n- a\nid: t\n---\n") == {"needs": "", "id": "t"}


def test_install_drift_names_every_difference():
    assert plan.install_drift({"a.py": "1", "b.py": "2"}, {"a.py": "1", "b.py": "2"}) == []
    assert plan.install_drift({"a.py": "1", "b.py": "2"}, {"a.py": "9", "c.py": "3"}) == ["a.py", "b.py", "c.py"]


def test_python_sources_skip_caches():
    assert plan.python_sources(["b.py", "a.py", "x/__pycache__/a.py", "c.txt"]) == ["a.py", "b.py"]


def test_package_dirs_follow_package_dir():
    dirs = plan.package_dirs(["agent_tools", "graphs.ops", "coxswain"], {"agent_tools": "src/tools/agent_tools", "graphs": "src/graphs/graphs"})
    assert dirs == {
        "agent_tools": ("src/tools/agent_tools", "agent_tools"),
        "graphs.ops": ("src/graphs/graphs/ops", "graphs/ops"),
        "coxswain": ("coxswain", "coxswain"),
    }
