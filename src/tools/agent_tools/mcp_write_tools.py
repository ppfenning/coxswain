"""MCP write tools. A plain write acts and reports what happened; a request that needs the chair is sent to it.

The store is the board, so filing an intake, setting a stage or a priority, and holding or releasing are row writes
the `cox route` commands already make. With an `act` port those tools run that same command and return its real
outcome, `done` or `refused` with the reason, and put one `chat_action` entry on the bus so the chair and the record
see it. `set_setting` edits the owner's cartridge the same way, after showing the diff.

Two things still go to the chair as a `chat_directive` and return `sent`: `cut_release`, which is destructive and
keeps its confirm step, and the directives that need judgment (`scope`, `enable`). Without an `act` port (a server
that does not run beside the store and the cartridge) every tool sends a directive, as all of them once did.

The caller string that the protocol layer passes to run is the conversation id, so every entry carries it as both
`caller` and `conversation`. A priority is an integer or a numeric string: the queue has no keyword table, so any
other word is refused.
"""

from collections.abc import Callable, Collection, Mapping

from agent_tools.intake_fields import normalise, validate
from agent_tools.mcp_protocol import ToolError, ToolSpec
from agent_tools.queue_rows import coerce_priority

__all__ = ["Act", "Send", "write_tools"]

Send = Callable[[str, dict], str]
# `act(tool, args)` carries one plain write out and returns (it happened, what the command said).
Act = Callable[[str, Mapping[str, object]], tuple[bool, str]]
_EVENT = "chat_directive"
_ACTED = "chat_action"  # a record of something already done; never a request, so nothing acts on it twice
_DIRECT_KINDS = ("hold", "release")
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


def _acted(send: Send, act: Act | None, entry: dict, will_change: str) -> dict:
    """Carry the write out and say what happened; with no `act` port, ask the chair instead."""
    if act is None:
        return _sent(send, entry, will_change)
    args = {k: v for k, v in entry.items() if k not in ("caller", "conversation", "tool", "at")}
    ok, detail = act(str(entry["tool"]), {**args, "caller": entry["caller"]})
    status = "done" if ok else "refused"
    message_id = send(_ACTED, {**entry, "status": status, "detail": detail})
    return {**_result(status, message_id, will_change), "detail": detail}


def _file_intake(send: Send, now: str, repos: Collection[str], epics: Collection[str], act: Act | None = None) -> Callable[..., dict]:
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
        return _acted(send, act, _entry("file_intake", caller, now, args), f"File intake {title!r} in {repo} as {stage}.")

    return run


def _set_stage(send: Send, now: str, act: Act | None = None) -> Callable[..., dict]:
    def run(arguments: Mapping[str, object], caller: str) -> dict:
        initiative = _required(arguments, "initiative")
        stage = _stage(arguments)
        entry = _entry("set_stage", caller, now, {"initiative": initiative, "stage": stage})
        return _acted(send, act, entry, f"Set the stage of initiative {initiative} to {stage}.")

    return run


def _set_priority(send: Send, now: str, act: Act | None = None) -> Callable[..., dict]:
    def run(arguments: Mapping[str, object], caller: str) -> dict:
        initiative = _required(arguments, "initiative")
        priority = _int_priority(arguments.get("priority"))
        entry = _entry("set_priority", caller, now, {"initiative": initiative, "priority": priority})
        return _acted(send, act, entry, f"Set the priority of initiative {initiative} to {priority}.")

    return run


def _send_directive(send: Send, now: str, act: Act | None = None) -> Callable[..., dict]:
    def run(arguments: Mapping[str, object], caller: str) -> dict:
        kind = _text(arguments, "kind")
        if kind not in _KINDS:
            raise ToolError(f"kind {kind!r} must be one of {', '.join(_KINDS)}")
        field = _KIND_FIELD[kind]
        target = _required(arguments, field)
        if kind in _DIRECT_KINDS and act is not None:
            # A hold says why and who; the queue keeps both beside the held item.
            reason = _text(arguments, "reason") or f"held over MCP by {caller}"
            entry = _entry("send_directive", caller, now, {"kind": kind, field: target, **({"reason": reason} if kind == "hold" else {})})
            return _acted(send, act, entry, f"{_KIND_EFFECT[kind].format(target=target).capitalize()}.")
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


def _set_setting(send: Send, now: str, act: Act | None) -> Callable[..., dict]:
    """Change one setting in the owner's cartridge. The first call returns the diff and writes nothing; a call with
    `confirm` true writes it. A server with no `act` port cannot reach the cartridge and says so."""

    def run(arguments: Mapping[str, object], caller: str) -> dict:
        key, value = _required(arguments, "key"), _required(arguments, "value")
        will_change = f"Set cartridge setting {key} to {value!r}."
        if act is None:
            raise ToolError("this server cannot change the cartridge: it does not run on the machine that holds it")
        if arguments.get("confirm") is not True:
            ok, detail = act("set_setting", {"key": key, "value": value, "dry_run": True, "caller": caller})
            if not ok:
                return {**_result("refused", "", will_change), "detail": detail}
            return {**_result("needs_confirm", "", f"{will_change} Pass confirm=true to write it."), "detail": detail}
        return _acted(send, act, _entry("set_setting", caller, now, {"key": key, "value": value}), will_change)

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
    act: Act | None = None,
) -> list[ToolSpec]:
    """The six write tools. `now` is an ISO timestamp from the caller; `send` returns the message id; `act` carries
    a plain write out (see the module docstring), and without it every tool only asks the chair."""
    return [
        ToolSpec(
            "file_intake",
            "File an intake item. Validated here; nothing is written or sent when a field is refused.",
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
            _file_intake(send, now, known_repos, known_epics, act),
        ),
        ToolSpec(
            "set_stage",
            "Move a queued intake to draft or ready.",
            _schema({"initiative": _STR, "stage": _STAGE}, ["initiative", "stage"]),
            True,
            _set_stage(send, now, act),
        ),
        ToolSpec(
            "set_priority",
            "Set an initiative's or queued intake's priority to a whole number.",
            _schema({"initiative": _STR, "priority": _PRIORITY}, ["initiative", "priority"]),
            True,
            _set_priority(send, now, act),
        ),
        ToolSpec(
            "send_directive",
            "Hold or release an initiative (done at once; a hold takes a reason), or ask the chair to scope an "
            "initiative or enable a feature.",
            _schema(
                {"kind": {"type": "string", "enum": list(_KINDS)}, "initiative": _STR, "feature": _STR, "reason": _STR},
                ["kind"],
            ),
            True,
            _send_directive(send, now, act),
        ),
        ToolSpec(
            "cut_release",
            "Ask the chair to cut a release. Sends nothing unless confirm equals the version.",
            _schema({"version": _STR, "confirm": _STR}, ["version"]),
            True,
            _cut_release(send, now),
            destructive=True,
        ),
        ToolSpec(
            "set_setting",
            "Change one setting in the cartridge its owner keeps. Returns the diff and writes nothing unless "
            "confirm is true. Operator-only settings are refused.",
            _schema({"key": _STR, "value": _STR, "confirm": {"type": "boolean"}}, ["key", "value"]),
            True,
            _set_setting(send, now, act),
        ),
    ]
