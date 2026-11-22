"""F13: the old model's `remote_*` modules gain no new importer before E4 retires them.

E4 (stateless-impl-plan, item 25) removes `cox runs fetch`, `remote_fetch`, `remote_launch` and the ssh launch,
`chair_login_check`'s ssh probes and the `remote_unfetched` gates. Those files, and the `remote_*` modules
themselves, may import each other. Every other importer is listed below, with the remote modules it uses now, as a
ratchet: the list may only shrink. A new importer, or a listed one reaching for another remote module, fails.
The `cox` command modules reach these through `agent_tools.cli_lazy`'s proxies, so a name imported from there counts.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "src" / "tools" / "agent_tools"
SCANNED = ("src/tools/agent_tools", "src/graphs/harness", "src/graphs/graphs", "src/graphs/runner", "src/cartridges/core")

# The E4 retirement list: files that go with the old model, so their imports of `remote_*` are not growth.
RETIRING = frozenset({"cli_runs_fetch", "chair_login_check", "chair_read_remote_unfetched"})

# Importers outside the retirement list, with the remote modules each imports today. Only shrinks.
# remote_argv is the shared ssh argv builder: release_deploy, host_cmd and the lane prereq checks use it for things
# E4 keeps, so E4 must move it out from under `remote_*` rather than delete it.
EXISTING: dict[str, frozenset[str]] = {
    "chair_exec_edge": frozenset({"remote_argv", "remote_fetch"}),
    "chair_exec_land": frozenset({"remote_argv", "remote_lane"}),
    "chair_facts_derive": frozenset({"remote_lane"}),
    "chair_pid_probe": frozenset({"remote_argv"}),
    "chair_read_approved": frozenset({"remote_lane"}),
    "chair_read_lost": frozenset({"remote_lane"}),
    "chair_read_stall": frozenset({"remote_lane"}),
    "chair_read_stranded": frozenset({"remote_lane"}),
    "cli_doctor": frozenset({"remote_doctor"}),
    "cli_host": frozenset({"remote_doctor"}),
    "cli_install": frozenset({"remote_argv"}),
    "cli_land": frozenset({"remote_lane"}),
    "cli_route": frozenset({"remote_lane"}),
    "cli_route_launch": frozenset({"remote_argv", "remote_doctor", "remote_lane", "remote_launch"}),
    "cli_runs": frozenset({"remote_lane"}),
    "host_cmd": frozenset({"remote_argv"}),
    "lane_checkout_sync": frozenset({"remote_argv", "remote_fetch"}),
    "lane_prereqs": frozenset({"remote_argv"}),
    "lane_toolchain": frozenset({"remote_argv"}),
    "release_deploy": frozenset({"remote_argv"}),
    "runs_stop": frozenset({"remote_argv", "remote_lane"}),
}


def remote_modules() -> frozenset[str]:
    return frozenset(path.stem for path in TOOLS.glob("remote_*.py"))


def remote_imports() -> dict[str, frozenset[str]]:
    """Module stem -> the `remote_*` modules it imports, at any depth, for every scanned module but the remote ones."""
    remote = remote_modules()
    found: dict[str, frozenset[str]] = {}
    for package in SCANNED:
        for path in sorted((ROOT / package).rglob("*.py")):
            if "tests" in path.relative_to(ROOT / package).parts or path.stem in remote:
                continue
            names: set[str] = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names |= {alias.name.split(".")[-1] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names |= {node.module.split(".")[-1], *(alias.name for alias in node.names)}
            if used := frozenset(names & remote):
                found[path.stem] = used
    return found


def test_nothing_new_imports_a_remote_module() -> None:
    grown = {
        stem: sorted(used - EXISTING.get(stem, frozenset()))
        for stem, used in remote_imports().items()
        if stem not in RETIRING and not used <= EXISTING.get(stem, frozenset())
    }
    assert grown == {}


def test_an_importer_that_let_go_leaves_the_list() -> None:
    now = remote_imports()
    assert {stem: sorted(used - now.get(stem, frozenset())) for stem, used in EXISTING.items() if not used <= now.get(stem, frozenset())} == {}
