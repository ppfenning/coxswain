"""The one definition of the environment every Coxswain unit reads. Pure text; the edge reads and writes the file."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from xml.sax.saxutils import unescape

from agent_tools.claude_token import TOKEN_ENV

# Only variables some module already reads, imported from the module that defines each name.
# TOKEN_ENV is read by claude_token.claude_env. The store and traces URLs are not environment variables:
# store_url.py takes `storage_url` and `traces_url` from the provider profile, so they are not keys here.
ENV_KEYS = (TOKEN_ENV,)

# systemd splits ExecStart on whitespace and expands `%`, `$` and `\`; a value outside this set reaches the chair changed.
_UNIT_SAFE = re.compile(r"[A-Za-z0-9._@+:,/=-]+")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def unsafe_values(values: dict[str, str]) -> list[str]:
    """The names whose value systemd would split or expand, in the order given."""
    return [name for name, value in values.items() if not _UNIT_SAFE.fullmatch(value)]


def export_lines(env_text: str) -> list[int]:
    """1-based numbers of `export NAME=value` lines; systemd's EnvironmentFile= does not document that prefix."""
    return [n for n, line in enumerate(env_text.splitlines(), start=1) if line.lstrip().startswith("export ")]


def render_env(values: Mapping[str, str]) -> str:
    """`NAME=value` lines, ENV_KEYS first in their order, then any other name as given. Raises ValueError for a
    name that is not a plain identifier or a value systemd would split or expand; the keys are checked too."""
    names = [name for name in ENV_KEYS if name in values] + [name for name in values if name not in ENV_KEYS]
    bad_names = [name for name in names if not _NAME.fullmatch(name)]
    if bad_names:
        raise ValueError(f"not a plain environment name: {', '.join(bad_names)}")
    unsafe = unsafe_values({name: values[name] for name in names})
    if unsafe:
        raise ValueError(f"systemd would split or expand {', '.join(unsafe)}; use only letters, digits and . _ @ + : , / = -")
    return "".join(f"{name}={values[name]}\n" for name in names)


def parse_env(text: str) -> dict[str, str]:
    """Blank lines, `#` comments and `export ` lines are skipped; each other line splits at its first `=`."""
    lines = (line.strip() for line in text.splitlines())
    pairs = (
        line.partition("=")
        for line in lines
        if line and not line.startswith("#") and not line.startswith("export ") and "=" in line
    )
    return {name.strip(): value.strip() for name, _, value in pairs}


def environment_file_line(path: str) -> str:
    """`EnvironmentFile=-` keeps a missing file from failing the unit."""
    return f"EnvironmentFile=-{path}"


# The one env file every unit reads: the chair, lander and upgrade units (and the chair watchdog), systemd by
# EnvironmentFile= and launchd by its parsed values. `cox setup unit-env` writes it.
ENV_FILE = ".config/agent-tools/garage.env"


def env_file_path(user_home: str) -> str:
    return f"{user_home.rstrip('/')}/{ENV_FILE}"


# launchd has no EnvironmentFile: an agent runs this script, which exports every NAME=value line of the one env file
# and then execs the real command. render_env's safe charset makes the lines safe to source. A missing file is skipped,
# as systemd's `EnvironmentFile=-` skips it. So the plist holds no value from the file, and a rotated token reaches the
# agent at its next launch with no reinstall.
SOURCE_SCRIPT = 'set -a; [ -r "$1" ] && . "$1"; set +a; shift; exec "$@"'
SCRIPT_NAME = "coxswain-env"  # the script's $0; the env file is the argument after it


def launchd_arguments(environment_file: str, argv: list[str]) -> list[str]:
    """The ProgramArguments of an agent that reads `environment_file` at launch, then runs `argv`."""
    return ["/bin/sh", "-c", SOURCE_SCRIPT, SCRIPT_NAME, environment_file, *argv]


