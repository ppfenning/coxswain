"""MCP stdio server: joins the read and write tools into one handler and runs the line loop.

The handler is the single object every transport carries. This module binds no data source and reads no profile;
the command that starts it supplies the ports and the caller label.
"""

from collections.abc import Callable, Collection, Iterable, Mapping
from typing import NamedTuple

from agent_tools.mcp_protocol import INVALID_REQUEST, PARSE_ERROR, ToolSpec, decode, encode, handle
from agent_tools.mcp_read_tools import ReadPorts, read_tools
from agent_tools.mcp_write_tools import Send, write_tools

__all__ = ["Ports", "build_tools", "make_handler", "serve_stdio"]

Handler = Callable[[Mapping], dict | None]


class Ports(NamedTuple):
    """Everything the tools reach the outside world through. `known_repos` and `known_epics` bound file_intake."""

    read: ReadPorts
    send: Send
    now: Callable[[], str]
    known_repos: Collection[str] = ()
    known_epics: Collection[str] = ()


def build_tools(ports: Ports) -> list[ToolSpec]:
    """The read tools followed by the write tools; raises ValueError naming any tool name used twice."""
    tools = [
        *read_tools(ports.read),
        *write_tools(ports.send, ports.now(), known_repos=ports.known_repos, known_epics=ports.known_epics),
    ]
    names = [tool.name for tool in tools]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate tool names: {', '.join(duplicates)}")
    return tools


def make_handler(ports: Ports, caller: str) -> Handler:
    """One handler for any transport. write_tools takes a fixed timestamp, so tools are rebuilt per message."""
    build_tools(ports)  # refuse a duplicate name now, not on the first request
    return lambda message: handle(message, build_tools(ports), caller)


def _is_decode_error(decoded: Mapping) -> bool:
    """True for the error `decode` returns for an unparseable line or a non-object: id None, no method, one of two codes."""
    error = decoded.get("error")
    return (
        decoded.get("id") is None
        and "method" not in decoded
        and isinstance(error, Mapping)
        and error.get("code") in (PARSE_ERROR, INVALID_REQUEST)
    )


def serve_stdio(lines: Iterable[str], write: Callable[[str], None], handler: Handler) -> int:
    """Answer each input line through `write`; only protocol messages are written. Returns 0 when input ends."""
    for line in lines:
        decoded = decode(line)
        if decoded is None:
            continue
        response = decoded if _is_decode_error(decoded) else handler(decoded)
        if response is not None:
            write(encode(response) + "\n")
    return 0
