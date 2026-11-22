"""The lander runs a new-model phase merge's level gate in the lane's environment. Real `cox route import`,
`cox lander adopt --apply` and `cox lander once` against a real clone of a real bare origin and the store, with the
local forge.

E1, 2026-11-21: the lander's merge of a phase ran the tree's `# level: phase` line, a bare `pytest`, in its scratch
tree with its own PATH, where `pytest` was a standalone tool's with no `yaml`: the merged tree's conftest, which puts
the tree's `src/` first on `sys.path` and imports its packages, failed to load. The lane passes the same line, because
the repository's `.venv` is linked into its tree and its `bin` is first on PATH.

The fixture repository is built the same way: `gate_tests/conftest.py` puts `src/` first on `sys.path` and imports
`demo`, whose `__init__` imports `fixture_dep`, a module only the repository's own `.venv` has (as `yaml` is only the
coxswain venv's). Its `.agent-checks` phase line is a bare `pytest`, which that `.venv/bin` holds. Two phases edit two
files, built on another clone as the phase lane builds them: p2's merge joins it with p1, work it has not seen, so the
lander runs the gate on the merged tree, and it passes. A `pytest` that is not the repository's is first on the
lander's own PATH, as E1's was: run under that one, the conftest does not load.
"""

from __future__ import annotations

import sys
import sysconfig
from pathlib import Path

from smoke import plan
from smoke.test_lander_lanes import _gate, _git, _origin_git, _record, _repository

AGENT_CHECKS = "true\n# level: phase\npytest -q -p no:cacheprovider gate_tests\n"
CONFTEST = ('import sys\nfrom pathlib import Path\n\nsys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src")]\n'
            "from demo import VALUE  # noqa: E402\n\n\ndef pytest_report_header(config):\n"
            '    return f"demo {VALUE}"\n')
TEST = "from demo import VALUE\n\n\ndef test_the_tree_imports_its_own_package():\n    assert VALUE == 7\n"


def _venv(repo: Path) -> None:
    """`repo/.venv`, a real venv with `fixture_dep` in its own site-packages, and `pytest` in its `bin`: this runner's
    pytest, reached through a `.pth` to this runner's site-packages, as an installed one would be."""
    import venv

    venv.EnvBuilder(with_pip=False).create(repo / ".venv")
    python = repo / ".venv" / "bin" / "python"
    site = next((repo / ".venv" / "lib").glob("python*/site-packages"))
    (site / "fixture_dep.py").write_text("VALUE = 7\n", encoding="utf-8")
    (site / "runner.pth").write_text(sysconfig.get_paths()["purelib"] + "\n", encoding="utf-8")
    script = repo / ".venv" / "bin" / "pytest"
    script.write_text(f"#!{python}\nimport sys\nfrom pytest import console_main\nsys.exit(console_main())\n",
                      encoding="utf-8")
    script.chmod(0o755)


def _standalone_pytest(sandbox, tmp_path: Path) -> None:
    """A `pytest` first on the lander's PATH that is not the repository's: this runner's, which has no `fixture_dep`,
    as E1's lander found a standalone tool's `~/.local/bin/pytest`, which had no `yaml`."""
    tools = tmp_path / "tools-bin"
    tools.mkdir()
    (tools / "pytest").write_text(f'#!/bin/sh\nexec "{sys.executable}" -m pytest "$@"\n', encoding="utf-8")
    (tools / "pytest").chmod(0o755)
    sandbox.env["PATH"] = f"{tools}:{sandbox.env['PATH']}"


def _repo(sandbox, tmp_path: Path) -> tuple[Path, Path]:
    repo, origin = _repository(sandbox, tmp_path, "importer", "exit 0\n")
    files = {".agent-checks": AGENT_CHECKS, ".gitignore": ".venv/\n", "src/demo/__init__.py": "from fixture_dep import VALUE\n",
             "gate_tests/conftest.py": CONFTEST, "gate_tests/test_import.py": TEST}
    for relative, text in files.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(text, encoding="utf-8")
    _git(sandbox, repo, "add", "--all")
    _git(sandbox, repo, "commit", "--quiet", "-m", "a package, its conftest and the phase gate")
    _git(sandbox, repo, "push", "--quiet", "origin", "main")
    _venv(repo)
    return repo, origin


def _files(sandbox, tips: dict[str, str]) -> None:
    own = {"title": "importer", "repo": "~/src/importer", "draft": "false", "lane_model": "phase"}
    sandbox.write("work/importer/initiative.md", plan.work_item_text(own, "An initiative."))
    for phase in ("p1", "p2"):
        tip = tips.get(phase, "")
        fields = {"id": f"importer-{phase}", "title": f"Add a file in {phase}", "phase": phase,
                  "state": "approved" if tip else "ready", **({"merged_commit": tip} if tip else {})}
        sandbox.write(f"work/importer/{phase}/importer-{phase}.md", plan.work_item_text(fields, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr


def _built_on_a_worker(sandbox, tmp_path: Path, origin: Path) -> dict[str, str]:
    worker = tmp_path / "worker-importer"
    _git(sandbox, tmp_path, "clone", "--quiet", str(origin), str(worker))
    tips: dict[str, str] = {}
    for phase in ("p1", "p2"):
        _git(sandbox, worker, "checkout", "--quiet", "-B", f"epic/importer/{phase}", "origin/main")
        (worker / f"{phase}.txt").write_text(f"{phase}\n", encoding="utf-8")
        _git(sandbox, worker, "add", f"{phase}.txt")
        _git(sandbox, worker, "commit", "--quiet", "-m", f"{phase}: a file of its own")
        tips[phase] = _git(sandbox, worker, "rev-parse", "HEAD")
        _git(sandbox, worker, "push", "--quiet", "origin", f"epic/importer/{phase}")
    return tips


def test_a_new_model_phase_merge_runs_the_trees_phase_gate_under_the_repos_venv(sandbox, tmp_path):
    _gate(sandbox, "ticket")
    _standalone_pytest(sandbox, tmp_path)
    _, origin = _repo(sandbox, tmp_path)
    _files(sandbox, {})
    adopted = sandbox.cox("lander", "adopt", "--apply", "importer")
    assert adopted.returncode == 0 and "importer: record made" in adopted.stdout, adopted.stdout + adopted.stderr
    assert _record(sandbox, "importer")["lane_model"] == "phase"
    tips = _built_on_a_worker(sandbox, tmp_path, origin)
    _files(sandbox, tips)

    done = sandbox.cox("lander", "once")

    out = done.stdout + done.stderr
    record = _record(sandbox, "importer")
    assert not record.get("held"), f"{record.get('held')}\n{out}"
    assert "phase p1 of importer merged into init/importer" in out, out
    assert "phase p2 of importer merged into init/importer" in out, out  # through the gate: p2 had not seen p1
    tip = _origin_git(sandbox, origin, "rev-parse", "--verify", "--quiet", "refs/heads/init/importer").stdout.strip()
    assert tip and all(_origin_git(sandbox, origin, "merge-base", "--is-ancestor", sha, tip).returncode == 0
                       for sha in tips.values()), "both phases on origin's initiative branch"