# The one reader of a systemd unit's EnvironmentFile= lines (chair_service.unit_args reads through it too). systemd
# allows leading whitespace, and the `-` prefix only makes a missing file not fail the unit.
_UNIT_ENV_FILE = re.compile(r"^[ \t]*EnvironmentFile=-?(\S+)[ \t]*$", re.MULTILINE)
_PLIST_ENV_FILE = re.compile(rf"<string>{SCRIPT_NAME}</string>\s*<string>([^<]*)</string>")
_SPECIFIER = re.compile(r"%[h%]")


def unit_environment_file(unit_text: str) -> str | None:
    """The first env file a unit names, as written: a systemd `EnvironmentFile=` path (specifiers unexpanded), or the
    file a launchd agent's ProgramArguments hand `launchd_arguments`' script. None when the unit names none."""
    found = _UNIT_ENV_FILE.search(unit_text) or _PLIST_ENV_FILE.search(unit_text)
    return unescape(found.group(1)) if found else None


def unit_environment_files(unit_text: str, home: str) -> list[str]:
    """Every env file a unit reads, in the order it reads them (a later file's value wins, as in systemd): each
    `EnvironmentFile=` line with `%h` as `home` and `%%` as `%`, else the file a launchd agent sources."""
    named = [m.group(1) for m in _UNIT_ENV_FILE.finditer(unit_text)]
    if named:
        return [_SPECIFIER.sub(lambda m: home if m.group(0) == "%h" else "%", path) for path in named]
    found = _PLIST_ENV_FILE.search(unit_text)
    return [unescape(found.group(1))] if found else []


def units_environment_files(unit_texts: Iterable[str | None], default: str, home: str) -> list[str]:
    """The env files a run outside any unit reads: those of the first unit text (None for a unit not installed or not
    readable) that names any, so a hand run sees what that unit sees; else `[default]`, the file `cox setup unit-env`
    writes."""
    named = (unit_environment_files(text, home) for text in unit_texts if text is not None)
    return next((files for files in named if files), [default])


def decode_text(data: bytes | None) -> str | None:
    """A file's bytes as UTF-8 text; None for no file or for bytes that are not UTF-8, which are skipped as missing."""
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def undecodable(reads: Iterable[tuple[str, bytes | None]]) -> list[str]:
    """The paths, of `(path, bytes or None)` reads, that exist but are not UTF-8 text."""
    return [path for path, data in reads if data is not None and decode_text(data) is None]


def read_systemd_env(text: str) -> dict[str, str]:
    """`text` as systemd's `EnvironmentFile=` reads it (its env-file parser, measured against a transient user unit):
    a line starting `#` or `;` is a comment; the name is trimmed and a name that is not a plain identifier (an `export`
    line, a pasted secret with `+` or `/`) is ignored. The value skips leading whitespace; unquoted, a backslash keeps
    the next character, a backslash at the end of a line joins the next line, interior whitespace is kept, trailing
    whitespace dropped, and a quote that does not open the value stays literal (`x"y"` is `x"y"`). Single quotes keep
    everything; double quotes drop a backslash only before `"`, `\\`, `$` or a backtick (`"a\\nb"` keeps it), and a
    closing quote returns to the value's start, so `"x"y'z'` is `xy'z'` and `'a''b'` is `ab`. A quote left open runs
    to the end of the file. `$` and `%` stay literal."""
    out: dict[str, str] = {}
    state, key, value, trailing = "pre_key", "", "", 0

    def emit() -> None:
        name = key.strip()
        if _NAME.fullmatch(name):
            out[name] = value[: len(value) - trailing] if trailing else value

    for char in text:
        if state == "pre_key":
            if char in "#;":
                state = "comment"
            elif not char.isspace():
                state, key = "key", char
        elif state == "key":
            if char == "\n":
                state = "pre_key"
            elif char == "=":
                state, value, trailing = "pre_value", "", 0
            else:
                key += char
        elif state == "pre_value":
            if char == "\n":
                emit()
                state = "pre_key"
            elif char == "'":
                state = "single"
            elif char == '"':
                state = "double"
            elif char == "\\":
                state = "escape"
            elif not char.isspace():
                state, value, trailing = "value", value + char, 0
        elif state == "value":
            if char == "\n":
                emit()
                state = "pre_key"
            elif char == "\\":
                state, trailing = "escape", 0
            else:
                value += char
                trailing = trailing + 1 if char.isspace() else 0
        elif state == "escape":
            state = "value"
            if char != "\n":
                value, trailing = value + char, 0
        elif state == "single":
            if char == "'":
                state = "pre_value"
            else:
                value += char
        elif state == "double":
            if char == '"':
                state = "pre_value"
            elif char == "\\":
                state = "double_escape"
            else:
                value += char
        elif state == "double_escape":
            state = "double"
            if char in _DQ_ESCAPABLE:
                value += char
            elif char != "\n":
                value += "\\" + char
        elif char == "\n":  # comment: a newline ends it, even after a backslash
            state = "pre_key"
    if state not in ("pre_key", "key", "comment"):
        emit()
    return out


