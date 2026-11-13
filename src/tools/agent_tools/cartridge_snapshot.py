"""A cartridges directory as one snapshot in the store, keyed by the digest of its files.

Cartridges are written and versioned in git. This module only moves them: the chair publishes the directory it
launches with, and a lane host writes that exact snapshot to its own disk before the run. Nothing is copied from one
machine to another, and a lane that holds the wrong files is told so by the sha, not by a second comparison pass.

The planning is pure. The edge reads and writes files and the store's `cartridge_snapshots` table.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Files = dict[str, dict[str, Any]]  # path -> {"text": str} or {"b64": str} (plus "x": True when executable), or {"link": target}

_FETCHED = ".cartridges-fetched"  # under runs/: the sha this machine's cartridges directory was last written from

_SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".ruff_cache"})


def entry(data: bytes, executable: bool) -> dict[str, Any]:
    """Pure: one file as the snapshot holds it. Text stays readable in the store; anything else is base64."""
    try:
        held: dict[str, Any] = {"text": data.decode("utf-8")}
    except UnicodeDecodeError:
        held = {"b64": base64.b64encode(data).decode("ascii")}
    return {**held, **({"x": True} if executable else {})}


def data_of(held: Mapping[str, Any]) -> bytes:
    if "link" in held:
        return f"link:{held['link']}".encode()
    return base64.b64decode(held["b64"]) if "b64" in held else str(held["text"]).encode("utf-8")


def may_replace(on_disk_sha: str | None, last_fetched: str | None) -> bool:
    """Pure: whether a fetch may write over a directory. Yes when it holds nothing, or exactly what the last fetch
    left there. Anything else was put or edited there by a person: their cartridges are theirs, and are not replaced
    unless the caller says so."""
    return on_disk_sha is None or on_disk_sha == last_fetched


def sha_of(files: Mapping[str, Mapping[str, Any]]) -> str:
    """Pure: the digest of a snapshot: every path, whether it is executable, and its bytes, in path order."""
    digest = hashlib.sha256()
    for path in sorted(files):
        data = data_of(files[path])
        digest.update(f"{path}\0{'x' if files[path].get('x') else '-'}\0{len(data)}\0".encode())
        digest.update(data)
    return digest.hexdigest()


def plan_write(files: Mapping[str, Mapping[str, Any]], on_disk: Mapping[str, Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """Pure: (paths to write, paths to delete) that make a directory holding `on_disk` hold exactly `files`."""
    writes = sorted(path for path, held in files.items() if on_disk.get(path) != held)
    return writes, sorted(path for path in on_disk if path not in files)


def read_tree(root: Path) -> Files:
    """Edge: every file under `root` by its relative path. Version-control and cache directories are not cartridges."""
    root = Path(root)
    found: Files = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if path.is_symlink():  # kept as a link, never followed: a link to a shared base stays one on the lane
            found[rel.as_posix()] = {"link": os.readlink(path)}
        elif path.is_file():
            found[rel.as_posix()] = entry(path.read_bytes(), os.access(path, os.X_OK))
    return found


def _conn(runs_dir: Path) -> Any:
    from agent_tools import courier_bus  # one migrated connection per store for the life of the process

    return courier_bus._conn(Path(runs_dir).parent / "courier.jsonl")


def publish(runs_dir: Path, root: Path, by: str | None = None) -> str:
    """Edge: store the cartridges under `root` and return their sha. The same files publish nothing new.
    Raises when the directory holds no file or the store cannot take the snapshot."""
    from harness import store_cartridges

    files = read_tree(root)
    if not files:
        raise ValueError(f"{root} holds no cartridge files")
    sha = sha_of(files)
    conn = _conn(runs_dir)
    if not store_cartridges.has(conn, sha):
        at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        store_cartridges.put(conn, sha, at, by or socket.gethostname(), json.dumps(files, sort_keys=True))
    return sha


def fetch(runs_dir: Path, sha: str, root: Path, replace: bool = False) -> tuple[int, int]:
    """Edge: make `root` hold exactly the snapshot `sha`; (files written, files deleted). Nothing is touched when the
    directory already matches. Raises LookupError when the store has no such snapshot, ValueError when the
    stored files do not give the sha they are stored under or the directory does not match after the write, and
    PermissionError when `root` holds files no fetch put there and `replace` is not set (`may_replace`)."""
    from harness import store_cartridges

    root = Path(root)
    stored = store_cartridges.get(_conn(runs_dir), sha)
    if stored is None:
        raise LookupError(f"the store holds no cartridge snapshot {sha}")
    files: Files = json.loads(stored)
    if sha_of(files) != sha:
        raise ValueError(f"cartridge snapshot {sha} is damaged in the store: its files give {sha_of(files)}")
    on_disk = read_tree(root) if root.is_dir() else {}
    writes, deletes = plan_write(files, on_disk)
    marker = Path(runs_dir) / _FETCHED
    if (writes or deletes) and not replace:
        last = marker.read_text(encoding="utf-8").strip() if marker.is_file() else None
        if not may_replace(sha_of(on_disk) if on_disk else None, last):
            raise PermissionError(
                f"{root} holds cartridges that no fetch put there, or that were edited since: nothing was written. "
                "Pass --replace to overwrite them with the snapshot."
            )
    for path in writes:
        full = root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        if full.is_symlink() or full.exists():
            full.unlink()
        if "link" in files[path]:
            full.symlink_to(files[path]["link"])
            continue
        full.write_bytes(data_of(files[path]))
        full.chmod(0o755 if files[path].get("x") else 0o644)
    for path in deletes:
        (root / path).unlink()
    for directory in sorted((p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()), reverse=True):
        if not any(part in _SKIP_DIRS for part in directory.relative_to(root).parts) and not any(directory.iterdir()):
            directory.rmdir()
    now = sha_of(read_tree(root))
    if now != sha:
        raise ValueError(f"{root} gives {now} after the write, not {sha}")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(sha + "\n", encoding="utf-8")
    except OSError:
        pass  # the marker only spares a later --replace; the files are right
    return len(writes), len(deletes)
