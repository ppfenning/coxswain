"""`cox release deploy`, run on the real installed `cox`, prints the moved pointer and exits 2, never a traceback.

The command once shipped wired to `agent_tools.release_deploy_edge`, a module that never landed, and the installed
`cox` raised ImportError on every run. It deployed nothing and is gone; `cox release` is a pointer like `cox dev`.
"""


def test_release_deploy_prints_the_pointer_not_a_traceback(sandbox):
    done = sandbox.cox("release", "deploy", "2026.11.6", timeout=120)
    assert "Traceback" not in done.stderr and "ImportError" not in done.stderr, done.stderr
    assert done.returncode == 2, done.stdout + done.stderr
    assert "run `cox upgrade` on that host" in done.stdout
