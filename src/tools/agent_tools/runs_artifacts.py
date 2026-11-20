"""`cox runs artifacts <run>`: the objects a run's step rows name, read from the rows and never from the bucket.

`listing` and `lines` are pure: they take the decoded `step_results` rows and give the rows to print.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["lines", "listing"]


def listing(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Pure. One entry per artifact, in step order then the order the row lists them: step, key, bytes, sha256."""
    found: list[dict[str, object]] = []
    for row in rows:
        items = row.get("artifacts_json")
        for item in items if isinstance(items, list) else []:
            if isinstance(item, Mapping) and item.get("key"):
                found.append({"step": str(row.get("step") or ""), "key": str(item["key"]),
                              "bytes": item.get("bytes"), "sha256": str(item.get("sha256") or ""),
                              "media": str(item.get("media") or "")})
    return found


def lines(run_id: str, entries: Sequence[Mapping[str, object]]) -> list[str]:
    """Pure. The human form: one `<key>  <bytes> B  <sha256 prefix>` line per artifact, or why there are none."""
    if not entries:
        return [f"{run_id}: no artifacts recorded (no step rows list any; is artifacts_url set?)"]
    return [f"{e['key']}  {e['bytes']} B  {str(e['sha256'])[:12]}" for e in entries]
