"""The pyarrow pieces of the Homebrew formula, as text in and text out.

Pure: no I/O, and no import of devtools.release, which imports this module in a later phase.
pyarrow needs native code, so the formula builds its sdist against Homebrew's apache-arrow rather than carrying a wheel.
"""

import re

_PYARROW_PIN_RE = re.compile(r"^pyarrow==([^\s;]+)", re.MULTILINE)
_INSTALL_RE = re.compile(r"^  def install\b")
_VENV_RE = re.compile(r"^(\s*)virtualenv_install_with_resources\b")
_TEST_RE = re.compile(r"^  test do\s*$")
_DEPENDS_RE = re.compile(r"^  depends_on\b")
_RESOURCE_RE = re.compile(r"^  resource\b")
_PYARROW_RESOURCE_RE = re.compile(r'^\s*resource "pyarrow" do', re.MULTILINE)

# (line to add, marker whose presence means the dependency is already declared)
_DEPENDS = (
    ('depends_on "apache-arrow"', 'depends_on "apache-arrow"'),
    ('depends_on "cmake" => :build', 'depends_on "cmake"'),
)

# ARROW_HOME and PYARROW_CMAKE_OPTIONS are the variables pyarrow's source build documents for an external Arrow.
_INSTALL_ENV = (
    "# pyarrow builds from its sdist against Homebrew's apache-arrow; a wheel would bundle libraries Homebrew cannot relink.",
    'ENV["ARROW_HOME"] = Formula["apache-arrow"].opt_prefix',
    'ENV["PYARROW_CMAKE_OPTIONS"] = "-DCMAKE_PREFIX_PATH=#{Formula["apache-arrow"].opt_prefix}"',
)

# The object_store row is built only from a shared store profile, and a local traces directory returns before the client
# is imported. So the profile names an s3:// traces URL that nothing answers.
_DOCTOR_TEST = (
    "# A bucket cannot be reached inside the Homebrew test sandbox, so this asserts that the object store client is",
    "# importable (pyarrow), not that the bucket is reachable.",
    r'(testpath/"provider.yaml").write("storage_url: postgresql://doctor@127.0.0.1:1/doctor\n'
    r'traces_url: s3://cox-doctor-probe/traces\nobject_store:\n  endpoint: https://127.0.0.1:9\n")',
    r'(testpath/"profile.yaml").write("provider_profile: #{testpath}/provider.yaml\nstore:\n  mode: shared\n")',
    'doctor = shell_output("#{bin}/cox setup doctor --profile #{testpath}/profile.yaml 2>&1", nil)',
    'assert_match "object_store", doctor',
    'refute_match "needs a client library that is not installed", doctor',
)


def pyarrow_pin(requirements_text: str) -> str:
    """The pinned pyarrow version in the text of extras-requirements.txt; ValueError names a missing pin."""
    found = _PYARROW_PIN_RE.search(requirements_text)
    if found is None:
        raise ValueError("extras requirements pin no pyarrow")
    return found.group(1)


def pyarrow_resource(url: str, sha256: str) -> str:
    """The top-level `resource "pyarrow"` stanza for the sdist at `url`, 2-space indented, newline-terminated."""
    return f'  resource "pyarrow" do\n    url "{url}"\n    sha256 "{sha256}"\n  end\n'


def _declared(text: str, marker: str) -> bool:
    return re.search(rf"^\s*{re.escape(marker)}", text, re.MULTILINE) is not None


def _first(lines: list[str], pattern: re.Pattern[str], start: int = 0) -> int | None:
    return next((i for i in range(start, len(lines)) if pattern.match(lines[i])), None)


def with_arrow_block(formula_text: str, resource_stanza: str) -> str:
    """`formula_text` with the apache-arrow and cmake dependencies, the pyarrow resource and the install env, each added once.

    ValueError when the formula has no `def install` or no `virtualenv_install_with_resources` line after it."""
    lines = formula_text.splitlines(keepends=True)
    install = _first(lines, _INSTALL_RE)
    if install is None:
        raise ValueError("formula has no `def install` to point the pyarrow build at apache-arrow in")
    venv = _first(lines, _VENV_RE, install + 1)
    if venv is None:
        raise ValueError("formula has no `virtualenv_install_with_resources` line to build pyarrow before")
    # Edits run from the bottom up so each earlier index stays valid.
    if not _declared(formula_text, 'ENV["ARROW_HOME"]'):
        indent = _VENV_RE.match(lines[venv]).group(1)
        lines[venv:venv] = [f"{indent}{line}\n" for line in _INSTALL_ENV]
    if _PYARROW_RESOURCE_RE.search(formula_text) is None:
        lines[install:install] = [resource_stanza if resource_stanza.endswith("\n") else resource_stanza + "\n", "\n"]
    missing = [line for line, marker in _DEPENDS if not _declared(formula_text, marker)]
    if missing:
        depends = [i for i, line in enumerate(lines) if _DEPENDS_RE.match(line)]
        if depends:
            lines[depends[-1] + 1:depends[-1] + 1] = [f"  {line}\n" for line in missing]
        else:
            at = _first(lines, _RESOURCE_RE)
            at = _first(lines, _INSTALL_RE) if at is None else at
            lines[at:at] = [*(f"  {line}\n" for line in missing), "\n"]
    return "".join(lines)


def with_doctor_test(formula_text: str) -> str:
    """`formula_text` with a doctor run and an assertion on the object_store row in its `test do` block.

    A line already in the block is left alone. ValueError when the formula has no `test do` block."""
    lines = formula_text.splitlines(keepends=True)
    start = _first(lines, _TEST_RE)
    end = None if start is None else next((i for i in range(start + 1, len(lines)) if lines[i].rstrip("\n") == "  end"), None)
    if start is None or end is None:
        raise ValueError("formula has no `test do` block to assert the object_store row in")
    present = {line.strip() for line in lines[start + 1:end]}
    lines[end:end] = [f"    {line}\n" for line in _DOCTOR_TEST if line not in present]
    return "".join(lines)