_DQ_ESCAPABLE = frozenset('"\\$`')  # what a backslash escapes inside double quotes, in systemd and in sh alike
_SH_META = frozenset(";&|<>()~")
SH_SPLITS = "launchd's sh would read it differently (an open quote, a line that continues, or text after a space)"
SH_EXPANDS = "launchd's sh would expand its $ or backtick; put the value in single quotes or drop them"
SH_SYNTAX = "launchd's sh would read its ; & | < > ( ) or ~ as syntax; put the value in single quotes"
SH_LEADING = "it starts with whitespace, so launchd's sh would set it empty and run the rest"


def sh_word(raw: str) -> tuple[str | None, str | None]:
    """`(value, None)` for `raw`, the text after `NAME=` on a line, as `sh .` assigns it, else `(None, why)`: one
    word of literal text, single-quoted text, and double-quoted text in which a backslash drops only before `"`, `\\`,
    `$` or a backtick. Anything sh would expand, read as syntax, split or carry onto the next line is skipped, not
    guessed at."""
    if raw[:1].isspace():
        return None, SH_LEADING
    out: list[str] = []
    quote: str | None = None
    chars = iter(raw)
    for char in chars:
        if quote == "'":
            if char == "'":
                quote = None
            else:
                out.append(char)
        elif quote == '"':
            if char == '"':
                quote = None
            elif char in "$`":
                return None, SH_EXPANDS
            elif char == "\\":
                escaped = next(chars, None)
                if escaped is None:
                    return None, SH_SPLITS
                out.append(escaped if escaped in _DQ_ESCAPABLE else "\\" + escaped)
            else:
                out.append(char)
        elif char == "\\":
            escaped = next(chars, None)
            if escaped is None:
                return None, SH_SPLITS
            out.append(escaped)
        elif char in "'\"":
            quote = char
        elif char in "$`":
            return None, SH_EXPANDS
        elif char in _SH_META:
            return None, SH_SYNTAX
        elif char.isspace():
            return (None, SH_SPLITS) if any(not c.isspace() for c in chars) else ("".join(out), None)
        else:
            out.append(char)
    return (None, SH_SPLITS) if quote is not None else ("".join(out), None)


def read_sh_env(text: str) -> tuple[dict[str, str], tuple[tuple[str, str], ...]]:
    """`text` as launchd's agent sources it with `sh .` under `set -a`: each `NAME=word` line (an `export ` prefix
    allowed), the word read by `sh_word`. Returns the values, and each name skipped with why. Blank lines, comments
    and lines that are not a plain assignment set nothing."""
    values: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.lstrip()
        stripped = stripped[len("export "):].lstrip() if stripped.startswith("export ") else stripped
        name, sep, raw = stripped.partition("=")
        if not sep or not _NAME.fullmatch(name):
            continue
        value, why = sh_word(raw)
        if why is None and value is not None:
            values[name] = value
            skipped.pop(name, None)
        else:
            values.pop(name, None)
            skipped[name] = why or SH_SPLITS
    return values, tuple(skipped.items())


