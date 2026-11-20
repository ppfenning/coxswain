"""Where the work item and intake files and the store's rows disagree. A report; the store is not authoritative."""

from __future__ import annotations

import json

from agent_tools import records

_COLUMNS = ("initiative", "task_id", "kind", "file_state", "store_state")


def _same_state(file_state: str, store_state: str) -> bool:
    return file_state == store_state


def _intake_states_agree(file_state: str, store_state: str) -> bool:
    """The store stamps a retired intake `done`; the file side names it `decomposed` or `landed`."""
    return file_state == store_state or (file_state in ("decomposed", "landed") and store_state == "done")


def _drift_rows(on_file: dict, in_store: dict, row_of, agree=_same_state) -> list[dict]:
    """One row per key in `on_file` or `in_store` that disagrees. `row_of` turns a key into the
    (initiative, task_id) pair the output names. Kinds: `state` (both sides, `agree` says no),
    `file_only`, `store_only`. The missing side's state is None."""
    return [
        {"initiative": i, "task_id": t, "kind": kind, "file_state": on_file.get(key), "store_state": in_store.get(key)}
        for key in on_file.keys() | in_store.keys()
        for i, t in [row_of(key)]
        for kind in [
            "file_only" if key not in in_store
            else "store_only" if key not in on_file
            else "state" if not agree(on_file[key], in_store[key])
            else None
        ]
        if kind is not None
    ]


def drift(
    files: list[tuple[str, str, str]],
    rows: list[dict],
    intake_files: list[tuple[str, str]] = (),
    intake_rows: list[dict] = (),
) -> list[dict]:
    """Disagreements, sorted by (initiative, task_id, kind).

    Tasks are keyed on (initiative, task_id); the file side's task_id is the frontmatter `id`, else the
    file stem, as the store mirror writes it. Intake is keyed on path and reported with `initiative`
    "intake" and `task_id` the path: `intake_files` pairs a path with its `intake_state.intake_group`
    state, `intake_rows` are the store's kind="intake" rows, matched by `intake/{task_id}.md`. A
    decomposed or landed intake file agrees with a done store row; a queued file does not."""
    task_on_file = {(i, t): s for i, t, s in files}
    task_in_store = {(r["initiative"], r["task_id"]): r["state"] for r in rows}
    intake_on_file = dict(intake_files)
    intake_in_store = {f"intake/{r['task_id']}.md": r["state"] for r in intake_rows}
    found = _drift_rows(task_on_file, task_in_store, lambda key: key) + _drift_rows(
        intake_on_file, intake_in_store, lambda key: ("intake", key), agree=_intake_states_agree
    )
    return sorted(found, key=lambda d: (d["initiative"], d["task_id"], d["kind"]))


def format_text(drift_rows: list[dict]) -> str:
    """The table, or `no drift` when there is nothing to show. A missing state prints as `-`."""
    shown = [{**d, "file_state": d["file_state"] or "-", "store_state": d["store_state"] or "-"} for d in drift_rows]
    return records.format_table(shown, _COLUMNS) if shown else "no drift"


def format_json(drift_rows: list[dict]) -> str:
    return json.dumps(drift_rows, indent=2, sort_keys=True)
