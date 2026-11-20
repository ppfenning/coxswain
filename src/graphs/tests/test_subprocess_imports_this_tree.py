"""A test that runs `sys.executable -m ...` or `-c ...` reaches the code under test, not whatever an editable install points at.

The store-traces dump, the pool edge's acquire and the import-isolation checks run as a child interpreter. On a lane
whose venv carries an editable install of another checkout, that child imported the other checkout's `core`: a
`harness` from this tree then failed to import a name only this tree's `core.landing` defines."""

import subprocess as sp
import sys
from pathlib import Path

import agent_tools
import core
import harness


def test_a_child_interpreter_imports_the_packages_this_test_imported():
    script = "import agent_tools, core, harness; print(agent_tools.__file__); print(core.__file__); print(harness.__file__)"
    done = sp.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd="/")
    seen = [Path(line).resolve() for line in done.stdout.splitlines()]
    assert seen == [Path(m.__file__).resolve() for m in (agent_tools, core, harness)]
