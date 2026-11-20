"""The smoke gate's lane-host prerequisites (`lane_prereqs`): each check's verdict from probe results, the probe script
run for real against fake binaries on a temp PATH, and the chair's login check placing no lane on a failing host.
No mocks: the verdicts are pure, and the edges run real processes against a throwaway store."""

import ast
import json
import socket
import stat
from functools import partial
from pathlib import Path

import pytest
from lane_probe_replies import PASSING, probe_output

from agent_tools import chair_login_watch, chair_plan_fill, chair_report, lane_prereqs, run_store, store_cli
from agent_tools.chair_exec_land import run_argv_split
from agent_tools.chair_login_check import check_login_on_host
from agent_tools.lane_prereqs import Image, Ran
from harness.store_migrate import open_store

ROOT = Path(__file__).resolve().parents[3]
DOCKER_DOWN = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
KEYCHAIN = "keychain cannot be accessed because the current session does not allow user interaction"
SHIM = "mise ERROR No version is set for shim: gh"
NOW = "2026-11-20T00:00:00+00:00"


def _by_check(rows):
    return {row["check"]: row for row in rows}


# --- each check's verdict, from probe results ------------------------------------------------------------------------


def test_no_docker_on_the_lane_path_fails_the_daemon_row():
    row = lane_prereqs.docker_row(Ran("", 127, ""))
    assert row == {"check": "docker daemon", "ok": False,
                   "detail": "no docker on the lane PATH: the smoke gate starts its fixtures with docker"}


def test_a_docker_whose_daemon_does_not_answer_names_the_gap_and_says_to_start_it():
    row = lane_prereqs.docker_row(Ran("/usr/local/bin/docker", 1, DOCKER_DOWN))
    assert row["ok"] is False
    assert row["detail"] == f"the docker daemon did not answer (exit 1): {DOCKER_DOWN}; start it"


def test_an_answering_daemon_passes_with_its_version():
    assert lane_prereqs.docker_row(Ran("/usr/bin/docker", 0, "27.3.1")) == {
        "check": "docker daemon", "ok": True, "detail": "server 27.3.1 via /usr/bin/docker"}


def test_a_present_image_passes():
    row = lane_prereqs.image_row(Image(lane_prereqs.POSTGRES_IMAGE, "present", None, ""), "desktop", "")
    assert row == {"check": "smoke image postgres", "ok": True, "detail": "present in the local store"}


def test_an_image_is_not_checked_while_the_daemon_is_down():
    row = lane_prereqs.image_row(Image(lane_prereqs.GARAGE_IMAGE, "unchecked", None, ""), "", "")
    assert row == {"check": "smoke image garage", "ok": False, "detail": "not checked: the docker daemon did not answer"}


def test_an_absent_image_whose_credential_helper_is_not_on_the_path_cannot_be_pulled():
    row = lane_prereqs.image_row(Image(lane_prereqs.FORGEJO_IMAGE, "absent", None, ""), "desktop", "")
    assert row == {"check": "smoke image forgejo", "ok": False, "detail":
                   "absent, and docker pull would fail: docker-credential-desktop (credsStore) is not on the lane PATH"}


def test_an_absent_image_whose_credential_helper_fails_unattended_cannot_be_pulled():
    image = Image(lane_prereqs.POSTGRES_IMAGE, "absent", 1, KEYCHAIN)
    row = lane_prereqs.image_row(image, "desktop", "/usr/local/bin/docker-credential-desktop")
    assert row["ok"] is False
    assert row["detail"] == (f"absent, and docker pull would fail in docker-credential-desktop (exit 1): {KEYCHAIN}; "
                             "pull it once in a session that can reach the credential store")


@pytest.mark.parametrize(("store", "code", "line"), [
    ("", None, ""),  # no credsStore: docker pull asks no helper
    ("osxkeychain", 1, "credentials not found in native keychain"),  # anonymous pull goes on
    ("osxkeychain", 0, '{"Username": "u"}'),
])
def test_an_absent_image_docker_pull_can_fetch_unattended_passes(store, code, line):
    row = lane_prereqs.image_row(Image(lane_prereqs.POSTGRES_IMAGE, "absent", code, line), store, "/bin/helper")
    assert row == {"check": "smoke image postgres", "ok": True, "detail": "absent; docker pull can fetch it without a prompt"}


