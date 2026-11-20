"""A lane check in one checkout imports only that checkout's code, with a second checkout first on the path (H1).

A lane's suites once ran the store as `python -m harness.store_cli` children of the shared venv, whose editable install
pointed at another checkout, so those children imported that checkout's `harness` and refused a store this tree had
migrated (#1171, #1173). The store is now called in process. Here a second checkout's `agent_tools`, `harness` and `core`
stand first on PYTHONPATH (ahead of anything an editable install's .pth adds), each raising if imported, and the lane
check's own command runs a probe from this checkout's `src/tools`: every module the probe loads, and the store module
its lease goes through, comes from this checkout, and the lease is taken in a real store.
"""

import os
import subprocess
import sys
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[3]

PROBE = '''import os
import sys
from pathlib import Path

import agent_tools
import core
import harness
from agent_tools import store_cli
from harness import store_api

CHECKOUT, OTHER = Path(os.environ["LANE_CHECKOUT"]), Path(os.environ["OTHER_CHECKOUT"])


def test_the_lane_runs_its_own_checkout(tmp_path):
    for module in (agent_tools, core, harness, store_api, store_cli):
        assert Path(module.__file__).resolve().is_relative_to(CHECKOUT), module.__file__
    granted = store_cli.lease_acquire(tmp_path, "lane", "probe", 60)
    assert isinstance(granted, store_api.LeaseGranted), granted
    loaded = [m.__file__ for m in list(sys.modules.values()) if getattr(m, "__file__", None)]
    assert not [f for f in loaded if Path(f).resolve().is_relative_to(OTHER)]
'''


def _other_checkout(root: Path) -> Path:
    """A second checkout whose packages refuse to load, so importing any of them fails the probe outright."""
    for package in ("agent_tools", "harness", "core"):
        (root / package).mkdir(parents=True)
        (root / package / "__init__.py").write_text(f'raise ImportError("imported {package} from the other checkout")\n')
    return root


def test_a_lane_check_imports_only_its_worktrees_code(tmp_path):
    other = _other_checkout(tmp_path / "other")
    probe = tmp_path / "probe" / "test_probe.py"
    probe.parent.mkdir()
    probe.write_text(PROBE)
    env = {
        **os.environ,
        "PYTHONPATH": str(other),
        "LANE_CHECKOUT": str(CHECKOUT),
        "OTHER_CHECKOUT": str(other),
        "COXSWAIN_STORE_DOUBLE": "sqlite",
        "AGENT_TOOLS_PROFILE": str(tmp_path / "no-profile.yaml"),
    }
    # The lane check's own line (`cd src/tools && pytest -q`), with this checkout's pytest.ini named, as the lane's is.
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "pytest.ini", str(probe)],
        cwd=CHECKOUT / "src" / "tools", env=env, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert "1 passed" in done.stdout
