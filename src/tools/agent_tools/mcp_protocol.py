"""Pure MCP JSON-RPC core: initialize, tools/list and tools/call, standard library only."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

SERVER_NAME = "coxswain"
# Placeholder, not the release version: MCP's Implementation type requires a version beside the name,
# and this pure module cannot read the installed package's. Wire it to the release version in its own change.
SERVER_VERSION = "1.0.0"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

_GENERIC_FAILURE = "The tool failed unexpectedly."

_TYPES: Mapping[str, Callable[[object], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, Mapping),
    "array": lambda v: isinstance(v, list | tuple),
}


class ToolError(Exception):
    """A tool-level failure; its message is shown to the client."""


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its schema, its safety hints and the callable behind it."""

    name: str
    description: str
    input_schema: Mapping[str, object]
    writes: bool
    run: Callable[[Mapping[str, object], str], dict]
    destructive: bool = False


def validate_arguments(schema: Mapping[str, object], arguments: Mapping[str, object]) -> list[str]:
    """Return one message per problem; empty means valid. Checks top-level keys only."""
    properties = schema.get("properties") or {}
    required = schema.get("required") or ()
    missing = [f"missing required argument: {key}" for key in required if key not in arguments]
    unknown = (
        [f"unknown argument: {key}" for key in arguments if key not in properties]
        if schema.get("additionalProperties") is False
        else []
    )
    problems = [
        problem
        for key, value in arguments.items()
        if key in properties
        for problem in _value_problems(key, properties[key], value)
    ]
    return [*missing, *unknown, *problems]


def _value_problems(key: str, spec: Mapping[str, object], value: object) -> list[str]:
    declared = spec.get("type")
    check = _TYPES.get(declared) if isinstance(declared, str) else None
    wrong_type = [f"argument {key} must be of type {declared}"] if check and not check(value) else []
    allowed = spec.get("enum")
    wrong_enum = (
        [f"argument {key} must be one of {list(allowed)}"]
        if isinstance(allowed, list | tuple) and value not in allowed
        else []
    )
    return [*wrong_type, *wrong_enum]


def result_response(request_id: object, result: Mapping[str, object]) -> dict:
    """Build a JSON-RPC 2.0 success response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def error_response(request_id: object, code: int, message: str) -> dict:
    """Build a JSON-RPC 2.0 error response."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _initialize(params: Mapping[str, object]) -> dict:
    asked = params.get("protocolVersion")
    version = asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _listing(tool: ToolSpec) -> dict:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": dict(tool.input_schema),
        "annotations": {"readOnlyHint": not tool.writes, "destructiveHint": tool.destructive},
    }


def _tool_result(text: str, structured: Mapping[str, object] | None, is_error: bool) -> dict:
    base = {"content": [{"type": "text", "text": text}], "isError": is_error}
    return base if structured is None else {**base, "structuredContent": dict(structured)}


def _call(request_id: object, params: Mapping[str, object], tools: Sequence[ToolSpec], caller: str) -> dict:
    name = params.get("name")
    arguments = params.get("arguments", {})
    tool = next((t for t in tools if t.name == name), None)
    if not isinstance(name, str) or tool is None:
        return error_response(request_id, INVALID_PARAMS, f"unknown tool: {name}")
    if not isinstance(arguments, Mapping):
        return error_response(request_id, INVALID_PARAMS, "arguments must be an object")
    problems = validate_arguments(tool.input_schema, arguments)
    if problems:
        return error_response(request_id, INVALID_PARAMS, "; ".join(problems))
    try:
        output = tool.run(arguments, caller)
        if not isinstance(output, dict):
            raise TypeError("tool result must be a dict")
        # everything that can fail on a tool's result stays inside the try, so no tool can raise out of handle
        shaped = _tool_result(json.dumps(output, allow_nan=False), output, False)
    except ToolError as failure:
        return result_response(request_id, _tool_result(str(failure), None, True))
    except Exception:  # any tool failure is reported without echoing its text
        return result_response(request_id, _tool_result(_GENERIC_FAILURE, None, True))
    return result_response(request_id, shaped)


def handle(message: Mapping[str, object], tools: Sequence[ToolSpec], caller: str) -> dict | None:
    """Answer one decoded JSON-RPC message, or return None for a notification."""
    if not isinstance(message, Mapping):
        return error_response(None, INVALID_REQUEST, "request must be an object")
    raw_id = message.get("id")
    request_id = raw_id if isinstance(raw_id, str | int) and not isinstance(raw_id, bool) else None
    method = message.get("method")
    params = message.get("params", {})
    if "id" not in message and message.get("jsonrpc") == "2.0" and isinstance(method, str):
        return None  # a notification is never answered, even when its params are malformed
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str) or not isinstance(params, Mapping):
        return error_response(request_id, INVALID_REQUEST, "malformed request")
    if method == "initialize":
        return result_response(request_id, _initialize(params))
    if method in ("ping", "notifications/initialized"):  # with an id it is a request, so it gets a reply
        return result_response(request_id, {})
    if method == "tools/list":
        return result_response(request_id, {"tools": [_listing(t) for t in tools]})
    if method == "tools/call":
        return _call(request_id, params, tools, caller)
    return error_response(request_id, METHOD_NOT_FOUND, f"method not found: {method}")


def decode(line: str) -> dict | None:
    """Parse one line; None for a blank line, an error response dict when it is not a request."""
    if not line.strip():
        return None
    try:
        parsed = json.loads(line)
    except ValueError:
        return error_response(None, PARSE_ERROR, "parse error")
    if not isinstance(parsed, dict):
        return error_response(None, INVALID_REQUEST, "request must be an object")
    return parsed


def encode(response: Mapping[str, object]) -> str:
    """Serialise one message as a single line without a trailing newline."""
    return json.dumps(response, separators=(",", ":"))
