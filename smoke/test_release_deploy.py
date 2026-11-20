"""`cox release deploy` refuses cleanly instead of raising ImportError.

The command shipped wired to `agent_tools.release_deploy_edge`, a module that never landed, and its unit tests stubbed
that module into `sys.modules`, so the installed `cox` raised ImportError on every run. This runs the real installed
`cox`, as an operator would. Until the edge exists it must exit 2 with a line that says what to run instead.
"""


def test_release_deploy_refuses_with_a_message_not_a_traceback(sandbox):
    done = sandbox.cox("release", "deploy", "2026.11.6", timeout=120)
    assert "Traceback" not in done.stderr and "ImportError" not in done.stderr, done.stderr
    assert done.returncode == 2, done.stdout + done.stderr
    assert "cox release deploy: not available" in done.stderr
    assert "run `cox upgrade` on each host" in done.stderr
