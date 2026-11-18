"""The one definition of the environment every Coxswain unit reads. Pure text; the edge reads and writes the file."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
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


_UNIT_ENV_FILE = re.compile(r"^EnvironmentFile=-?(\S+)\s*$", re.MULTILINE)
_PLIST_ENV_FILE = re.compile(rf"<string>{SCRIPT_NAME}</string>\s*<string>([^<]*)</string>")


def unit_environment_file(unit_text: str) -> str | None:
    """The env file a unit reads: a systemd unit's `EnvironmentFile=` path, or the file a launchd agent's
    ProgramArguments hand `launchd_arguments`' script. None when the unit names none."""
    found = _UNIT_ENV_FILE.search(unit_text) or _PLIST_ENV_FILE.search(unit_text)
    return unescape(found.group(1)) if found else None


def units_environment_file(unit_texts: Iterable[str | None], default: str) -> str:
    """The env file a run outside any unit reads: the first unit text (None for a unit not installed) that names one,
    so a hand run sees what that unit sees; else `default`, the file `cox setup unit-env` writes."""
    named = (unit_environment_file(text) for text in unit_texts if text is not None)
    return next((path for path in named if path is not None), default)


def missing_values(process_env: Mapping[str, str], file_values: Mapping[str, str]) -> dict[str, str]:
    """The file's variables the process does not already have. The process environment wins: a name it sets, even to
    the empty string, is never taken from the file."""
    return {name: value for name, value in file_values.items() if name not in process_env}


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
