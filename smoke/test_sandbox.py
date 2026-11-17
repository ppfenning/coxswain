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


def _paths(value):
    """Every string in a JSON value that is an absolute path."""
    if isinstance(value, dict):
        return [p for v in value.values() for p in _paths(v)]
    if isinstance(value, list):
        return [p for v in value for p in _paths(v)]
    return [value] if isinstance(value, str) and value.startswith("/") else []


def test_cox_resolves_its_profile_from_the_sandbox_home(sandbox):
    # This shows cox resolves the profile the sandbox HOME names, and that every path it reports is the sandbox's or
    # the install's. It does not prove cox opens no other file; the environment (no real HOME, XDG root or
    # AGENT_TOOLS_PROFILE reaches it, see plan.sandbox_env) is what keeps the real home out of its reach.
    done = sandbox.cox("route", "context", "--json")
    assert done.returncode == 0, done.stderr
    context = json.loads(done.stdout)
    assert context["team"] == "smoke"
    assert context["workspace_dir"] == str(sandbox.workspace)
    assert context["harness_dir"] == str(sandbox.install.root)
    assert context["provider_profile"] == str(sandbox.home / ".config" / "agent-tools" / "provider.yaml")
    roots = (str(sandbox.home.parent), str(sandbox.install.root))
    assert [p for p in _paths(context) if not p.startswith(roots)] == []
