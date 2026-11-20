"""`cox runs artifacts <run>`: the objects a run's step rows name, read from the rows and never from the bucket.

`listing` and `lines` are pure: they take the decoded `step_results` rows and give the rows to print. A row whose
uploads failed carries an `artifacts_missing` item (`harness.artifacts.mark_missing`), listed with its error.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["lines", "listing"]


def _one(step: str, item: Mapping[str, object]) -> dict[str, object] | None:
    if item.get("artifacts_missing"):
        return {"step": step, "missing": True, "error": str(item.get("error") or "")}
    if not item.get("key"):
        return None
    return {"step": step, "key": str(item["key"]), "bytes": item.get("bytes"), "sha256": str(item.get("sha256") or ""),
            "media": str(item.get("media") or "")}


def listing(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Pure. One entry per artifact, or per missing-artifacts mark, in step order then the order the row lists them."""
    found: list[dict[str, object]] = []
    for row in rows:
        items = row.get("artifacts_json")
        for item in items if isinstance(items, list) else []:
            one = _one(str(row.get("step") or ""), item) if isinstance(item, Mapping) else None
            if one is not None:
                found.append(one)
    return found


def lines(run_id: str, entries: Sequence[Mapping[str, object]]) -> list[str]:
    """Pure. The human form: one `<key>  <bytes> B  <sha256 prefix>` line per artifact, a `MISSING` line per mark, or
    why there are none."""
    if not entries:
        return [f"{run_id}: no artifacts recorded (no step rows list any; is artifacts_url set?)"]
    return [f"{e['step']}: MISSING artifacts: {e['error']}" if e.get("missing")
            else f"{e['key']}  {e['bytes']} B  {str(e['sha256'])[:12]}" for e in entries]
