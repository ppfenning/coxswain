"""MCP write tools: each call becomes one chair bus entry handed to a send port, nothing else is written.

The caller string that the protocol layer passes to run is the conversation id, so every entry carries it as both
`caller` and `conversation` and the chair's replies stay in that conversation. All five tools send `chat_directive`;
none asks the chair a question, so `chat_question` is unused here. A priority is an integer or a numeric string:
the queue has no keyword table, so any other word is refused.
"""

from collections.abc import Callable, Collection, Mapping

from agent_tools.intake_fields import normalise, validate
from agent_tools.mcp_protocol import ToolError, ToolSpec
from agent_tools.queue_rows import coerce_priority

__all__ = ["write_tools"]

Send = Callable[[str, dict], str]
_EVENT = "chat_directive"
_STAGES = ("draft", "ready")
_KINDS = ("hold", "release", "scope", "enable")
_KIND_FIELD = {"hold": "initiative", "release": "initiative", "scope": "initiative", "enable": "feature"}
_KIND_EFFECT = {
    "hold": "hold initiative {target}",
    "release": "release initiative {target}",
    "scope": "scope initiative {target}",
    "enable": "enable feature {target}",
}


def _text(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    return "" if value is None else str(value).strip()


def _required(arguments: Mapping[str, object], key: str) -> str:
    value = _text(arguments, key)
    if not value:
        raise ToolError(f"{key} is required")
    return value


def _stage(arguments: Mapping[str, object]) -> str:
    stage = _text(arguments, "stage") or "draft"
    if stage not in _STAGES:
        raise ToolError(f"stage {stage!r} must be one of {', '.join(_STAGES)}")
    return stage


def _int_priority(value: object) -> int:
    coerced = coerce_priority(value)
    if isinstance(coerced, int) and not isinstance(coerced, bool):
        return coerced
    raise ToolError(f"priority {value!r} must be a whole number; higher runs first")


def _entry(tool: str, caller: str, now: str, args: Mapping[str, object]) -> dict:
    return {"caller": caller, "conversation": caller, "tool": tool, "at": now, **args}


def _result(status: str, message_id: str, will_change: str) -> dict:
    return {"status": status, "message_id": message_id, "will_change": will_change}


def _sent(send: Send, entry: dict, will_change: str) -> dict:
    return _result("sent", send(_EVENT, entry), will_change)


def _file_intake(send: Send, now: str, repos: Collection[str], epics: Collection[str]) -> Callable[..., dict]:
    def run(arguments: Mapping[str, object], caller: str) -> dict:
        title = _text(arguments, "title")
        stage = _text(arguments, "stage") or "draft"
        refusals = [
            *validate(
                _text(arguments, "repo"),
                _text(arguments, "epic"),
                arguments.get("priority"),
                _text(arguments, "target_release"),
                repos,
                epics,
            ),
            *([] if title else ["title is required"]),
            *([] if stage in _STAGES else [f"stage {stage!r} must be one of {', '.join(_STAGES)}"]),
        ]
        if refusals:
            raise ToolError("; ".join(refusals))
        fields = normalise(
            _text(arguments, "repo"),
            _text(arguments, "epic"),
            arguments.get("priority"),
            _text(arguments, "target_release"),
        )
        args = {**fields, "title": title, "body": _text(arguments, "body"), "stage": stage}
        repo = args["repo"]
        return _sent(send, _entry("file_intake", caller, now, args), f"File intake {title!r} in {repo} as {stage}.")

    return run


def _set_stage(send: Send, now: str) -> Callable[..., dict]:
    def run(arguments: Mapping[str, object], caller: str) -> dict:
        initiative = _required(arguments, "initiative")
        stage = _stage(arguments)
        entry = _entry("set_stage", caller, now, {"initiative": initiative, "stage": stage})
        return _sent(send, entry, f"Set the stage of initiative {initiative} to {stage}.")

    return run


def _set_priority(send: Send, now: str) -> Callable[..., dict]:
    def run(arguments: Mapping[str, object], caller: str) -> dict:
        initiative = _required(arguments, "initiative")
        priority = _int_priority(arguments.get("priority"))
        entry = _entry("set_priority", caller, now, {"initiative": initiative, "priority": priority})
        return _sent(send, entry, f"Set the priority of initiative {initiative} to {priority}.")

    return run


def _send_directive(send: Send, now: str) -> Callable[..., dict]:
    def run(arguments: Mapping[str, object], caller: str) -> dict:
        kind = _text(arguments, "kind")
        if kind not in _KINDS:
            raise ToolError(f"kind {kind!r} must be one of {', '.join(_KINDS)}")
        field = _KIND_FIELD[kind]
        target = _required(arguments, field)
        entry = _entry("send_directive", caller, now, {"kind": kind, field: target})
        return _sent(send, entry, f"Ask the chair to {_KIND_EFFECT[kind].format(target=target)}.")

    return run


def _cut_release(send: Send, now: str) -> Callable[..., dict]:
    def run(arguments: Mapping[str, object], caller: str) -> dict:
        version = _required(arguments, "version")
        confirm = _text(arguments, "confirm")
        will_change = f"Cut release {version}."
        if confirm != version:
            return _result("needs_confirm", "", f"{will_change} Pass confirm equal to {version!r} to send it.")
        return _sent(send, _entry("cut_release", caller, now, {"version": version, "confirm": confirm}), will_change)

    return run


def _schema(properties: Mapping[str, object], required: list[str]) -> dict:
    return {"type": "object", "properties": dict(properties), "required": required, "additionalProperties": False}


_STR = {"type": "string"}
_STAGE = {"type": "string", "enum": list(_STAGES)}
_PRIORITY = {"description": "A whole number; higher runs first. A numeric string is accepted."}


def write_tools(
    send: Send,
    now: str,
    *,
    known_repos: Collection[str],
    known_epics: Collection[str],
) -> list[ToolSpec]:
    """The five write tools. `now` is an ISO timestamp from the caller; `send` returns the message id."""
    return [
        ToolSpec(
            "file_intake",
            "Ask the chair to file an intake item. Validated here; nothing is sent when a field is refused.",
            _schema(
                {
                    "repo": _STR,
                    "epic": _STR,
                    "title": _STR,
                    "body": _STR,
                    "priority": _PRIORITY,
                    "target_release": _STR,
                    "stage": _STAGE,
                },
                ["repo", "title"],
            ),
            True,
            _file_intake(send, now, known_repos, known_epics),
        ),
        ToolSpec(
            "set_stage",
            "Ask the chair to move an initiative to draft or ready.",
            _schema({"initiative": _STR, "stage": _STAGE}, ["initiative", "stage"]),
            True,
            _set_stage(send, now),
        ),
        ToolSpec(
            "set_priority",
            "Ask the chair to set an initiative's priority to a whole number.",
            _schema({"initiative": _STR, "priority": _PRIORITY}, ["initiative", "priority"]),
            True,
            _set_priority(send, now),
        ),
        ToolSpec(
            "send_directive",
            "Ask the chair to hold, release or scope an initiative, or to enable a feature.",
            _schema(
                {"kind": {"type": "string", "enum": list(_KINDS)}, "initiative": _STR, "feature": _STR},
                ["kind"],
            ),
            True,
            _send_directive(send, now),
        ),
        ToolSpec(
            "cut_release",
            "Ask the chair to cut a release. Sends nothing unless confirm equals the version.",
            _schema({"version": _STR, "confirm": _STR}, ["version"]),
            True,
            _cut_release(send, now),
            destructive=True,
        ),
    ]