def read_env_files(texts: Iterable[str | None], platform: str) -> tuple[dict[str, str], tuple[tuple[str, str], ...]]:
    """The values the units on `platform` (a `sys.platform` value) take from these files, in order, a later file's
    value winning; and each name skipped, with why. systemd reads Linux's files (`read_systemd_env`) and skips
    nothing; launchd's `sh .` reads darwin's (`read_sh_env`). None (no file) adds nothing."""
    values: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for text in texts:
        if text is None:
            continue
        found, passed = (read_sh_env(text) if platform == "darwin" else (read_systemd_env(text), ()))
        for name in found:
            skipped.pop(name, None)
        values.update(found)
        for name, why in passed:
            values.pop(name, None)
            skipped[name] = why
    return values, tuple(skipped.items())


@dataclass(frozen=True)
class EnvLoad:
    """What a hand run takes from the units' files: `loaded` is set in the process; `overridden` names the variables
    the shell sets to a value other than the file's (the empty string included), which the shell keeps; `skipped`
    pairs each file variable not loaded with why (`SH_SPLITS`, `SH_EXPANDS`, `SH_SYNTAX`, `SH_LEADING`)."""

    loaded: dict[str, str]
    overridden: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]


def plan_load(process_env: Mapping[str, str], file_values: Mapping[str, str],
              skipped: tuple[tuple[str, str], ...] = ()) -> EnvLoad:
    """`file_values` and `skipped` as `read_env_files` read them. The process environment wins: a name it sets, even
    to the empty string, is never taken from the file, and is not reported skipped."""
    return EnvLoad(
        loaded={name: value for name, value in file_values.items() if name not in process_env},
        overridden=tuple(name for name, value in file_values.items()
                         if name in process_env and process_env[name] != value),
        skipped=tuple((name, why) for name, why in skipped if name not in process_env),
    )


# Only a name in this form is printed: a lower-case or mixed one may be a secret pasted on a line of its own (base64
# padded with `=` parses as NAME=value), so it is counted instead.
_PRINTABLE_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")


def printable_names(names: Iterable[str]) -> str:
    """The names that may be printed, joined, and a count of the others: `A, B and 2 other names`."""
    names = list(names)
    shown = [name for name in names if _PRINTABLE_NAME.fullmatch(name)]
    hidden = len(names) - len(shown)
    other = f"{hidden} {'other ' if shown else ''}name{'s' if hidden != 1 else ''}" if hidden else ""
    return " and ".join(part for part in (", ".join(shown), other) if part)


def load_notes(load: EnvLoad, files: list[str], absent: list[str], unreadable: list[str]) -> list[str]:
    """The lines a hand run prints about the units' env: paths and `printable_names` only, never a value. `absent`
    are files a unit names that do not exist; `unreadable` are unit or env files skipped as not UTF-8."""
    where = ", ".join(files)
    reasons = dict.fromkeys(why for _, why in load.skipped)
    return [
        *(f"env: skipped {path}: not UTF-8 text" for path in unreadable),
        *(f"env: {path}, which a unit names, does not exist" for path in absent),
        *([f"env: loaded {printable_names(load.loaded)} from {where}, as the units read it"] if load.loaded else []),
        *([f"env: the shell's {printable_names(load.overridden)} override {where}; unset them to use the file's"]
          if load.overridden else []),
        *(f"env: not loaded {printable_names(n for n, w in load.skipped if w == why)} from {where}: {why}"
          for why in reasons),
    ]


def file_text(existing: str | None, token: str | None) -> str:
    """The env file's new text: every NAME=value line the file already has, with the token set when one is given.
    Raises ValueError for `export` lines (parse_env would drop them, losing a setting) and for anything `render_env`
    refuses, so nothing is written that a unit would read differently."""
    exports = export_lines(existing or "")
    if exports:
        numbers = ", ".join(str(n) for n in exports)
        raise ValueError(f"export lines ({numbers}) that EnvironmentFile= may skip; rewrite them as NAME=value")
    values = parse_env(existing or "")
    return render_env({**values, TOKEN_ENV: token} if token else values)
