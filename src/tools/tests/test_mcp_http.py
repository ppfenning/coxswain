import http.client
import json
import threading

import pytest

from agent_tools import mcp_http
from agent_tools.mcp_http import authenticate, check_bind, parse_tokens, respond
from agent_tools.mcp_protocol import ToolSpec, handle

TOKEN = "t" * 24
TOKENS = {TOKEN: "alice", "u" * 24: "bob"}
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LIST = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()


def _recorder():
    callers = []

    def make_handler(caller):
        callers.append(caller)
        return lambda message: handle(message, [], caller)

    return callers, make_handler


@pytest.mark.parametrize(
    "host",
    [
        "100.64.1.2",  # clean: allow
        "192.168.1.5",  # clean: allow
        "10.0.0.1",
        "127.0.0.1",
        "fd7a:115c:a1e0::1",
    ],
)
def test_check_bind_allows(host):
    assert check_bind(host) is None


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "example.com"])
def test_check_bind_refuses_and_names_the_host(host):
    reason = check_bind(host)
    assert isinstance(reason, str) and host in reason


def test_parse_tokens_maps_token_to_label():
    assert parse_tokens(f"# keys\n\nalice {TOKEN}\n") == {TOKEN: "alice"}


@pytest.mark.parametrize("text", ["", "# only a comment\n", "alice short", f"alice {TOKEN} extra", TOKEN])
def test_parse_tokens_rejects(text):
    with pytest.raises(ValueError):
        parse_tokens(text)


def test_authenticate():
    assert authenticate(f"Bearer {TOKEN}", TOKENS) == "alice"
    assert authenticate("Bearer " + "x" * 24, TOKENS) is None
    assert authenticate(None, TOKENS) is None
    assert authenticate(f"Basic {TOKEN}", TOKENS) is None


def test_respond_without_a_token_is_401_and_never_calls_the_handler():
    callers, make_handler = _recorder()
    status, headers, _ = respond("POST", "/mcp", {}, LIST, TOKENS, make_handler)
    assert (status, headers["WWW-Authenticate"], callers) == (401, "Bearer", [])


def test_respond_refuses_an_origin_even_with_a_token():
    callers, make_handler = _recorder()
    status, _, _ = respond("POST", "/mcp", {**AUTH, "Origin": "http://evil.test"}, LIST, TOKENS, make_handler)
    assert (status, callers) == (403, [])


def test_respond_method_and_path():
    _, make_handler = _recorder()
    assert respond("GET", "/mcp", AUTH, b"", TOKENS, make_handler)[0] == 405
    assert respond("POST", "/other", AUTH, LIST, TOKENS, make_handler)[0] == 404


def test_respond_oversize_body_is_413():
    _, make_handler = _recorder()
    assert respond("POST", "/mcp", AUTH, b" " * ((1 << 20) + 1), TOKENS, make_handler)[0] == 413


def test_respond_200_carries_the_http_caller():
    callers, make_handler = _recorder()
    status, headers, body = respond("POST", "/mcp", AUTH, LIST, TOKENS, make_handler)
    assert (status, headers["Content-Type"], json.loads(body)["result"], callers) == (
        200,
        "application/json",
        {"tools": []},
        ["http:alice"],
    )


def test_respond_notification_is_202_and_empty():
    _, make_handler = _recorder()
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    assert respond("POST", "/mcp", AUTH, note, TOKENS, make_handler) == (202, {}, b"")


def test_respond_blank_and_unparseable_bodies():
    callers, make_handler = _recorder()
    assert respond("POST", "/mcp", AUTH, b"  ", TOKENS, make_handler)[0] == 400
    status, _, body = respond("POST", "/mcp", AUTH, b"{not json", TOKENS, make_handler)
    assert (status, json.loads(body)["error"]["code"], callers) == (200, -32700, [])


def test_loopback_server_rejects_then_serves(capsys):
    spec = ToolSpec("ping", "d", {"type": "object"}, False, lambda arguments, caller: {})
    server = mcp_http._make_server("127.0.0.1", 0, lambda caller: lambda m: handle(m, [spec], caller), TOKENS)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        def post(headers):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request("POST", "/mcp", LIST, headers)
            reply = conn.getresponse()
            return reply.status, reply.read()

        assert post({})[0] == 401
        status, body = post(AUTH)
        assert status == 200
        assert [t["name"] for t in json.loads(body)["result"]["tools"]] == ["ping"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    logged = capsys.readouterr().err
    assert "POST /mcp" in logged and TOKEN not in logged


def test_serve_refuses_a_public_bind_without_binding(monkeypatch, capsys):
    def boom(*args):
        raise AssertionError("a socket was created")

    monkeypatch.setattr(mcp_http, "_make_server", boom)
    assert mcp_http.serve("0.0.0.0", 0, lambda caller: None, TOKENS) == 2
    assert "0.0.0.0" in capsys.readouterr().err
