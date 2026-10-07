import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import agent_tools.notify_core
import agent_tools.notify_dispatch
import agent_tools.route
import pytest
from agent_tools.notify_core import NotifyConfig

from devtools import cli

STEPS = [
    {"kind": "tag", "component": "coxswain-tools"},
    {"kind": "pinned", "component": "coxswain-dash"},
    {"kind": "rejoin", "component": "coxswain-docs"},
    {"kind": "tag_self", "component": "coxswain"},
]


@pytest.fixture
def server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.append({"title": self.headers.get("Title"), "body": self.rfile.read(length).decode()})
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/topic", seen
    httpd.shutdown()
    httpd.server_close()


def test_a_successful_cut_posts_once_with_the_release_title(server, tmp_path):
    url, seen = server
    state = tmp_path / "notify-sent.json"
    assert cli._after_release(0, "1.2.3", STEPS, NotifyConfig(url), state, 1000.0) == 0
    assert [post["title"] for post in seen] == ["Release 1.2.3 cut"]
    assert "release_cut:1.2.3" in state.read_text()


def test_a_failed_cut_posts_nothing(server, tmp_path):
    url, seen = server
    assert cli._after_release(2, "1.2.3", STEPS, NotifyConfig(url), tmp_path / "s.json", 1000.0) == 2
    assert seen == []


def test_no_config_writes_no_state_file(tmp_path):
    state = tmp_path / "notify-sent.json"
    assert cli._after_release(0, "1.2.3", STEPS, None, state, 1000.0) == 0
    assert not state.exists()


def test_a_dead_port_still_returns_zero_and_prints_a_line(tmp_path, capsys):
    config = NotifyConfig("http://127.0.0.1:1/topic")
    assert cli._after_release(0, "1.2.3", STEPS, config, tmp_path / "s.json", 1000.0) == 0
    assert capsys.readouterr().out.count("notify: release_cut:1.2.3") == 1


def test_a_raising_post_still_returns_zero_and_prints_a_line(tmp_path, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("down")

    config = NotifyConfig("http://127.0.0.1:1/topic")
    assert cli._after_release(0, "1.2.3", STEPS, config, tmp_path / "s.json", 1000.0, post=boom) == 0
    assert capsys.readouterr().out.count("notify: release_cut:1.2.3") == 1


def test_the_event_names_cut_components_in_plan_order_and_not_pinned():
    event = cli._release_cut_event("1.2.3", STEPS)
    assert (event.kind, event.key, event.title) == ("release_cut", "release_cut:1.2.3", "Release 1.2.3 cut")
    assert event.body == "Release 1.2.3 cut: coxswain-tools, coxswain-docs, coxswain"
    assert "coxswain-dash" not in event.body


def test_notify_config_is_empty_for_no_path_a_missing_file_and_no_notify(tmp_path):
    no_notify = tmp_path / "profile.yaml"
    no_notify.write_text("workspace_dir: /tmp/w\n")
    assert cli._notify_config(None) == (None, None)
    assert cli._notify_config(str(tmp_path / "absent.yaml")) == (None, None)
    assert cli._notify_config(str(no_notify)) == (None, None)


def test_a_raising_dispatch_still_returns_zero_and_prints_a_line(monkeypatch, tmp_path, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("dispatch down")

    monkeypatch.setattr(agent_tools.notify_dispatch, "dispatch", boom)
    config = NotifyConfig("http://127.0.0.1:1/topic")
    assert cli._after_release(0, "1.2.3", STEPS, config, tmp_path / "s.json", 1000.0) == 0
    assert capsys.readouterr().out == "notify: release_cut:1.2.3 failed: dispatch down\n"


def test_notify_config_is_empty_for_a_binary_profile(tmp_path):
    binary = tmp_path / "profile.yaml"
    binary.write_bytes(b"\xff\xfe\x00notify")
    assert cli._notify_config(str(binary)) == (None, None)


def test_notify_config_is_empty_when_parsing_raises_anything(monkeypatch, tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text("anything\n")
    monkeypatch.setattr(agent_tools.route, "parse_profile", lambda text: 1 / 0)
    assert cli._notify_config(str(profile)) == (None, None)


def test_notify_config_branches_on_a_parsed_profile(monkeypatch, tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text("anything\n")
    parsed = {}
    monkeypatch.setattr(agent_tools.route, "parse_profile", lambda text: parsed)
    monkeypatch.setattr(agent_tools.notify_core, "config_from_profile", lambda p: ("config", p["notify"]["ntfy"]))
    parsed.update({"workspace_dir": "/tmp/w"})
    assert cli._notify_config(str(profile)) == (None, None)
    parsed.update({"notify": {"ntfy": "http://n/t"}})
    assert cli._notify_config(str(profile)) == (("config", "http://n/t"), Path("/tmp/w/notify-sent.json"))


def test_profile_flag_wins_over_the_environment_and_parses_on_release():
    assert cli._profile_path("a.yaml", {"AGENT_TOOLS_PROFILE": "b.yaml"}) == "a.yaml"
    assert cli._profile_path(None, {"AGENT_TOOLS_PROFILE": "b.yaml"}) == "b.yaml"
    assert cli._profile_path(None, {}) is None
    assert cli.build_parser().parse_args(["release", "1.2.3", "--profile", "p.yaml"]).profile == "p.yaml"
