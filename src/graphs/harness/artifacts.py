"""Step artifacts in object storage: each step's log, transcript and patch, written once under a content-named key.

The root is the provider profile's `artifacts_url`, resolved as `traces_url` is (`traces_url.resolve_traces_root`): a
plain path (relative to the runs directory), `file://`, `s3://` with the `object_store` block, or a `coxswain.storage`
plugin. Without the key there is no root and nothing is uploaded; the local run folder stays what it always was.

Layout, under the root:

    v1/runs/<run_id>/<step with '/' as '__'>/<name>@<first 12 hex of the sha256>
        e.g. v1/runs/ship-7/task__t1__build/patch.diff@3f1a9c0b2d4e
             v1/runs/ship-7/task__t1__build/transcript-build-1.jsonl@0c9e1d2a7b44
             v1/runs/ship-7/task__t1__checks/01-pytest.log@a81f00c3e9d2

**Write-once, and safe to retry.** The content hash is in the key, so a key names one content. Writing a key that is
already there is a no-op once its size matches; a size that differs is `ArtifactConflict`, a hash-prefix collision.
Nothing is downloaded to compare. A save that uploaded and then failed before its row was written is retried by
writing the same keys again. A rebuild with a different patch writes a different key, so no step is ever locked out.
- `file://` and plain paths: the bytes go to a temporary name in the key's own directory, are fsynced, and are then
  hard-linked to the key, which fails if the key exists. A write cut off part way leaves a `.part` file, never a
  truncated key. A filesystem with no hard links (SMB, some FUSE mounts) fails every link, so every save on it is
  marked missing: use a local disk, NFS, or `s3://`.
- `s3://`: one PutObject, or one multipart upload, is all or nothing on the server, so no reader sees a partial
  object. pyarrow's S3 filesystem cannot send `If-None-Match`, and staging the object then copying it would only add a
  copy, so the guard against two writers is the run claim: a run id is one claim, and one claim has one writer.
  Timeouts are bounded (`S3_BOUNDS`) so a broken bucket cannot stall a save.

**A broken bucket never drops a step row.** `step_artifacts` does not raise. If an upload fails, it stops uploading
for that save. Every row is still written. `artifacts_json` keeps only the items that were written, each
`{key, bytes, sha256, media}`, plus `scrubbed: true` on one the scrub changed. A row that carried an artifact which was not written records the failure in its own
field, `result_json.artifacts_error` (see `store_types.StepRow`). The worker only marks the row. The chair reads the
marked rows and tells the operator once per run (`agent_tools.chair_stall_alarm`), because a worker's scoped store
role can write nothing else.

**What a step carries.** The build step carries its patch, and the checks step carries each check's non-empty log.
Each step carries the trace files of the task's node calls whose role is of its kind (`ROLE_KINDS`). Two limits
follow from today's model:
- A fix-loop re-entry (`task/<task>-refix-<n>/...`) is a new graph run of the same task. Its steps also list the
  transcripts of earlier attempts in this run, because node calls are matched by task and not by graph run.
- A later fix round inside one graph run writes no new row, because its step key is the one round 1 wrote. Its
  transcripts are therefore uploaded nowhere.

**Secrets.** Checks inherit the lane's environment, so a check log or a transcript may print a token, a password or
a store URL. Before upload, `scrub` replaces two things with `***` in every source but the patch. The first is the value of every environment
variable whose name holds TOKEN, SECRET, PASSWORD, PASSWD or KEY (`secret_values`), in each of the forms
`forgejo_api.redact` knows. The second is the userinfo, and any password in the query (`store_switch.display`), of
every URL in the text. That is a floor, not a guarantee, so the bucket must be private to the fleet and reached with
credentials scoped to it. An item whose bytes the scrub changed carries `scrubbed: true` in `artifacts_json`, so a
reader knows the stored copy is not the bytes the step saw.

**The patch is never scrubbed** (`UNSCRUBBED`, `text/x-diff`). It is repo content on its way into git, so the stored
copy must be the change itself, byte for byte: a test fixture holding `postgresql://cox:cox@localhost/db` lands as
written, and a scrubbed copy would be a different change. A secret in a patch is the patch's problem, and the review
and the land see it there.

`key`, `entry`, `decide`, `secret_values`, `scrub`, `prepared`, `sources`, `attach` and `mark_missing` are pure.
`artifacts_root`, `put_object`, `upload` and `step_artifacts` are the edges.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from harness.store_dialect import Connection, JsonObject, json_load
from harness.store_types import StepRow
from harness.traces_url import redact_url, resolve_traces_root

if TYPE_CHECKING:
    from pyarrow.fs import FileSystem

__all__ = [
    "ERROR_FIELD",
    "LAYOUT",
    "ROLE_KINDS",
    "S3_BOUNDS",
    "SCRUBBED_FIELD",
    "UNSCRUBBED",
    "ArtifactConflict",
    "ArtifactsRoot",
    "Source",
    "artifacts_root",
    "attach",
    "decide",
    "entry",
    "key",
    "mark_missing",
    "prepared",
    "put_object",
    "scrub",
    "secret_values",
    "sources",
    "step_artifacts",
    "upload",
]

LAYOUT = "v1"
ERROR_FIELD = "artifacts_error"  # the `result_json` key a row whose artifacts were not all written carries
SCRUBBED_FIELD = "scrubbed"  # the `artifacts_json` item key, true on an item whose bytes the scrub changed
UNSCRUBBED = frozenset({"text/x-diff"})  # media stored as written: the patch is repo content headed for git
SECRET_NAME_PARTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "KEY")
# Seconds, and attempts: a save waits at most about connect + request per attempt per object, then gives up.
S3_BOUNDS: Mapping[str, float] = {"connect_timeout": 5.0, "request_timeout": 30.0, "max_attempts": 2}

# The step kind each lifecycle role's transcript belongs to. A role not named here (a phase validator) has no task step.
ROLE_KINDS: Mapping[str, str] = {
    "plan": "plan",
    "plan_adversary": "plan",
    "plan_alternative": "plan",
    "plan_arbitrate": "plan",
    "build": "build",
    "review_charter": "review",
    "review_adversary": "adversary",
    "arbitrate": "arbitration",
}

_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SECRET_MIN = 8  # a shorter value would redact ordinary text
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>`]+")
_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/?#@]+@")


class ArtifactConflict(ValueError):
    """A key already holds an object of another size: two contents share a hash prefix."""


@dataclass(frozen=True)
class ArtifactsRoot:
    """Where artifacts go: a pyarrow filesystem, the root path on it, the URL as it may be shown, and the secret values
    of the lane's environment (`secret_values`) to scrub from every upload."""

    fs: FileSystem
    path: str
    shown: str
    secrets: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class Source:
    """One artifact a step carries: inline `text`, or the local file at `path`."""

    step: str
    name: str
    media: str
    text: str | None = None
    path: Path | None = None