def test_a_client_tool_off_the_lane_path_names_itself_and_the_fix():
    row = lane_prereqs.tool_row("pg_dump", "")
    assert row == {"check": "pg_dump", "ok": False, "detail":
                   "not on the lane PATH: the smoke gate's store fixtures run pg_dump; put the PostgreSQL client tools' "
                   "folder on the PATH the lane's login shell sets"}
    assert lane_prereqs.tool_row("psql", "")["detail"].startswith("not on the lane PATH: the smoke gate's store fixtures run psql")
    assert lane_prereqs.tool_row("psql", "/usr/bin/psql") == {"check": "psql", "ok": True, "detail": "/usr/bin/psql"}


def test_gh_off_the_path_fails():
    assert lane_prereqs.gh_row(Ran("", 127, "")) == {"check": "gh", "ok": False, "detail": "not on the lane PATH"}


def test_a_gh_shim_with_no_version_set_is_not_a_usable_gh():
    row = lane_prereqs.gh_row(Ran("/home/user/.local/share/mise/shims/gh", 1, SHIM))
    assert row == {"check": "gh", "ok": False,
                   "detail": f"/home/user/.local/share/mise/shims/gh is not a usable gh (exit 1): {SHIM}"}


def test_a_gh_that_exits_0_without_its_version_line_is_not_a_usable_gh():
    assert lane_prereqs.gh_row(Ran("/usr/local/bin/gh", 0, "hello"))["ok"] is False


def test_a_real_gh_passes():
    row = lane_prereqs.gh_row(Ran("/usr/bin/gh", 0, "gh version 2.60.1 (2026-10-01)"))
    assert row == {"check": "gh", "ok": True, "detail": "gh version 2.60.1 (2026-10-01) at /usr/bin/gh"}


def test_tmp_headroom_under_the_floor_fails_and_names_the_directory():
    row = lane_prereqs.tmp_row("/tmp", "tmpfs 16262544 16000000 1048576 99% /tmp")
    assert row == {"check": "tmp headroom /tmp", "ok": False, "detail":
                   "1024 MiB free, under the 2048 MiB a lane needs; clear old build and test directories under /tmp"}


def test_tmp_headroom_at_the_floor_passes():
    assert lane_prereqs.tmp_row("/tmp", "tmpfs 9 1 2097152 1% /tmp") == {
        "check": "tmp headroom /tmp", "ok": True, "detail": "at least 2048 MiB free"}


@pytest.mark.parametrize("line", ["", "Filesystem 1024-blocks Used Available Capacity Mounted on", "df: /tmp: nope"])
def test_tmp_headroom_that_cannot_be_read_fails_as_not_measured(line):
    row = lane_prereqs.tmp_row("/tmp", line)
    assert row["ok"] is False
    assert row["detail"].startswith("not measured: df -Pk /tmp gave ")


def test_low_available_memory_fails():
    assert lane_prereqs.memory_row(["MemAvailable:     1048576 kB"]) == {
        "check": "free memory", "ok": False, "detail": "1024 MiB available, under the 2048 MiB a lane needs"}


def test_available_memory_reads_meminfo_or_vm_stat():
    assert lane_prereqs.available_memory_kb(["MemAvailable:   24433688 kB"]) == 24433688
    vm_stat = ["Mach Virtual Memory Statistics: (page size of 16384 bytes)", "Pages free:      100000.",
               "Pages inactive:   50000.", "Pages speculative:   6144."]
    assert lane_prereqs.available_memory_kb(vm_stat) == 156144 * 16
    assert lane_prereqs.memory_row(vm_stat) == {"check": "free memory", "ok": True, "detail": "at least 2048 MiB available"}


