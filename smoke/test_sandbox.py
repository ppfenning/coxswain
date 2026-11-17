"""The harness proves itself: `cox` is this tree's code, installed, and it reads only the sandbox."""

import json
import subprocess

PROBE = "import json, agent_tools, harness, core; print(json.dumps([m.__file__ for m in (agent_tools, harness, core)]))"


def test_the_interpreter_cox_starts_for_the_harness_imports_the_install(sandbox):
    # #1184/#1185: child interpreters imported a venv's editable checkout of another tree. The install fixture already
    # refused any installed file that differs from this tree; this checks that the python the profile's harness_dir
    # resolves to imports agent_tools, harness and core from that install and from nowhere else.
    done = subprocess.run([str(sandbox.install.python), "-c", PROBE], env=sandbox.env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert all(path.startswith(str(sandbox.install.site)) for path in json.loads(done.stdout))
    assert "PYTHONPATH" not in sandbox.env
    assert sandbox.env["PATH"].split(":")[0] == str(sandbox.install.bin)


def test_cox_reads_the_sandbox_profile_and_nothing_else(sandbox):
    done = sandbox.cox("route", "context", "--json")
    assert done.returncode == 0, done.stderr
    context = json.loads(done.stdout)
    assert context["team"] == "smoke"
    assert context["workspace_dir"] == str(sandbox.workspace)
    assert context["harness_dir"] == str(sandbox.install.root)
    assert context["provider_profile"].startswith(str(sandbox.home))