def _part(value: str, what: str) -> str:
    if not _PART.match(value) or value in (".", "..") or "__" in value:
        raise ValueError(f"artifact {what} {value!r} is empty, holds '__' or is not a plain name")
    return value


def key(run_id: str, step: str, name: str, sha256: str) -> str:
    """Pure. `v1/runs/<run_id>/<step with '/' as '__'>/<name>@<sha12>`. Refuses a part that is empty, holds '/' or
    '__', or is '..', so no two steps can spell one directory."""
    segments = [_part(s, "step") for s in step.split("/")]
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError(f"artifact digest {sha256!r} is not a sha256")
    return f"{LAYOUT}/runs/{_part(run_id, 'run id')}/{'__'.join(segments)}/{_part(name, 'name')}@{sha256[:12]}"


def entry(object_key: str, data: bytes, media: str, *, scrubbed: bool = False) -> JsonObject:
    """Pure. The `artifacts_json` item for one uploaded object; `scrubbed: true` only on one the scrub changed."""
    item: JsonObject = {"key": object_key, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "media": media}
    return {**item, SCRUBBED_FIELD: True} if scrubbed else item


def decide(object_key: str, size: int, existing_size: int | None) -> Literal["write", "same"]:
    """Pure. Write a key that holds nothing; leave one that holds an object of this size, which with the hash in the
    key is this content; refuse one of another size."""
    if existing_size is None:
        return "write"
    if existing_size == size:
        return "same"
    raise ArtifactConflict(f"artifact {object_key} already holds {existing_size} bytes, not {size}")


