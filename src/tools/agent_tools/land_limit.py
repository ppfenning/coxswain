"""How many lands one repository admits at once.

The setting lives in the repo's cartridge overlay, `<repo>/.agent/cartridge.yaml`, as a top-level `land_limit:` or as
`repos.<directory name>.land_limit`. It is an int of at least 1 and defaults to 1. The overlay is read here, not through
`land_manual`, so the two settings stay independent.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from agent_tools.base_branch import OVERLAY

DEFAULT = 1


def _valid(value: object) -> bool:
    """An int of at least 1; a bool is not an int here."""
    return type(value) is int and value >= 1


def land_limit_setting(overlay: Mapping[str, Any] | None, repo_name: str) -> int:
    """`repos.<repo_name>.land_limit`, else the top-level `land_limit`, of a parsed overlay; 1 when neither is valid."""
    overlay = overlay or {}
    repos = overlay.get("repos")
    entry = repos.get(repo_name) if isinstance(repos, Mapping) else None
    candidates = (entry.get("land_limit") if isinstance(entry, Mapping) else None, overlay.get("land_limit"))
    return next((v for v in candidates if _valid(v)), DEFAULT)


def repo_land_limit(repo: str) -> int:
    """Edge. The repo overlay's land limit for its directory name; 1 when the file is absent, unreadable or malformed."""
    path = Path(repo).expanduser()
    try:
        overlay = yaml.safe_load((path / OVERLAY).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return DEFAULT
    return land_limit_setting(overlay if isinstance(overlay, Mapping) else None, path.name)
