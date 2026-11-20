"""H2's type check for a lane and for CI: pyright over pyproject.toml's [tool.pyright], or a skip that says why.

pyright reads its packages from the python running this script. When pyright or an extra it resolves (psycopg,
pyarrow, pyiceberg, duckdb) is not installed there, a lane skips with the reason and the command that fixes it,
the way the smoke suite skips without docker. `COX_TYPES_REQUIRED=1` (CI's test job sets it) turns the skip into
a failure, so the required check never passes by not running.

The whole project is checked, not only the files a patch touched: a changed signature breaks its callers, which a
check narrowed to the changed files would not read, and the whole run takes well under a minute.
"""

import importlib.util
import os
import subprocess
import sys

REQUIRED_ENV = "COX_TYPES_REQUIRED"
FIX = "uv sync --frozen --all-extras --group dev"
NEEDED = ("pyright", "psycopg", "pyarrow", "pyiceberg", "duckdb")
TIMEOUT_S = 600


def is_required(value: str | None) -> bool:
    """`COX_TYPES_REQUIRED` read as a flag: any non-empty value other than 0, false or no."""
    return bool(value) and value.strip().lower() not in {"0", "false", "no"}


def unavailable(missing: list[str], *, required: bool) -> tuple[int, str] | None:
    """Pure. None when nothing is missing; else the exit code and the line to print: 0 and a skip for a lane,
    1 and a failure when the check is required."""
    if not missing:
        return None
    verb = "failed" if required else "skipped"
    return (1 if required else 0), f"types: {verb}: {', '.join(missing)} not installed for {sys.executable}; run `{FIX}`"


def main() -> int:
    missing = [name for name in NEEDED if importlib.util.find_spec(name) is None]
    verdict = unavailable(missing, required=is_required(os.environ.get(REQUIRED_ENV)))
    if verdict is not None:
        code, line = verdict
        print(line)
        return code
    try:
        argv = [sys.executable, "-m", "pyright", "--pythonpath", sys.executable]
        return subprocess.run(argv, timeout=TIMEOUT_S, check=False).returncode
    except subprocess.TimeoutExpired:
        print(f"types: failed: pyright ran past {TIMEOUT_S} s")
        return 1


if __name__ == "__main__":
    sys.exit(main())