def secret_values(env: Mapping[str, str]) -> tuple[str, ...]:
    """Pure. The values, of at least 8 characters, of every variable whose name holds TOKEN, SECRET, PASSWORD, PASSWD
    or KEY, longest first, so a secret that holds another is replaced whole."""
    found = {v for k, v in env.items() if any(part in k.upper() for part in SECRET_NAME_PARTS) and len(v) >= _SECRET_MIN}
    return tuple(sorted(found, key=lambda v: (-len(v), v)))


def _url_scrubbed(match: re.Match[str]) -> str:
    from agent_tools.store_switch import display  # the tools package: imported on use, as land_claims does

    return display(_USERINFO.sub(r"\1***@", match.group(0)))


def scrub(text: str, secrets: Sequence[str]) -> str:
    """Pure. `text` with each secret, in every form `forgejo_api.redact` replaces, then the userinfo and any query
    password of every URL, replaced by `***`."""
    from agent_tools.forgejo_api import redact  # the tools package: imported on use, as land_claims does

    for secret in sorted({s for s in secrets if len(s) >= _SECRET_MIN}, key=len, reverse=True):
        text = redact(text, secret)
    return _URL.sub(_url_scrubbed, text)


def _scrub_bytes(data: bytes, secrets: Sequence[str]) -> bytes:
    """`scrub` over bytes that may not be UTF-8: bytes that do not decode pass through unchanged."""
    return scrub(data.decode("utf-8", "surrogateescape"), secrets).encode("utf-8", "surrogateescape")


def prepared(media: str, data: bytes, secrets: Sequence[str]) -> tuple[bytes, bool]:
    """Pure. The bytes to store for a source of `media`, and whether the scrub changed them. A patch (`UNSCRUBBED`) is
    stored as written."""
    if media in UNSCRUBBED:
        return data, False
    stored = _scrub_bytes(data, secrets)
    return stored, stored != data


def _mapping(value: object) -> Mapping[str, object]:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, object]]:
    return [_mapping(v) for v in cast("list[object]", value) if isinstance(v, Mapping)] if isinstance(value, list) else []


