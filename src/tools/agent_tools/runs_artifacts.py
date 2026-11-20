"""`cox runs artifacts <run>`: the objects a run's step rows name, read from the rows and never from the bucket.

`listing` and `lines` are pure: they take the decoded `step_results` rows and give the rows to print. A row whose
artifacts were not all written carries `result_json.artifacts_error` (`harness.artifacts.mark_missing`), listed as a
missing entry with that error after the row's written keys.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["ERROR_FIELD", "lines", "listing"]

ERROR_FIELD = "artifacts_error"  # harness.artifacts.ERROR_FIELD, copied so this module imports no harness code


def _items(step: str, row: Mapping[str, object]) -> list[dict[str, object]]:
    items = row.get("artifacts_json")
    found: list[dict[str, object]] = [
        {"step": step, "key": str(i["key"]), "bytes": i.get("bytes"), "sha256": str(i.get("sha256") or ""),
         "media": str(i.get("media") or ""), **({"scrubbed": True} if i.get("scrubbed") is True else {})}
        for i in (items if isinstance(items, list) else [])
        if isinstance(i, Mapping) and i.get("key")
    ]
    result = row.get("result_json")
    error = result.get(ERROR_FIELD) if isinstance(result, Mapping) else None
    if error:
        found.append({"step": step, "missing": True, "error": str(error)})
    return found


def listing(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Pure. One entry per artifact in step order, then the order the row lists them; a row's missing mark follows its
    keys."""
    return [e for row in rows for e in _items(str(row.get("step") or ""), row)]


def lines(run_id: str, entries: Sequence[Mapping[str, object]]) -> list[str]:
    """Pure. The human form: one `<key>  <bytes> B  <sha256 prefix>` line per artifact, ending `  scrubbed` when the
    secret scrub changed it, a `MISSING` line per mark, or why there are none."""
    if not entries:
        return [f"{run_id}: no artifacts recorded (no step rows list any; is artifacts_url set?)"]
    return [f"{e['step']}: MISSING artifacts: {e['error']}" if e.get("missing")
            else f"{e['key']}  {e['bytes']} B  {str(e['sha256'])[:12]}" + ("  scrubbed" if e.get("scrubbed") else "")
            for e in entries]
