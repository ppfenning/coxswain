"""The tools a repo's `.agent-checks` run, and whether a host's lane PATH has them.

Pure except for the injected `run`, the one edge: it takes an argv and returns (exit code, output).
A lane runs under `$SHELL -l -i -c` on a remote host, so a tool the login shell cannot find is a tool the checks cannot run.
"""
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent_tools.remote_argv import login_shell_argv

_SEGMENTS = re.compile(r"&&|;")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")
_LOOKUP = 'for t in "$@"; do command -v "$t" >/dev/null 2>&1 || echo "$t"; done'

Probe = Callable[[list[str]], tuple[int, str]]


@dataclass(frozen=True)
class CheckTools:
    found: tuple[str, ...]
    unparsed: tuple[str, ...]


def check_lines(text: str) -> list[str]:
    """The non-blank, non-comment lines of an `.agent-checks` file."""
    stripped = (line.strip() for line in text.splitlines())
    return [line for line in stripped if line and not line.startswith("#")]


def _segment_tool(segment: str) -> tuple[str, ...] | None:
    """The tool a segment runs, () for a segment that runs nothing on the PATH, None when it cannot be read.

    A `cd`, an empty segment and a path such as `./run` run nothing found by PATH lookup."""
    try:
        words = shlex.split(segment)
    except ValueError:
        return None
    rest = next((words[i:] for i, w in enumerate(words) if not _ASSIGNMENT.match(w)), [])
    if not rest or rest[0] == "cd" or "/" in rest[0]:
        return ()
    return (rest[0],) if _NAME.fullmatch(rest[0]) else None


def parse_check_tools(lines: Sequence[str]) -> CheckTools:
    """The first executable of each `&&`/`;` segment of each line, once each. A line with a segment that cannot be
    read, or with nothing but empty segments, is unparsed and contributes no tool."""
    segments = [[s for s in _SEGMENTS.split(line) if s.strip()] for line in lines]
    tools = [[_segment_tool(s) for s in line_segments] for line_segments in segments]
    bad = [not line_tools or None in line_tools for line_tools in tools]
    found = (t for line_tools, b in zip(tools, bad, strict=True) if not b for parsed in line_tools for t in parsed or ())
    return CheckTools(
        tuple(dict.fromkeys(found)),
        tuple(line for line, b in zip(lines, bad, strict=True) if b),
    )


def lane_argv(tools: Sequence[str]) -> list[str]:
    """The command that prints each of `tools` the lane's login shell cannot find, one per line. Run it on a remote
    host through `ssh_argv`; for this machine run `lookup_argv` directly."""
    return login_shell_argv(lookup_argv(tools))


def lookup_argv(tools: Sequence[str]) -> list[str]:
    return ["sh", "-c", _LOOKUP, "lane-toolchain", *tools]


def refuse_message(host: str, tool: str) -> str:
    return f"{host}: {tool} not on the lane PATH"


def refusals(host: str, tools: Sequence[str], argv: list[str], probe: Probe) -> list[str]:
    """One refusal line per tool missing on `host`, in `tools` order; [] when none is missing or there is nothing to check.

    A login shell may print noise, so only an output line equal to a tool name counts. A probe that exits non-zero
    never read as found: it is one refusal naming the exit code."""
    if not tools:
        return []
    code, output = probe(argv)
    if code != 0:
        return [f"{host}: toolchain probe exited {code}"]
    missing = {line.strip() for line in output.splitlines()}
    return [refuse_message(host, tool) for tool in tools if tool in missing]