def _ran_checks(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The checks as `store_steps` reads them: under change_facts in an epic save, else at the top."""
    return _rows(_mapping(result.get("change_facts")).get("checks")) or _rows(result.get("checks"))


def _check_name(number: int, check: Mapping[str, object]) -> str:
    stem = re.sub(r"_{2,}", "_", _UNSAFE.sub("-", str(check.get("name") or "check"))).strip("-._") or "check"
    return f"{number:02d}-{stem}.log"


def _trace_name(trace: Path) -> str:
    return "transcript-" + re.sub(r"_{2,}", "_", _UNSAFE.sub("-", trace.name))


def _of_row(
    row: StepRow, result: Mapping[str, object], run_dir: Path, calls: Sequence[tuple[str, Path]]
) -> Iterable[Source]:
    step, kind = row["step"], row["kind"]
    if kind == "build":
        patch = str(_mapping(result.get("build")).get("patch") or "")
        if patch.strip():
            yield Source(step, "patch.diff", "text/x-diff", text=patch)
    if kind == "checks":
        for number, check in enumerate(_ran_checks(result), 1):
            log = check.get("log")
            if isinstance(log, str) and log:
                yield Source(step, _check_name(number, check), "text/plain", path=run_dir / log)
    for role, trace in calls:
        if ROLE_KINDS.get(role) == kind:
            yield Source(step, _trace_name(trace), "application/x-ndjson", path=trace)


def sources(
    rows: Sequence[StepRow],
    result: Mapping[str, object],
    *,
    run_dir: Path,
    calls: Sequence[tuple[str, Path]] = (),
) -> tuple[Source, ...]:
    """Pure. What each step row carries: the build's patch, each check's log, and each node call's transcript.

    `run_dir` is `<runs_dir>/<run_id>`, which a check's `log` is relative to. `calls` are the task's node calls as
    `(role, trace file)`; each goes to the step of its role's kind (`ROLE_KINDS`)."""
    return tuple(s for row in rows for s in _of_row(row, result, run_dir, calls))


def attach(row: StepRow, entries: Sequence[JsonObject]) -> StepRow:
    """Pure. `row` with `entries` after whatever artifacts it already lists."""
    return cast(StepRow, {**row, "artifacts_json": [*row["artifacts_json"], *entries]})


def mark_missing(row: StepRow, error: str) -> StepRow:
    """Pure. `row` with `result_json.artifacts_error` saying why some of its artifacts were not written. Its
    `artifacts_json` still lists only objects that were written."""
    return cast(StepRow, {**row, "result_json": {**row["result_json"], ERROR_FIELD: error}})


def _resolved_url(url: str, runs_dir: Path) -> str:
    """A plain relative path is under the runs directory, as `<runs_dir>/traces` is; anything else stays as written."""
    return url if _SCHEME.match(url) or Path(url).is_absolute() else str(runs_dir / url)


def _s3_extra() -> dict[str, Any]:
    from pyarrow.fs import AwsStandardS3RetryStrategy

    return {"connect_timeout": S3_BOUNDS["connect_timeout"], "request_timeout": S3_BOUNDS["request_timeout"],
            "retry_strategy": AwsStandardS3RetryStrategy(max_attempts=int(S3_BOUNDS["max_attempts"]))}


def artifacts_root(profile: Mapping[str, Any], env: Mapping[str, str], runs_dir: Path) -> ArtifactsRoot | None:
    """Edge. The root the provider profile's `artifacts_url` names; None without one. A bad URL raises ValueError."""
    url = profile.get("artifacts_url")
    if not isinstance(url, str) or not url:
        return None
    raw = profile.get("object_store")
    block = cast("Mapping[str, Any]", raw) if isinstance(raw, Mapping) else None
    resolved = _resolved_url(url, runs_dir)
    found = resolve_traces_root(resolved, runs_dir, env, block, label="artifacts", s3_extra=_s3_extra)
    return ArtifactsRoot(found.fs, found.path.rstrip("/"), redact_url(url), secret_values(env))


def _existing_size(root: ArtifactsRoot, path: str) -> int | None:
    from pyarrow.fs import FileType

    info = root.fs.get_file_info(path)
    return None if info.type == FileType.NotFound else int(info.size or 0)


def _link_local(path: str, data: bytes, link: Callable[[str, str], None]) -> bool:
    """Write `data` to a temporary name beside `path`, fsync it, and hard-link it to `path`; False if `path` exists."""
    final = Path(path)
    final.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=final.parent, prefix=f".{final.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        try:
            link(tmp, str(final))
        except FileExistsError:
            return False
        directory = os.open(final.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def put_object(
    root: ArtifactsRoot, object_key: str, data: bytes, link: Callable[[str, str], None] = os.link
) -> bool:
    """Edge. Write `data` at `object_key` under the root; True when written, False when it was already there.

    Compares sizes only: the key carries the content hash. `link` is the hard link a local write ends with."""
    from pyarrow.fs import LocalFileSystem

    path = f"{root.path}/{object_key}"
    if decide(object_key, len(data), _existing_size(root, path)) == "same":
        return False
    if isinstance(root.fs, LocalFileSystem):
        if _link_local(path, data, link):
            return True
        decide(object_key, len(data), _existing_size(root, path))  # another writer won: same size, or a conflict
        return False
    with root.fs.open_output_stream(path) as stream:  # S3: one put, all or nothing on the server
        stream.write(data)
    return True


def _bytes_of(source: Source) -> bytes | None:
    if source.text is not None:
        return source.text.encode("utf-8")
    try:
        return source.path.read_bytes() if source.path is not None else None
    except FileNotFoundError:
        return None


def _shown(exc: BaseException, root: ArtifactsRoot) -> str:
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return scrub(text, root.secrets)[:500]


def upload(
    root: ArtifactsRoot,
    run_id: str,
    rows: Sequence[StepRow],
    found: Sequence[Source],
    read: Callable[[Source], bytes | None] = _bytes_of,
    link: Callable[[str, str], None] = os.link,
) -> tuple[tuple[StepRow, ...], str | None]:
    """Edge. Upload each source, scrubbed unless it is the patch (`prepared`), then give back the rows listing what was written, and the error that stopped
    the uploads, or None.

    A source whose file is gone, or that is empty, is left out: an empty object carries nothing, and an S3 server may
    refuse its empty upload part. The first failure stops the uploads, and each row with a source that was not written
    is marked (`mark_missing`); no row ever names an object that was not written."""
    entries: dict[str, list[JsonObject]] = {}
    short: set[str] = set()
    error: str | None = None
    for source in found:
        if error is not None:
            short.add(source.step)
            continue
        data = read(source)
        if not data:
            continue
        data, scrubbed = prepared(source.media, data, root.secrets)
        object_key = key(run_id, source.step, source.name, hashlib.sha256(data).hexdigest())
        try:
            put_object(root, object_key, data, link)
        except Exception as exc:
            error = _shown(exc, root)
            short.add(source.step)
            continue
        entries.setdefault(source.step, []).append(entry(object_key, data, source.media, scrubbed=scrubbed))
    listed = (attach(row, entries.get(row["step"], [])) for row in rows)
    return tuple(mark_missing(r, cast(str, error)) if r["step"] in short else r for r in listed), error


def _task_calls(conn: Connection, run_id: str, phase: str, task: str) -> list[tuple[str, Path]]:
    """The task's node calls in this run that name a trace file, as (role, trace path), in call order."""
    p = conn.dialect.placeholder
    sql = f"SELECT role, phase_id, detail_json FROM node_calls WHERE run_id = {p} AND task_id = {p} ORDER BY seq"
    found: list[tuple[str, Path]] = []
    for role, phase_id, detail in conn.query_all(sql, (run_id, task)):
        trace = _mapping(json_load(detail)).get("trace")
        if phase_id in (None, "", phase) and isinstance(trace, str) and trace:
            found.append((str(role), Path(trace)))
    return found


def step_artifacts(
    root: ArtifactsRoot | None,
    conn: Connection,
    rows: Sequence[StepRow],
    result: Mapping[str, object],
    *,
    runs_dir: Path,
    run_id: str,
    phase: str,
    task: str,
) -> tuple[StepRow, ...]:
    """Edge. The save path's one call, which never raises. With a root, it uploads the artifacts of each row that is
    not yet stored and returns the rows listing their keys. Without a root, it returns the rows unchanged.

    A stored row is passed through untouched: `put_step` keeps the first row. If the uploads fail, the rows are still
    returned, each one that carried artifacts marked (`mark_missing`); the chair raises the alarm from the marks."""
    if root is None or not rows:
        return tuple(rows)
    try:
        p = conn.dialect.placeholder
        stored = {str(r[0]) for r in conn.query_all(f"SELECT step FROM step_results WHERE run_id = {p}", (run_id,))}
        fresh = [row for row in rows if row["step"] not in stored]
        found = sources(fresh, result, run_dir=runs_dir / run_id, calls=_task_calls(conn, run_id, phase, task))
        done, error = upload(root, run_id, fresh, found)
    except Exception as exc:
        error = _shown(exc, root)
        done = tuple(mark_missing(row, error) for row in rows)
    by_step = {row["step"]: row for row in done}
    return tuple(by_step.get(row["step"], row) for row in rows)