def test_memory_that_cannot_be_read_fails_as_not_measured():
    assert lane_prereqs.memory_row([]) == {"check": "free memory", "ok": False,
                                           "detail": "not measured: no MemAvailable in /proc/meminfo and no vm_stat"}


def test_a_host_short_on_room_is_blocked_but_keeps_its_capability():
    rows = lane_prereqs.reply_rows(0, probe_output(memory=("MemAvailable: 1024 kB",)), remote=True)
    assert lane_prereqs.capabilities(rows) == frozenset({"docker"})
    assert lane_prereqs.blocking_reason(rows) == "free memory: 1 MiB available, under the 2048 MiB a lane needs"


def test_an_unreachable_host_has_only_the_failing_reachability_row():
    output = "ssh: connect to host lane-a port 22: Operation timed out"
    assert lane_prereqs.reply_rows(255, output, remote=True) == [
        {"check": "host reachable", "ok": False, "detail": output}]


def test_a_reply_with_no_end_line_is_an_unreadable_probe():
    cut = PASSING.replace("cox-lane-prereq\tend\n", "")
    assert lane_prereqs.reply_rows(0, cut, remote=False) == [
        {"check": "lane probe", "ok": False, "detail": f"unreadable (exit 0): {' '.join(cut.split())[:160]}"}]


def test_a_local_probe_has_no_reachability_row_and_a_remote_one_does():
    assert "host reachable" not in _by_check(lane_prereqs.reply_rows(0, PASSING, remote=False))
    assert _by_check(lane_prereqs.reply_rows(0, PASSING, remote=True))["host reachable"]["ok"] is True


def test_startup_noise_around_the_probe_lines_is_ignored():
    noisy = "Last login: today\nstty: not a tty\n" + PASSING + "logout\n"
    assert lane_prereqs.reply_rows(0, noisy, remote=False) == lane_prereqs.reply_rows(0, PASSING, remote=False)


# --- capabilities and the stored record ------------------------------------------------------------------------------


def test_every_check_passing_gives_the_docker_capability():
    assert lane_prereqs.capabilities(lane_prereqs.reply_rows(0, PASSING, remote=True)) == frozenset({"docker"})


@pytest.mark.parametrize("output", [
    probe_output(docker=f"1\t/usr/bin/docker\t{DOCKER_DOWN}",
                 images=tuple(f"unchecked\t-\t{i}\t" for i in lane_prereqs.SMOKE_IMAGES)),
    probe_output(tools={"pg_dump": "", "psql": "/usr/bin/psql"}),
    probe_output(creds="desktop\t", images=(f"absent\t-\t{lane_prereqs.POSTGRES_IMAGE}\t",
                                             *(f"present\t-\t{i}\t" for i in lane_prereqs.SMOKE_IMAGES[1:]))),
])
def test_a_docker_gap_withholds_the_docker_capability(output):
    assert lane_prereqs.capabilities(lane_prereqs.reply_rows(0, output, remote=False)) == frozenset()


def test_rows_missing_a_docker_check_give_no_capability():
    assert lane_prereqs.capabilities([{"check": "docker daemon", "ok": True, "detail": ""}]) == frozenset()


def test_a_gh_gap_fails_the_host_but_leaves_the_docker_capability():
    rows = lane_prereqs.reply_rows(0, probe_output(gh=f"1\t/shims/gh\t{SHIM}"), remote=False)
    assert lane_prereqs.capabilities(rows) == frozenset({"docker"})
    assert lane_prereqs.blocking_reason(rows) == f"gh: /shims/gh is not a usable gh (exit 1): {SHIM}"


def test_a_failure_stores_the_reason_and_a_pass_clears_it():
    failing = lane_prereqs.reply_rows(0, probe_output(tools={"pg_dump": "", "psql": "/usr/bin/psql"}), remote=False)
    stored = lane_prereqs.merge_versions({"cox": "1.0"}, failing)
    assert stored["lane_ok"] is False
    assert stored["lane_capabilities"] == []
    assert stored["lane_reason"].startswith("pg_dump: not on the lane PATH")
    cleared = lane_prereqs.merge_versions(stored, lane_prereqs.reply_rows(0, PASSING, remote=False))
    assert cleared == {"cox": "1.0", "lane_ok": True, "lane_capabilities": ["docker"]}


