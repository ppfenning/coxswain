"""The temp directory every Coxswain service unit gives the work it runs. Pure text; the edges write and read the files.

A lane's scratch, a smoke's installs and a full suite all go under the process's temp directory, `$TMPDIR` or else
`/tmp`. On some hosts `/tmp` is RAM, or a tmpfs mounted with `usrquota` (E1, 2026-11-21: a suite failed with 85
`OSError: [Errno 122] Disk quota exceeded`). So the chair's, the queue workers' and the lander's units set TMPDIR to
an on-disk directory, `/var/tmp/coxswain-<user>` unless the profile's `lane_tmpdir` names another, and create it
before each start (`ExecStartPre=-mkdir`; the `-` lets a unit whose directory cannot be made still start, with
Python's tempfile falling back to /tmp). The doctor's `lane TMPDIR` rows judge what each unit sets (`tmpdir_check`).

A unit installed before this setting gets it from `cox upgrade` (`with_tmpdir`), the way `KillMode=process` reached
the units installed before it (`chair_service.with_kill_mode`).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from functools import reduce
from xml.sax.saxutils import escape, unescape

from agent_tools.unit_env import unsafe_values

PROFILE_KEY = "lane_tmpdir"
DEFAULT_TEMPLATE = "/var/tmp/coxswain-{user}"
SYSTEM_DEFAULT = "/tmp"  # what Python's tempfile uses when a unit sets no TMPDIR

_ENVIRONMENT = "Environment="
_NEVER = re.compile(r"(?!)")  # matches nothing: `with_tmpdir` has taken every TMPDIR assignment out already
_MKDIR_LINE = re.compile(r"^ExecStartPre=-?mkdir -p -m 0700 .*$\n?", re.MULTILINE)
_SERVICE = re.compile(r"^\[Service\][ \t]*$", re.MULTILINE)
_PROFILE_ARG = re.compile(r"^ExecStart=.*?\s--profile\s+(\S+)", re.MULTILINE)
_PLIST_TMPDIR = re.compile(r"<key>TMPDIR</key>\s*<string>([^<]*)</string>")


def tmpdir_choice(profile: Mapping[str, object], user: str) -> tuple[str, str | None]:
    """Pure. The units' TMPDIR and, when the profile's `lane_tmpdir` was refused, why. The one check of the value:
    it must be an absolute path systemd reads as written (`unit_env.unsafe_values`: no space to split it in two, no
    `%` or `$` to expand). A refused or absent value gives `/var/tmp/coxswain-<user>`."""
    default = DEFAULT_TEMPLATE.format(user=user or "operator")
    named = profile.get(PROFILE_KEY)
    if not isinstance(named, str) or not named.strip():
        return default, None
    value = named.strip().rstrip("/") or "/"
    if not value.startswith("/"):
        return default, f"lane_tmpdir {value!r} is not an absolute path; the units use {default}"
    if unsafe_values({PROFILE_KEY: value}):
        return default, (f"lane_tmpdir {value!r} has a character systemd would split or expand (use only letters, "
                         f"digits and . _ @ + : , / = -); the units use {default}")
    return value, None


def _assignments(body: str) -> list[str]:
    """The `NAME=value` assignments of one Environment= line's value, its double or single quotes taken off."""
    try:
        return shlex.split(body)
    except ValueError:
        return body.split()


def _quoted(assignment: str) -> str:
    return f'"{assignment}"' if any(c in assignment for c in " \t'\"\\") else assignment


def _without_tmpdir(line: str) -> str | None:
    """One line with its TMPDIR assignments taken out: the line as it is when it has none, None when TMPDIR was all it
    set, else the line with only the other assignments."""
    if not line.startswith(_ENVIRONMENT):
        return line
    kept = [a for a in _assignments(line[len(_ENVIRONMENT):]) if not a.startswith("TMPDIR=")]
    if len(kept) == len(_assignments(line[len(_ENVIRONMENT):])):
        return line
    return f"{_ENVIRONMENT}{' '.join(map(_quoted, kept))}" if kept else None


def tmpdir_line(tmpdir: str) -> str:
    return f"Environment=TMPDIR={tmpdir}"


def mkdir_line(tmpdir: str) -> str:
    """systemd resolves a bare `mkdir` on its fixed search path; `-` keeps a failed mkdir from failing the start."""
    return f"ExecStartPre=-mkdir -p -m 0700 {tmpdir}"


def service_lines(tmpdir: str | None) -> list[str]:
    """The [Service] lines a unit carries for `tmpdir`; none for None."""
    return [] if tmpdir is None else [tmpdir_line(tmpdir), mkdir_line(tmpdir)]


def set_service_line(text: str, pattern: re.Pattern[str], line: str) -> str:
    """Pure. `text` with `line` in its [Service] section: every line `pattern` matches is replaced by it, else it goes
    at the end of the section. Text with no [Service] section comes back unchanged."""
    if pattern.search(text):
        return pattern.sub(line, text)
    lines = text.splitlines(keepends=True)
    start = next((i for i, item in enumerate(lines) if item.strip() == "[Service]"), None)
    if start is None:
        return text
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    if lines[end - 1] and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    return "".join([*lines[:end], line + "\n", *lines[end:]])


def with_tmpdir(text: str, tmpdir: str) -> str:
    """Pure. A unit file's text with `service_lines(tmpdir)` at the end of its [Service] section, any TMPDIR or mkdir
    line it had taken out first. Run twice, the second run changes nothing; the rest of the file is left as it is, and
    text with no [Service] section comes back unchanged."""
    if not _SERVICE.search(text):
        return text
    kept = (_without_tmpdir(line) for line in _MKDIR_LINE.sub("", text).split("\n"))
    bare = "\n".join(line for line in kept if line is not None)
    return set_service_line(bare, _NEVER, "\n".join(service_lines(tmpdir)))


def _tmpdir_in(text: str | None) -> str | None:
    """The last TMPDIR one file sets: an assignment on any Environment= line (several may share one), else a plist's."""
    if text is None:
        return None
    found = [a[len("TMPDIR="):].strip() for line in text.splitlines() if line.startswith(_ENVIRONMENT)
             for a in _assignments(line[len(_ENVIRONMENT):]) if a.startswith("TMPDIR=")]
    found = found or [unescape(m.group(1)).strip() for m in _PLIST_TMPDIR.finditer(text)]
    return found[-1] if found else None


def effective_tmpdir(unit_text: str | None, drop_ins: Sequence[tuple[str, str]] = ()) -> tuple[str, str] | None:
    """The TMPDIR a unit runs with and the file that set it: the unit file first, then its drop-ins by name, the last
    one setting it winning, as systemd reads them. A launchd plist's EnvironmentVariables are read too. None when no
    file sets it: the unit's work then gets `SYSTEM_DEFAULT`."""

    def pick(found: tuple[str, str] | None, item: tuple[str, str | None]) -> tuple[str, str] | None:
        value = _tmpdir_in(item[1])
        return found if value is None else (value, item[0])

    return reduce(pick, [("unit", unit_text), *sorted(drop_ins)], None)


def profile_arg(unit_text: str) -> str | None:
    """The `--profile` a unit's ExecStart pins, which the upgrade reads `lane_tmpdir` from; None when it pins none."""
    found = _PROFILE_ARG.search(unit_text)
    return found.group(1) if found else None


def plist_lines(tmpdir: str | None) -> list[str]:
    """The TMPDIR entry of a plist's EnvironmentVariables dict; none for None."""
    return [] if tmpdir is None else ["    <key>TMPDIR</key>", f"    <string>{escape(tmpdir)}</string>"]
