"""Edge readers for the chair's session and agent transcripts and the price catalog. Every directory is an argument."""

from __future__ import annotations

import glob
import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Literal, NamedTuple

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


def resolve_by_content(hits: Mapping[Path, bool], session: str) -> tuple[str, Path | None]:
    """(status, transcript) from a map of transcript path to whether its text holds the session id. One distinct stem is ok; the same stem in two projects counts once."""
    if not session:
        return ("none", None)
    matched = sorted(path for path, hit in hits.items() if hit)
    stems = {path.stem for path in matched}
    if not stems:
        return ("not_found", None)
    if len(stems) > 1:
        return ("ambiguous", None)
    return ("ok", matched[0])


def _content_hits(projects_dir: Path, session: str) -> dict[Path, bool]:
    """Edge: for each `*/<stem>.jsonl` with no dot in the stem, whether its text contains `session`. An unreadable file is a miss."""
    paths = Path(projects_dir).glob("*/*.jsonl") if session else []
    return {p: session in (_read_text(p) or "") for p in sorted(paths) if "." not in p.stem}


def _resolve(projects_dir: Path, session: str) -> tuple[str, str, Path | None]:
    """(status, name-matched id, content-matched transcript). The content match is tried only when no file name matched."""
    status, resolved = _match(projects_dir, session)
    if status == "not_found" and not resolved:
        content_status, transcript = resolve_by_content(_content_hits(projects_dir, session), session)
        return (content_status, "", transcript)
    return (status, resolved, None)


def _agents_from_metas(metas: Iterable[Path]) -> list[dict]:
    """Agents of the `agent-*.meta.json` paths, sorted by agent_id. An unreadable or non-JSON meta file is skipped."""
    found = ((path.name.removeprefix("agent-").removesuffix(".meta.json"), path) for path in metas)
    agents = (
        {
            "agent_id": agent_id,
            "meta": _parse_meta(_read_text(path)),
            "lines": _read_lines(path.with_name(f"agent-{agent_id}.jsonl")),
        }
        for agent_id, path in found
    )
    return sorted((a for a in agents if a["meta"] is not None), key=lambda a: a["agent_id"])


def read_agents(projects_dir: Path, session: str) -> list[dict]:
    """Agents of the one session `session` names, sorted by agent_id; empty when none or ambiguous. An unreadable or non-JSON meta file is skipped."""
    _, resolved, transcript = _resolve(projects_dir, session)
    pattern = f"*/{glob.escape(resolved)}/subagents/agent-*.meta.json"
    metas = (
        transcript.parent.glob(f"{glob.escape(transcript.stem)}/subagents/agent-*.meta.json")
        if transcript
        else (Path(projects_dir).glob(pattern) if resolved else [])
    )
    return _agents_from_metas(metas)