def test_merging_the_same_rows_twice_changes_nothing():
    rows = lane_prereqs.reply_rows(0, probe_output(gh="127\t\t"), remote=True)
    once = lane_prereqs.merge_versions({"login_ok": True}, rows)
    assert lane_prereqs.merge_versions(once, rows) == once


def test_only_an_active_host_whose_lane_ok_is_false_is_blocked():
    rows = [
        {"name": "lane-a", "state": "active", "versions_json": {"lane_ok": False, "lane_reason": "pg_dump: missing"}},
        {"name": "lane-b", "state": "active", "versions_json": {"lane_ok": True}},
        {"name": "lane-c", "state": "active", "versions_json": "{}"},
        {"name": "lane-d", "state": "draining", "versions_json": {"lane_ok": False}},
    ]
    assert lane_prereqs.lane_blocked(rows) == {"lane-a": "pg_dump: missing"}
    assert lane_prereqs.lane_needs_chair(rows) == [
        {"kind": "needs_chair", "host": "lane-a", "cause": "lane_prereqs", "reason": "pg_dump: missing"}]


def test_the_status_line_names_the_host_and_why():
    action = {"kind": "needs_chair", "host": "lane-a", "cause": "lane_prereqs", "reason": "docker daemon: down"}
    assert chair_report.needs_chair_items([action]) == ["needs chair: lane-a:lane_prereqs docker daemon: down"]


def test_the_pinned_images_match_the_smoke_suite():
    tree = ast.parse((ROOT / "smoke" / "plan.py").read_text(encoding="utf-8"))
    pins = {node.targets[0].id: node.value.value for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith("_IMAGE") and isinstance(node.value, ast.Constant)}
    assert (pins["POSTGRES_IMAGE"], pins["FORGEJO_IMAGE"], pins["GARAGE_IMAGE"]) == lane_prereqs.SMOKE_IMAGES


# --- the real probe script against fake binaries on a temp PATH ------------------------------------------------------


def _tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


_DOCKER_UP = """case "$1" in
  info) echo 27.3.1 ;;
  image) [ "$5" = "$PRESENT" ] && exit 0; echo "Error: No such image: $5" >&2; exit 1 ;;
  *) echo "unexpected docker $*" >&2; exit 99 ;;
esac"""


def _host(tmp_path: Path, *, docker: str | None = _DOCKER_UP, tools=("pg_dump", "psql"), gh: str | None = None,
          creds_store: str = "", helper: str | None = None) -> dict[str, str]:
    """A HOME and a PATH of fake binaries only; the environment the probe runs in."""
    bin_dir, home = tmp_path / "bin", tmp_path / "home"
    bin_dir.mkdir()
    (home / ".docker").mkdir(parents=True)
    if docker is not None:
        _tool(bin_dir, "docker", docker)
    for name in tools:
        _tool(bin_dir, name, "exit 0")
    _tool(bin_dir, "gh", gh if gh is not None else 'echo "gh version 2.60.1 (2026-10-01)"')
    if creds_store:
        (home / ".docker" / "config.json").write_text(json.dumps({"auths": {}, "credsStore": creds_store}, indent=2))
    if helper is not None:
        _tool(bin_dir, f"docker-credential-{creds_store}", helper)
    # Room to work, as `df -Pk` reports it; FREE_KB sets the Available column. vm_stat is read only where no
    # /proc/meminfo exists (macOS), and then reports 4 GiB.
    _tool(bin_dir, "df", 'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"; '
                         'echo "tmpfs 16000000 1000 ${FREE_KB:-8000000} 1% $2"')
    _tool(bin_dir, "vm_stat", 'echo "Mach Virtual Memory Statistics: (page size of 16384 bytes)"; '
                              'echo "Pages free:                              262144."')
    # The lane's login shell, minus startup files: a real one would read /etc/profile, which puts this machine's own
    # tools back on the PATH. It runs `-c <command>` under exactly the PATH given.
    _tool(bin_dir, "login-sh", 'while [ "$#" -gt 0 ] && [ "$1" != -c ]; do shift; done; exec /bin/sh -c "$2"')
    return {"PATH": str(bin_dir), "HOME": str(home), "PRESENT": "", "SHELL": str(bin_dir / "login-sh")}


