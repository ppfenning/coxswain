"""The command line over the harness.

Thin on purpose. Everything here is argument plumbing; the machinery it drives
lives in the sibling modules, and the graphs it offers come from discovery
rather than a dispatch table — `python shell.py <graph>` works for any module
under `graphs/` that declares a SPEC.

`phase` is the one subcommand that is not a graph: it is the harness's own
driver, running the lifecycle graph once per ready task, concurrently.
"""

from __future__ import annotations

from typing import Any

from core import module_facade
from harness.cli_launch import profile_storage_url as profile_storage_url
from harness.cli_main import main as main
from harness.cli_run_graph import read_provider_profile as read_provider_profile

__all__ = ["main"]


# H9 (docs/hardening.md): the command line lives in the part modules above.
# This module answers for every name they define, and a write (a test's monkeypatch) reaches every part that
# holds the name (core.module_facade). Callers' names are re-exported here, typed; the rest resolve on read.
def __getattr__(name: str) -> Any:
    return module_facade.attribute(__name__, name)


module_facade.install(
    __name__,
    (
        "harness.cli_run_graph",
        "harness.cli_launch",
        "harness.cli_main",
    ),
    eager=True,
)