def project_folder_name(workspace: Path) -> str:
    """Claude's folder name under its projects dir: every character of the path that is not an ASCII letter or digit becomes a hyphen."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(workspace))


def read_project_sessions(projects_dir: Path, workspace: Path) -> list[dict]:
    """Every session of `workspace` as {session_id, lines, agents}, sorted by session_id. Stems with a dot are not sessions; nothing raises."""
    folder = Path(projects_dir) / project_folder_name(workspace)
    transcripts = (p for p in folder.glob("*.jsonl") if "." not in p.stem and p.is_file())
    return sorted(
        (
            {
                "session_id": path.stem,
                "lines": _read_lines(path),
                "agents": _agents_from_metas(folder.glob(f"{glob.escape(path.stem)}/subagents/agent-*.meta.json")),
            }
            for path in transcripts
        ),
        key=lambda s: s["session_id"],
    )


def read_session_lines(projects_dir: Path, session: str) -> list[str]:
    """Lines of the first `*/<id>.jsonl` for the session `session` names; empty unless the status is ok."""
    status, resolved, transcript = _resolve(projects_dir, session)
    if transcript:
        return _read_lines(transcript)
    matches = sorted(Path(projects_dir).glob(f"*/{glob.escape(resolved)}.jsonl")) if status == "ok" else []
    return _read_lines(matches[0]) if matches else []


class Entry(NamedTuple):
    """One file as last read. `offset` is the byte after the last newline read; `lines` ends with an unterminated tail, so only `lines[:done]` is final. A meta file holds its parsed `meta` and no lines."""

    size: int
    mtime_ns: int
    offset: int
    lines: tuple[str, ...]
    done: int
    meta: dict | None = None


Cache = Mapping[Path, Entry]
Plan = Literal["keep", "append", "fresh", "drop"]


def plan_read(prev: Entry | None, size: int | None, mtime_ns: int | None) -> Plan:
    """What to do with a file given its last entry and its stat now. Same size with a new mtime is a rewrite."""
    if size is None or mtime_ns is None:
        return "drop"
    if prev is None or size < prev.size:
        return "fresh"
    if size == prev.size:
        return "keep" if mtime_ns == prev.mtime_ns else "fresh"
    return "append"


def extend_lines(final: tuple[str, ...], chunk: bytes) -> tuple[tuple[str, ...], int, int] | None:
    """(lines, final count, bytes consumed) after adding `chunk` to the `final` lines; None when the chunk is not UTF-8. Bytes after the last newline are shown but not consumed."""
    cut = chunk.rfind(b"\n") + 1
    try:
        head, tail = chunk[:cut].decode("utf-8"), chunk[cut:].decode("utf-8")
    except UnicodeDecodeError:
        return None
    done = (*final, *head.splitlines())
    return ((*done, *tail.splitlines()), len(done), cut)


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _read_from(path: Path, offset: int) -> bytes | None:
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            return fh.read()
    except OSError:
        return None


def refresh_entry(
    path: Path,
    prev: Entry | None,
    stat: Callable[[Path], tuple[int, int] | None] | None = None,
    read_from: Callable[[Path, int], bytes | None] | None = None,
) -> Entry | None:
    """The entry for a transcript now, reading only what `plan_read` needs; None when it is gone or unreadable. An unchanged file is not opened."""
    now = (stat or _stat)(path)
    size, mtime_ns = now if now else (None, None)
    plan = plan_read(prev, size, mtime_ns)
    if plan == "drop" or now is None:
        return None
    if plan == "keep":
        return prev
    base = prev if plan == "append" and prev else None
    offset = base.offset if base else 0
    chunk = (read_from or _read_from)(path, offset)
    if chunk is None:
        return None
    grown = extend_lines(base.lines[: base.done] if base else (), chunk)
    if grown is None:
        return Entry(now[0], now[1], 0, (), 0)
    lines, done, consumed = grown
    return Entry(now[0], now[1], offset + consumed, lines, done)


def refresh_meta(
    path: Path, prev: Entry | None, stat: Callable[[Path], tuple[int, int] | None] | None = None
) -> Entry | None:
    """The entry for an agent meta file now; it is parsed only when it is new or changed."""
    now = (stat or _stat)(path)
    plan = plan_read(prev, *(now or (None, None)))
    if plan == "drop" or now is None:
        return None
    if plan == "keep":
        return prev
    return Entry(now[0], now[1], 0, (), 0, _parse_meta(_read_text(path)))


def _refreshed(
    paths: Iterable[Path], cache: Cache, refresh: Callable[[Path, Entry | None], Entry | None]
) -> dict[Path, Entry]:
    pairs = [(path, refresh(path, cache.get(path))) for path in paths]
    return {path: entry for path, entry in pairs if entry is not None}


def _agents_cached(metas: Iterable[Path], cache: Cache) -> tuple[dict[Path, Entry], list[dict]]:
    """Cached counterpart of `_agents_from_metas`: the entries seen and the agents."""
    metas_seen = _refreshed(metas, cache, refresh_meta)
    valid = {
        path: path.with_name(f"agent-{path.name.removeprefix('agent-').removesuffix('.meta.json')}.jsonl")
        for path, entry in metas_seen.items()
        if entry.meta is not None
    }
    lines_seen = _refreshed(valid.values(), cache, refresh_entry)
    agents = [
        {
            "agent_id": path.name.removeprefix("agent-").removesuffix(".meta.json"),
            "meta": metas_seen[path].meta,
            "lines": list(lines_seen[jsonl].lines) if jsonl in lines_seen else [],
        }
        for path, jsonl in valid.items()
    ]
    return ({**metas_seen, **lines_seen}, sorted(agents, key=lambda a: a["agent_id"]))


def read_project_sessions_cached(projects_dir: Path, workspace: Path, cache: Cache) -> tuple[Cache, list[dict]]:
    """`read_project_sessions` past the `cache` of the last call: (new cache, sessions). A file that grew is read from its offset; the new cache holds only files seen now."""
    folder = Path(projects_dir) / project_folder_name(workspace)
    transcripts = [p for p in folder.glob("*.jsonl") if "." not in p.stem and p.is_file()]
    lines_seen = _refreshed(transcripts, cache, refresh_entry)
    per_agents = {
        path: _agents_cached(folder.glob(f"{glob.escape(path.stem)}/subagents/agent-*.meta.json"), cache)
        for path in transcripts
    }
    sessions = [
        {
            "session_id": path.stem,
            "lines": list(lines_seen[path].lines) if path in lines_seen else [],
            "agents": per_agents[path][1],
        }
        for path in transcripts
    ]
    seen = {k: v for entries, _ in per_agents.values() for k, v in entries.items()}
    return ({**lines_seen, **seen}, sorted(sessions, key=lambda s: s["session_id"]))


def read_session_lines_cached(projects_dir: Path, session: str, cache: Cache) -> tuple[Cache, list[str]]:
    """`read_session_lines` past the `cache`: (new cache, lines)."""
    status, resolved, transcript = _resolve(projects_dir, session)
    matches = (
        [transcript]
        if transcript
        else sorted(Path(projects_dir).glob(f"*/{glob.escape(resolved)}.jsonl"))
        if status == "ok"
        else []
    )
    seen = _refreshed(matches[:1], cache, refresh_entry)
    return (seen, list(seen[matches[0]].lines) if matches and matches[0] in seen else [])


def read_agents_cached(projects_dir: Path, session: str, cache: Cache) -> tuple[Cache, list[dict]]:
    """`read_agents` past the `cache`: (new cache, agents)."""
    _, resolved, transcript = _resolve(projects_dir, session)
    pattern = f"*/{glob.escape(resolved)}/subagents/agent-*.meta.json"
    metas = (
        transcript.parent.glob(f"{glob.escape(transcript.stem)}/subagents/agent-*.meta.json")
        if transcript
        else (Path(projects_dir).glob(pattern) if resolved else [])
    )
    return _agents_cached(metas, cache)


def session_status(projects_dir: Path, session: str) -> str:
    """none, not_found, ambiguous or ok. A session id found in no file name is matched by transcript content."""
    return _resolve(projects_dir, session)[0]


def _catalog_candidates(profile: Mapping) -> list[Path]:
    """Catalog locations in priority order: beside the provider profile, then under the cartridges dir."""
    beside = [Path(profile["provider_profile"]).parent / "catalog.yaml"] if profile.get("provider_profile") else []
    under = [Path(profile["cartridges_dir"]) / "providers" / "catalog.yaml"] if profile.get("cartridges_dir") else []
    return [*beside, *under]


def catalog_for_profile(profile: Mapping | None, read_text: Callable[[Path], str | None] | None = None) -> object | None:
    """The parsed first readable catalog.yaml for `profile`; None when none is readable or the YAML is invalid."""
    if not profile:
        return None
    reader = read_text or _read_text
    texts = (text for text in map(reader, _catalog_candidates(profile)) if text is not None)
    first = next(texts, None)
    if first is None:
        return None
    try:
        return yaml.safe_load(first)
    except yaml.YAMLError:
        return None


def prices_for_profile(
    profile: Mapping | None,
    read_text: Callable[[Path], str | None] | None = None,
) -> dict[str, dict[str, float]]:
    """model -> price from the first readable catalog.yaml for `profile`; {} when none is readable or the YAML is invalid."""
    catalog = catalog_for_profile(profile, read_text)
    return {} if catalog is None else catalog_prices(catalog)


def catalog_status(profile: Mapping | None, read_text: Callable[[Path], str | None] | None = None) -> str:
    """ok when `prices_for_profile` finds at least one price, else missing."""
    return "ok" if prices_for_profile(profile, read_text) else "missing"