def _probe(env: dict[str, str]) -> list[dict]:
    done = run_argv_split(lane_prereqs.script_argv(), env=env)
    return lane_prereqs.reply_rows(done.returncode, done.mixed, remote=False)


def test_the_probe_with_docker_stopped_names_the_gap_and_checks_no_image(tmp_path):
    env = _host(tmp_path, docker=f'echo "{DOCKER_DOWN}" >&2; exit 1')
    rows = _by_check(_probe(env))
    assert rows["docker daemon"]["detail"] == f"the docker daemon did not answer (exit 1): {DOCKER_DOWN}; start it"
    assert rows["smoke image postgres"]["detail"] == "not checked: the docker daemon did not answer"
    assert lane_prereqs.capabilities(list(rows.values())) == frozenset()


def test_the_probe_without_pg_dump_names_it(tmp_path):
    env = _host(tmp_path, tools=("psql",))
    rows = _by_check(_probe(env))
    assert rows["pg_dump"]["ok"] is False
    assert rows["pg_dump"]["detail"].startswith("not on the lane PATH")
    assert rows["psql"] == {"check": "psql", "ok": True, "detail": f"{env['PATH']}/psql"}


def test_the_probe_without_docker_names_it(tmp_path):
    rows = _by_check(_probe(_host(tmp_path, docker=None)))
    assert rows["docker daemon"]["detail"].startswith("no docker on the lane PATH")


def test_the_probe_asks_the_configured_credential_helper_for_an_absent_image(tmp_path):
    helper = f'read server; [ "$1" = get ] || exit 9; echo "$server: {KEYCHAIN}" >&2; exit 1'
    env = _host(tmp_path, creds_store="desktop", helper=helper)
    env["PRESENT"] = lane_prereqs.GARAGE_IMAGE
    rows = _by_check(_probe(env))
    assert rows["smoke image garage"]["ok"] is True
    assert rows["smoke image postgres"]["detail"] == (
        f"absent, and docker pull would fail in docker-credential-desktop (exit 1): https://index.docker.io/v1/: "
        f"{KEYCHAIN}; pull it once in a session that can reach the credential store")
    # A registry named in the image is the server the helper is asked about.
    assert "data.forgejo.org: " in rows["smoke image forgejo"]["detail"]


def test_the_probe_with_a_credsstore_whose_helper_is_absent_names_the_helper(tmp_path):
    rows = _by_check(_probe(_host(tmp_path, creds_store="desktop")))
    assert rows["smoke image postgres"]["detail"] == (
        "absent, and docker pull would fail: docker-credential-desktop (credsStore) is not on the lane PATH")


def test_the_probe_reports_a_gh_shim(tmp_path):
    rows = _by_check(_probe(_host(tmp_path, gh=f'echo "{SHIM}" >&2; exit 1')))
    assert rows["gh"]["detail"] == f"{tmp_path / 'bin' / 'gh'} is not a usable gh (exit 1): {SHIM}"


def test_the_probe_measures_tmp_and_names_a_host_short_on_room(tmp_path):
    env = {**_host(tmp_path), "FREE_KB": "512000", "TMPDIR": str(tmp_path / "lane-tmp")}
    rows = _by_check(_probe(env))
    assert rows["tmp headroom /tmp"] == {"check": "tmp headroom /tmp", "ok": False, "detail":
                                         "500 MiB free, under the 2048 MiB a lane needs; clear old build and test "
                                         "directories under /tmp"}
    assert rows[f"tmp headroom {tmp_path / 'lane-tmp'}"]["ok"] is False
    assert not rows["free memory"]["detail"].startswith("not measured")  # this machine's own memory, read for real
    assert lane_prereqs.capabilities(list(rows.values())) == frozenset({"docker"})


