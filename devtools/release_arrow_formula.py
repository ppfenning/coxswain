"""The pyarrow pieces of the Homebrew formula, as text in and text out.

Pure: no I/O, and no import of devtools.release, which imports this module.
pyarrow needs native code, and Homebrew builds its build dependencies, numpy among them, from source. That build fails,
so the formula ships without pyarrow and its caveats say how to get Parquet traces.
"""

import re

_PYARROW_PIN_RE = re.compile(r"^pyarrow==([^\s;]+)", re.MULTILINE)
_TEST_RE = re.compile(r"^  test do\s*$")
_CAVEATS_RE = re.compile(r"^  def caveats\b")
_RESOURCE_OPEN_RE = re.compile(r'^(\s*)resource "pyarrow" do\s*$')
_ARROW_LINE_RE = re.compile(
    r'^\s*(depends_on\s+"(apache-arrow|cmake)"'
    r'|ENV\["(ARROW_HOME|PYARROW_CMAKE_OPTIONS)"\]'
    r"|# pyarrow builds from its sdist\b)")
_HEREDOC_RE = re.compile(r"<<[~-]?(['\"]?)(\w+)\1")

CAVEATS_LINE = "Traces are kept as JSON. Install the traces extra with pip for Parquet."

# The object_store row is built only from a shared store profile; these lines were the formula's doctor test.
_DOCTOR_TEST = frozenset((
    "# A bucket cannot be reached inside the Homebrew test sandbox, so this asserts that the object store client is",
    "# importable (pyarrow), not that the bucket is reachable.",
    r'(testpath/"provider.yaml").write("storage_url: postgresql://doctor@127.0.0.1:1/doctor\n'
    r'traces_url: s3://cox-doctor-probe/traces\nobject_store:\n  endpoint: https://127.0.0.1:9\n")',
    r'(testpath/"profile.yaml").write("provider_profile: #{testpath}/provider.yaml\nstore:\n  mode: shared\n")',
    'doctor = shell_output("#{bin}/cox setup doctor --profile #{testpath}/profile.yaml 2>&1", nil)',
    'assert_match "object_store", doctor',
    'refute_match "needs a client library that is not installed", doctor',
))


def pyarrow_pin(requirements_text: str) -> str:
    """The pinned pyarrow version in the text of extras-requirements.txt; ValueError names a missing pin."""
    found = _PYARROW_PIN_RE.search(requirements_text)
    if found is None:
        raise ValueError("extras requirements pin no pyarrow")
    return found.group(1)


def _first(lines: list[str], pattern: re.Pattern[str], start: int = 0) -> int | None:
    return next((i for i in range(start, len(lines)) if pattern.match(lines[i])), None)


def _closing(lines: list[str], start: int, indent: str) -> int | None:
    return next((i for i in range(start + 1, len(lines)) if lines[i].rstrip() == f"{indent}end"), None)


def _resource_indices(lines: list[str]) -> set[int]:
    """Indices of every `resource "pyarrow"` stanza; ValueError when one has no closing `end`."""
    found: set[int] = set()
    for i, line in enumerate(lines):
        opened = _RESOURCE_OPEN_RE.match(line)
        if opened is None:
            continue
        end = _closing(lines, i, opened.group(1))
        if end is None:
            raise ValueError("the pyarrow resource has no closing `end`")
        found.update(range(i, end + 1))
    return found


def _doctor_indices(lines: list[str]) -> set[int]:
    start = _first(lines, _TEST_RE)
    end = None if start is None else _closing(lines, start, "  ")
    if start is None or end is None:
        return set()
    return {i for i in range(start + 1, end) if lines[i].strip() in _DOCTOR_TEST}


def _without(lines: list[str], drop: set[int]) -> list[str]:
    """`lines` minus `drop`; a blank line left next to a blank line by a removal goes too."""
    kept: list[str] = []
    for i, line in enumerate(lines):
        if i in drop:
            continue
        if (i - 1) in drop and not line.strip() and kept and not kept[-1].strip():
            continue
        kept.append(line)
    return kept


def _with_caveats_line(lines: list[str]) -> list[str]:
    """`lines` with CAVEATS_LINE in a `def caveats` block: added to the existing one, or a new one before `test do`."""
    existing = _first(lines, _CAVEATS_RE)
    if existing is None:
        at = _first(lines, _TEST_RE)
        if at is None:
            raise ValueError("formula has no `test do` to place `def caveats` before")
        block = ["  def caveats\n", "    <<~EOS\n", f"      {CAVEATS_LINE}\n", "    EOS\n", "  end\n", "\n"]
        return [*lines[:at], *block, *lines[at:]]
    end = _closing(lines, existing, "  ")
    opener = None if end is None else next(
        (i for i in range(existing + 1, end) if _HEREDOC_RE.search(lines[i])), None)
    if end is None or opener is None:
        raise ValueError("formula has a `def caveats` without a heredoc to add the traces line to")
    tag = _HEREDOC_RE.search(lines[opener]).group(2)
    stop = next((i for i in range(opener + 1, end) if lines[i].strip() == tag), None)
    if stop is None:
        raise ValueError("the `def caveats` heredoc has no terminator")
    sample = lines[opener + 1] if opener + 1 < stop else lines[stop]
    indent = re.match(r"\s*", sample).group(0) + ("" if opener + 1 < stop else "  ")
    return [*lines[:stop], f"{indent}{CAVEATS_LINE}\n", *lines[stop:]]


def without_pyarrow(formula_text: str) -> str:
    """`formula_text` with no pyarrow build and a `def caveats` that says traces are kept as JSON.

    Removes a `resource "pyarrow"` stanza, the apache-arrow and cmake dependencies, the ARROW_HOME and
    PYARROW_CMAKE_OPTIONS env lines with their comment, and the object store doctor lines in `test do`.
    Anything already absent is left alone and the caveats line is added once, so a second pass changes nothing.
    ValueError when there is no `test do` to place a new `def caveats` before; no text is returned half-written."""
    lines = formula_text.splitlines(keepends=True)
    drop = _resource_indices(lines) | _doctor_indices(lines) | {i for i, line in enumerate(lines) if _ARROW_LINE_RE.match(line)}
    kept = _without(lines, drop)
    return "".join(kept if CAVEATS_LINE in "".join(kept) else _with_caveats_line(kept))
