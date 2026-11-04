"""Edge readers for the chair's session and agent transcripts and the price catalog. Every directory is an argument."""

from __future__ import annotations

import glob
import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import yaml

from agent_tools.stats_chair import catalog_prices


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeDecodeError:
        return []


def _parse_meta(text: str | None) -> dict | None:
    try:
        meta = json.loads(text) if text is not None else None
    except ValueError:
        return None
    return meta if isinstance(meta, dict) else None


def resolve_session(transcripts: Iterable[str], agent_dirs: Iterable[str], session: str) -> tuple[str, str]:
    """(status, id) from the matched transcript stems and subagent folder names. The id is set when exactly one distinct id matched, even with no transcript."""
    found = set(transcripts)
    ids = found | set(agent_dirs)
    if not session:
        return ("none", "")
    if len(ids) > 1:
        return ("ambiguous", "")
    return ("ok" if found else "not_found", min(ids, default=""))


def _transcripts(projects_dir: Path, session: str) -> list[Path]:
    """Session ids hold no dot, so `<id>.bak.jsonl` style neighbours are not sessions."""
    paths = Path(projects_dir).glob(f"*/{glob.escape(session)}*.jsonl") if session else []
    return sorted(p for p in paths if "." not in p.stem)


def _agent_dirs(projects_dir: Path, session: str) -> list[Path]:
    dirs = Path(projects_dir).glob(f"*/{glob.escape(session)}*/subagents") if session else []
    return sorted(d for d in dirs if "." not in d.parent.name)


def _match(projects_dir: Path, session: str) -> tuple[str, str]:
    """(status, id) of `session` as a prefix of the transcripts and subagent folders under `projects_dir`."""
    return resolve_session(
        (p.stem for p in _transcripts(projects_dir, session)),
        (d.parent.name for d in _agent_dirs(projects_dir, session)),
        session,
    )


def read_agents(projects_dir: Path, session: str) -> list[dict]:
    """Agents of the one session `session` names, sorted by agent_id; empty when none or ambiguous. An unreadable or non-JSON meta file is skipped."""
    _, resolved = _match(projects_dir, session)
    pattern = f"*/{glob.escape(resolved)}/subagents/agent-*.meta.json"
    found = (
        (path.name.removeprefix("agent-").removesuffix(".meta.json"), path)
        for path in (Path(projects_dir).glob(pattern) if resolved else [])
    )
    agents = (
        {
            "agent_id": agent_id,
            "meta": _parse_meta(_read_text(path)),
            "lines": _read_lines(path.with_name(f"agent-{agent_id}.jsonl")),
        }
        for agent_id, path in found
    )
    return sorted((a for a in agents if a["meta"] is not None), key=lambda a: a["agent_id"])


def read_session_lines(projects_dir: Path, session: str) -> list[str]:
    """Lines of the first `*/<id>.jsonl` for the session `session` names; empty unless the status is ok."""
    status, resolved = _match(projects_dir, session)
    matches = sorted(Path(projects_dir).glob(f"*/{glob.escape(resolved)}.jsonl")) if status == "ok" else []
    return _read_lines(matches[0]) if matches else []


def session_status(projects_dir: Path, session: str) -> str:
    """none, not_found, ambiguous or ok."""
    return _match(projects_dir, session)[0]


def _catalog_candidates(profile: Mapping) -> list[Path]:
    """Catalog locations in priority order: beside the provider profile, then under the cartridges dir."""
    beside = [Path(profile["provider_profile"]).parent / "catalog.yaml"] if profile.get("provider_profile") else []
    under = [Path(profile["cartridges_dir"]) / "providers" / "catalog.yaml"] if profile.get("cartridges_dir") else []
    return [*beside, *under]


def prices_for_profile(
    profile: Mapping | None,
    read_text: Callable[[Path], str | None] | None = None,
) -> dict[str, dict[str, float]]:
    """model -> price from the first readable catalog.yaml for `profile`; {} when none is readable or the YAML is invalid."""
    if not profile:
        return {}
    reader = read_text or _read_text
    texts = (text for text in map(reader, _catalog_candidates(profile)) if text is not None)
    first = next(texts, None)
    if first is None:
        return {}
    try:
        return catalog_prices(yaml.safe_load(first))
    except yaml.YAMLError:
        return {}


def catalog_status(profile: Mapping | None, read_text: Callable[[Path], str | None] | None = None) -> str:
    """ok when `prices_for_profile` finds at least one price, else missing."""
    return "ok" if prices_for_profile(profile, read_text) else "missing"
