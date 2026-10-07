import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import agent_tools.notify_dispatch
import pytest
from agent_tools.notify_core import NotifyConfig

from devtools import cli, release

_TOOLS_REPO_URL = "https://github.com/ppfenning/coxswain-tools"
_MANIFEST = """
[coxswain]
version = "0.2.0"
repo = "ppfenning/coxswain"

[components.tools]
repo = "ppfenning/coxswain-tools"
tag = "v0.2.0"
"""
_INDEX = {"urls": [{"packagetype": "sdist", "url": "https://x/cox-0.2.0.tar.gz", "digests": {"sha256": "b" * 64}}]}


def _fake_run(fail_on=None):
    """A git/gh runner for the tap clone; `fail_on(argv)` picks the one call that returns 1."""
    calls: list = []
    dispatched: list = []

    def run(argv, cwd):
        calls.append(argv)
        if fail_on is not None and fail_on(argv):
            return 1, "refused"
        if argv[:3] == ["gh", "workflow", "run"]:
            dispatched.append(argv)
        if argv[:3] == ["gh", "run", "list"]:
            return 0, json.dumps([{"databaseId": i} for i in ([2, 1] if dispatched else [1])])
        if argv[:3] == ["gh", "run", "view"]:
            return 0, json.dumps({"databaseId": 2, "status": "completed", "conclusion": "success", "url": "https://x/2"})
        if argv[0] == "gh":
            return 0, ""
        if argv[3] == "rev-parse":
            return 0, "main\n"
        if argv[3] == "symbolic-ref":
            return 0, "refs/remotes/origin/main\n"
        return 0, ""

    return calls, run


def _spy_dispatch(monkeypatch, events: list) -> None:
    monkeypatch.setattr(agent_tools.notify_dispatch, "dispatch",
                        lambda sent, config, state, now, **kw: (events.extend(sent), [e.key for e in sent])[1])


def _release_tap_only(tmp_path, monkeypatch, tools_tags=("v0.2.0",), fail_on=None, repos=None):
    """Runs `release 0.2.0 --tap-only` against fakes; returns (rc, runner calls, pushed events).

    `repos`, when given, collects every slug whose remote tags were read."""
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text(_MANIFEST)
    tap = tmp_path / release.TAP_CHECKOUT
    (tap / "Formula").mkdir(parents=True)
    (tap / "Formula" / "cox.rb").write_text("formula\n")
    calls, run = _fake_run(fail_on)
    events: list = []
    read = repos if repos is not None else []
    execute = cli._release_execute
    monkeypatch.setattr(cli, "_remote_tags", lambda repo: (
        read.append(repo), list(tools_tags) if repo.endswith("-tools") else ["v0.2.0"])[1])
    monkeypatch.setattr(cli, "_real_run", run)
    monkeypatch.setattr(cli, "_tools_repository_url", lambda: _TOOLS_REPO_URL)
    monkeypatch.setattr(cli, "_tap_state", lambda directory: "clean")
    monkeypatch.setattr(cli, "_maintainer_remote_url", lambda directory: "git@github.com:ppfenning/coxswain.git")
    monkeypatch.setattr(cli, "_release_execute", lambda *a: execute(
        *a, sleep=lambda s: None, now=iter(range(0, 10**9, 60)).__next__,
        fetch_index=lambda url: _INDEX, fetch_text=lambda url: "a" * 64 + "  asset\n"))
    monkeypatch.setattr(release, "bumped_formula_text", lambda text, *a, **k: text)
    monkeypatch.setattr(cli, "_notify_config", lambda path: (NotifyConfig("http://unused"), tmp_path / "sent.json"))
    _spy_dispatch(monkeypatch, events)
    rc = cli.main(["release", "0.2.0", "--tap-only", "--manifest", str(manifest_path), "--root", str(tmp_path)])
    return rc, calls, events


def test_the_flag_defaults_off():
    parse = cli.build_parser().parse_args
    assert parse(["release", "0.2.0", "--tap-only"]).tap_only is True
    assert parse(["release", "0.2.0"]).tap_only is False


