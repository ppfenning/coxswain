"""Optional token-guarded HTTP transport for the MCP core, standard library only.

Nothing here defaults a host, a port or a token. There is no TLS: the tailnet and ssh
provide transport encryption, and a bind to a public address is refused.
"""

import hmac
import ipaddress
import socket
import sys
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_tools import mcp_protocol

PATH = "/mcp"
MAX_BODY = 1 << 20
MIN_TOKEN = 24

Handler = Callable[[dict], dict | None]
Response = tuple[int, dict[str, str], bytes]

# The product's allow list; the two ranges that .github/check_clean.py flags carry its marker.
_ALLOWED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",  # clean: allow
        "100.64.0.0/10",  # clean: allow
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def check_bind(host: str) -> str | None:
    """Return why this host may not be bound, or None. Only literal private addresses pass."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return f"{host!r} is not a literal IP address; names are never resolved"
    if address.is_unspecified:
        return f"{address} is an unspecified address and would listen on every interface"
    if any(address in network for network in _ALLOWED):
        return None
    return f"{address} is not a loopback, private, tailnet or link-local address"


def parse_tokens(text: str) -> dict[str, str]:
    """Map token to label from 'label token' lines; blank and '#' lines are skipped."""
    lines = [line.split() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if any(len(fields) != 2 for fields in lines):
        raise ValueError("each token line must be 'label token'")
    tokens = {token: label for label, token in lines}
    if not tokens:
        raise ValueError("no tokens found")
    if any(len(token) < MIN_TOKEN for token in tokens):
        raise ValueError(f"every token must be at least {MIN_TOKEN} characters")
    return tokens


def authenticate(authorization: str | None, tokens: Mapping[str, str]) -> str | None:
    """Return the label for a 'Bearer <token>' header, comparing every stored token in constant time."""
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not presented.strip():
        return None
    candidate = presented.strip().encode()
    matches = [label for token, label in tokens.items() if hmac.compare_digest(token.encode(), candidate)]
    return matches[0] if matches else None


def _reply(status: int, body: bytes = b"", **headers: str) -> Response:
    return status, dict(headers), body


def respond(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    tokens: Mapping[str, str],
    handler: Callable[[str], Handler],
) -> Response:
    """Answer one request. `handler` is a factory taking the caller 'http:<label>'."""
    seen = {name.lower(): value for name, value in headers.items()}
    if path != PATH:
        return _reply(404)
    if method != "POST":
        return _reply(405, Allow="POST")
    if "origin" in seen:  # a browser sent this; refuse it as a guard against DNS rebinding
        return _reply(403)
    label = authenticate(seen.get("authorization"), tokens)
    if label is None:
        return _reply(401, **{"WWW-Authenticate": "Bearer"})
    if len(body) > MAX_BODY:
        return _reply(413)
    try:
        message = mcp_protocol.decode(body.decode("utf-8"))
    except UnicodeDecodeError:
        return _reply(400)
    if message is None:  # a blank body carries no request
        return _reply(400)
    if "error" in message and "method" not in message:  # decode answers a parse failure with an error response
        return _json(message)
    result = handler(f"http:{label}")(message)
    return _reply(202) if result is None else _json(result)


def _json(message: Mapping[str, object]) -> Response:
    return _reply(200, mcp_protocol.encode(message).encode(), **{"Content-Type": "application/json"})


class _Server6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _make_server(
    host: str, port: int, make_handler: Callable[[str], Handler], tokens: Mapping[str, str]
) -> ThreadingHTTPServer:
    class RequestHandler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            try:
                length = max(int(self.headers.get("Content-Length") or 0), 0)
            except ValueError:
                length = 0
            body = self.rfile.read(min(length, MAX_BODY + 1))  # an oversized body is never read in full
            status, out_headers, out = respond(
                self.command, self.path.split("?", 1)[0], dict(self.headers), body, tokens, make_handler
            )
            self.close_connection = True
            self.send_response(status)
            for name, value in out_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(out)

        do_POST = do_GET = do_PUT = do_DELETE = do_PATCH = _serve

    server_class = _Server6 if ipaddress.ip_address(host).version == 6 else ThreadingHTTPServer
    return server_class((host, port), RequestHandler)


def serve(host: str, port: int, make_handler: Callable[[str], Handler], tokens: Mapping[str, str]) -> int:
    """Serve until interrupted. Returns 2, binding nothing, when the host is refused."""
    refusal = check_bind(host)
    if refusal is not None:
        print(f"refusing to bind: {refusal}", file=sys.stderr)
        return 2
    server = _make_server(host, port, make_handler, tokens)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
