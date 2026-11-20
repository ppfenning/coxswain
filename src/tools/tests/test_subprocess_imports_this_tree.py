"""A test that runs `sys.executable -m ...` reaches the code under test, not whatever an editable install points at.

The store double and the land's leases run as a child interpreter (`_harness_python` is `sys.executable`). On a host
whose venv carries an editable install of another checkout, that child imported the other checkout's `harness`:
a store migrated in-process to this tree's newest schema was then refused as newer than the newest known migration."""

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