def test_the_probe_without_df_cannot_measure_tmp(tmp_path):
    env = _host(tmp_path)
    (tmp_path / "bin" / "df").unlink()
    assert _by_check(_probe(env))["tmp headroom /tmp"] == {
        "check": "tmp headroom /tmp", "ok": False, "detail": "not measured: df -Pk /tmp gave nothing"}


def test_the_probe_passes_on_a_ready_host_and_running_it_twice_changes_nothing(tmp_path):
    env = _host(tmp_path, creds_store="osxkeychain", helper='echo "credentials not found in native keychain"; exit 1')
    before = sorted((p, p.stat().st_mtime_ns) for p in tmp_path.rglob("*"))
    first, second = _probe(env), _probe(env)
    assert first == second
    # Free memory is this machine's own, read for real; every other row answers from the fake host.
    assert all(row["ok"] for row in first if row["check"] != "free memory"), first
    assert lane_prereqs.capabilities(first) == frozenset({"docker"})
    assert sorted((p, p.stat().st_mtime_ns) for p in tmp_path.rglob("*")) == before


# --- the chair's login check, then placement, over a real store ------------------------------------------------------


def _store(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir()
    open_store(run_store.store_url_for(runs), "2026-01-01T00:00:00Z").close()
    return runs


def _checked_rows(tmp_path: Path, env: dict[str, str]) -> list[dict[str, object]]:
    """The loop's own host, logged in, checked by the real login check through the real store and runner."""
    _tool(tmp_path / "bin", "cox", 'echo \'{"loggedIn": true, "tokenFile": true}\'')
    runs = _store(tmp_path)
    name = socket.gethostname()
    cli_run = lambda argv: store_cli.reply_row(*store_cli.runner(runs)(argv))  # noqa: E731
    cli_run(["host", "upsert", name, "--ssh", f"operator@{name}", "--capacity", "2", "--by", "test"])
    check_login_on_host(name, f"operator@{name}", {}, NOW, partial(run_argv_split, env=env, timeout=60), cli_run)
    return run_store.hosts(runs)


def _facts(rows: list[dict[str, object]]) -> dict:
    hosts = [{"name": str(row["name"]), "capacity": 2, "live_runs": 0} for row in rows]
    return {"dispatch": {"max_in_flight": 2, "hosts": hosts}, "login_hosts": rows}


def test_with_docker_stopped_the_chair_places_no_lane_on_the_host_and_says_why(tmp_path):
    env = _host(tmp_path, docker=f'echo "{DOCKER_DOWN}" >&2; exit 1')
    rows = _checked_rows(tmp_path, env)
    name = socket.gethostname()

    assert [(slot[0], slot[3]) for slot in chair_plan_fill.host_free_slots(_facts(rows))] == [(name, 0)]
    (notice,) = lane_prereqs.lane_needs_chair(rows)
    (line,) = chair_report.needs_chair_items([notice])
    assert line.startswith(f"needs chair: {name}:lane_prereqs docker daemon: the docker daemon did not answer")
    assert chair_login_watch.versions_of(rows[0])["login_ok"] is True


def test_with_pg_dump_missing_the_chair_places_no_lane_on_the_host(tmp_path):
    rows = _checked_rows(tmp_path, _host(tmp_path, tools=("psql",)))
    assert [slot[3] for slot in chair_plan_fill.host_free_slots(_facts(rows))] == [0]
    assert "pg_dump: not on the lane PATH" in lane_prereqs.lane_blocked(rows)[socket.gethostname()]


def test_a_ready_host_keeps_its_lanes_and_advertises_docker(tmp_path):
    rows = _checked_rows(tmp_path, _host(tmp_path))
    assert [slot[3] for slot in chair_plan_fill.host_free_slots(_facts(rows))] == [2]
    assert chair_login_watch.versions_of(rows[0])["lane_capabilities"] == ["docker"]
    assert lane_prereqs.lane_needs_chair(rows) == []