def test_tap_only_runs_just_the_tap_step_and_reads_the_umbrella_tags(tmp_path, monkeypatch, capsys):
    repos: list = []
    rc, calls, events = _release_tap_only(tmp_path, monkeypatch, repos=repos)
    out = capsys.readouterr().out
    assert rc == 0
    assert [line for line in out.splitlines() if line] == ["tap_formula_pr tap: cox 0.2.0"]
    assert not any(c[0] == "git" and c[3] in ("tag", "merge") for c in calls)
    assert next(c for c in calls if c[:2] == ["gh", "pr"])[:3] == ["gh", "pr", "create"]
    assert "ppfenning/coxswain" in repos
    assert [(e.kind, e.title, e.body) for e in events] == [
        ("release_tap", "Release 0.2.0 tap PR opened", "Release 0.2.0 tap formula PR opened: cox 0.2.0")]


def test_tap_only_skips_the_drift_gate_on_a_resume(tmp_path, monkeypatch, capsys):
    def no_gate(*args, **kwargs):
        raise AssertionError("tap-only consulted the drift gate")

    monkeypatch.setattr(cli.release_check, "run_checks", lambda facts: ["a standing drift"])
    monkeypatch.setattr(release, "gate", no_gate)
    rc, _, _ = _release_tap_only(tmp_path, monkeypatch)
    assert rc == 0
    assert "tap_formula_pr tap: cox 0.2.0" in capsys.readouterr().out


def test_tap_only_is_refused_when_a_tag_is_missing_and_the_refusal_notifies(tmp_path, monkeypatch, capsys):
    rc, calls, events = _release_tap_only(tmp_path, monkeypatch, tools_tags=())
    assert rc == 2
    assert "missing on tools" in capsys.readouterr().out
    assert not any(c[0] == "gh" or c[3] in ("checkout", "push") for c in calls)
    assert [e.kind for e in events] == ["release_failed"]
    assert "refuse tools" in events[0].body


def test_a_failed_step_notifies_with_its_name(tmp_path, monkeypatch):
    rc, _, events = _release_tap_only(tmp_path, monkeypatch, fail_on=lambda argv: argv[:3] == ["gh", "pr", "create"])
    assert rc == 2
    assert [(e.kind, e.title) for e in events] == [("release_failed", "Release 0.2.0 failed")]
    assert events[0].key.startswith("release_failed:0.2.0:")
    assert "FAILED tap_formula_pr" in events[0].body


def test_a_full_cut_still_notifies_the_release_cut_as_before(monkeypatch, tmp_path):
    events: list = []
    _spy_dispatch(monkeypatch, events)
    steps = [{"kind": "tag", "component": "tools"}, {"kind": "tap_formula_pr", "component": "tap", "title": "cox 0.2.0"}]
    assert cli._after_release(0, "0.2.0", steps, NotifyConfig("http://unused"), tmp_path / "s.json", 1000.0) == 0
    assert [(e.kind, e.key, e.title, e.body) for e in events] == [
        ("release_cut", "release_cut:0.2.0", "Release 0.2.0 cut", "Release 0.2.0 cut: tools")]


def test_a_second_failed_resume_of_one_version_notifies_again_through_the_real_dispatch(tmp_path):
    seen: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        config = NotifyConfig(f"http://127.0.0.1:{httpd.server_address[1]}/topic")
        state = tmp_path / "notify-sent.json"
        for now in (1000.0, 1060.0):
            assert cli._after_release(2, "0.2.0", [], config, state, now, failed="FAILED tap_formula_pr") == 2
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert len(seen) == 2
    assert all("FAILED tap_formula_pr" in body for body in seen)


@pytest.mark.parametrize("output, expected", [
    ("FAILED tap_formula_pr tap: boom\n", "FAILED tap_formula_pr"),
    ("tag a\nFAILED github_release harness: x\nFAILED tag b: y\n", "FAILED github_release"),
    ("refuse tap: dirty\n", "refuse tap"),
    ("tag a\n", None),
    ("", None)])
def test_the_failed_step_is_the_first_failed_or_refuse_line(output, expected):
    assert cli._failed_step(output) == expected
