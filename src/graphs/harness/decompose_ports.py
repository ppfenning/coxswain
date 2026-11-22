"""What a decompose run needs from the tools package, passed in instead of imported.

`_run_graph` files the tasks a decompose cleared, reads them back, withdraws them when the run is partial, resolves
the profile's `repo_map`, and files follow-ups. Every one of those is a tools-side store or route call. The harness
does not import the tools package (`test_import_direction` holds it to that), so the composition root, `shell.py`,
builds these with `agent_tools.harness_ports.decompose_ports()` and hands them to `harness.cli.main`.
A run started without them refuses a decompose before any model call.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DecomposePorts:
    # (runs_dir, path, path_parts, text) -> exit code: `route file`'s writer for one task, store row then file.
    write_task: Callable[[Path, Path, tuple[str, ...], str], int]
    # (runs_dir, initiative) -> the queue rows `route edit` would see for that initiative.
    read_tasks: Callable[[Path, str], Sequence[Mapping[str, Any]]]
    # (runs_dir, root, initiative, rels, whole) -> the rels actually withdrawn.
    withdraw_tasks: Callable[[Path, Path, str, Sequence[str], bool], Sequence[str]]
    # (value, repos_root, is_checkout, home) -> the path a `repo_map` value names, or None when it is refused.
    resolve_repo: Callable[[str, Path, Callable[[Path], bool], str], str | None]
    # path -> whether it is a git checkout.
    is_checkout: Callable[[Path], bool]
    # (runs_dir, workspace, follow_up, after=, date=, home=) -> the filed intake's relative path, or None.
    file_follow_up: Callable[..., str | None]
