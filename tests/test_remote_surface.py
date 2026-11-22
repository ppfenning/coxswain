"""F13: the old model's `remote_*` modules gain no new importer before E4 retires them.

E4 (stateless-impl-plan, item 25) removes `cox runs fetch`, `remote_fetch`, `remote_launch` and the ssh launch,
`chair_login_check`'s ssh probes and the `remote_unfetched` gates. Those files, and the `remote_*` modules
themselves, may import each other. Every other importer is listed below, with the remote modules it uses now, as a
ratchet: the list may only shrink. A new importer, or a listed one reaching for another remote module, fails.
The `cox` command modules reach these through `agent_tools.cli_lazy`'s proxies, so a name imported from there counts.
Today: 3 retiring files, and 21 listed importers, which are 20 modules plus `cli_lazy`, whose proxies name all five.
`devtools.import_scan` also counts an `import_module` or `__import__` of a literal or f-string name, and a string
literal naming an `agent_tools.remote_*` module.
"""

from __future__ import annotations

import ast
from pathlib import Path

from devtools.import_scan import module_names

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "src" / "tools" / "agent_tools"
SCANNED = ("src/tools/agent_tools", "src/graphs/harness", "src/graphs/graphs", "src/graphs/runner", "src/cartridges/core")

# The E4 retirement list: files that go with the old model, so their imports of `remote_*` are not growth.
RETIRING = frozenset({"cli_runs_fetch", "chair_login_check", "chair_read_remote_unfetched"})

# Importers outside the retirement list, with the remote modules each imports today. Only shrinks.
# remote_argv is the shared ssh argv builder: host_cmd, the installer and the lane prereq checks use it for things
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
    # the `cox` command modules' lazy proxies (`_LazyModule("agent_tools.remote_argv")`), one per remote module
    "cli_lazy": frozenset({"remote_argv", "remote_doctor", "remote_fetch", "remote_lane", "remote_launch"}),
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
    "runs_stop": frozenset({"remote_argv", "remote_lane"}),
}


def remote_modules() -> frozenset[str]:
    return frozenset(path.stem for path in TOOLS.glob("remote_*.py"))


def remote_reached(name: str, remote: frozenset[str]) -> set[str]:
    """Pure. The remote modules an `agent_tools.` name reaches; `remote_*` when an f-string's head is only a prefix of one."""
    if name.split(".")[0] != "agent_tools":
        return set()
    segments = name.split(".")[1:]
    exact = {seg for seg in segments if seg in remote}
    partial = {"remote_*" for seg in segments if seg not in remote and any(m.startswith(seg) and seg.startswith("remote_") for m in remote)}
    return exact | partial


def remote_imports() -> dict[str, frozenset[str]]:
    """Module stem -> the `remote_*` modules it reaches (`devtools.import_scan`), for every scanned module but the
    remote ones."""
    remote = remote_modules()
    found: dict[str, frozenset[str]] = {}
    for package in SCANNED:
        for path in sorted((ROOT / package).rglob("*.py")):
            if "tests" in path.relative_to(ROOT / package).parts or path.stem in remote:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if used := frozenset().union(*(remote_reached(name, remote) for _, name in module_names(tree))):
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


def test_the_scan_sees_a_literal_dynamic_and_an_f_string_reach() -> None:
    remote = frozenset({"remote_launch", "remote_lane"})
    source = "import_module('agent_tools.remote_launch')\nimport_module(f'agent_tools.remote_{kind}')\n"
    reached = [remote_reached(name, remote) for _, name in module_names(ast.parse(source))]
    assert set().union(*reached) == {"remote_launch", "remote_*"}
