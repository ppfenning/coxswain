"""Create or update the Coxswain database, datasets, charts and dashboard in Superset from the YAML specs.

Objects are matched by name. A second run against an unchanged server writes nothing.
The planner is pure: specs, the existing objects and the present sources go in, ordered operations come out.
Everything that touches the network, the environment or the filesystem sits under "Edge" at the bottom.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

import yaml

KINDS = ("database", "dataset", "chart", "dashboard")
NAME_FIELD = {"database": "database_name", "dataset": "table_name", "chart": "slice_name", "dashboard": "dashboard_title"}
SOURCE_NOTES = {"sqlite": "no cox.db yet", "parquet-traces": "no Parquet traces yet"}


class Op(NamedTuple):
    action: str  # create, update, unchanged or skipped
    kind: str
    name: str
    payload: dict[str, Any]
    reason: str = ""


def line(op: Op) -> str:
    return f"skipped: {op.name} ({op.reason})" if op.action == "skipped" else f"{op.action}: {op.kind} {op.name}"


def covers(have: Any, want: Any) -> bool:
    """True when `have` holds everything in `want`. The server may add keys to a dict, never to a list."""
    if isinstance(want, dict):
        return isinstance(have, dict) and all(k in have and covers(have[k], v) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(have, list) and len(have) == len(want) and all(covers(h, w) for h, w in zip(have, want))
    return have == want


def mask_uri(uri: str) -> str:
    """The URI as Superset returns it: a password becomes ten X."""
    return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:XXXXXXXXXX@", uri)


def loads(raw: Any) -> dict[str, Any]:
    """Superset returns params and query_context as JSON strings, or null. Read them as dicts."""
    return json.loads(raw) if isinstance(raw, str) and raw else (raw or {})


def detail_path(kind: str, pk: int) -> str:
    """Superset 4.x leaves sqlalchemy_uri out of GET /database/<pk>; only /connection returns it."""
    return f"/api/v1/database/{pk}/connection" if kind == "database" else f"/api/v1/{kind}/{pk}"


def axis_column(x: str, grain: str | None) -> dict[str, Any]:
    """The x axis as Superset's getXAxisColumn writes it: a BASE_AXIS column, timed only when a grain is set."""
    base = {"columnType": "BASE_AXIS", "expressionType": "SQL", "label": x, "sqlExpression": x}
    return {**base, "timeGrain": grain} if grain else base


def queries_of(params: dict[str, Any]) -> list[dict[str, Any]]:
    """The one query a timeseries chart runs. Only SIMPLE adhoc filters are read."""
    x, grain = params.get("x_axis"), params.get("time_grain_sqla")
    axis = [axis_column(x, grain)] if x else []
    metrics = params["metrics"]
    by_label = {m.get("label"): m for m in metrics}
    sort = params.get("x_axis_sort")
    return [
        {
            "columns": [*axis, *params.get("groupby", [])],
            "metrics": metrics,
            "orderby": [[by_label.get(sort, sort), params.get("x_axis_sort_asc", True)]] if sort else [],
            "row_limit": params.get("row_limit", 10000),
            "filters": [{"col": f["subject"], "op": f["operator"], "val": f["comparator"]} for f in params.get("adhoc_filters", []) if f["expressionType"] == "SIMPLE"],
            "extras": {"time_grain_sqla": grain} if grain else {},
        }
    ]


def query_context(queries: list[dict[str, Any]], dataset_id: int) -> dict[str, Any]:
    """The query context Superset's chart data endpoint needs saved beside the form data."""
    return {"datasource": {"id": dataset_id, "type": "table"}, "force": False, "queries": queries, "result_format": "json", "result_type": "full"}


def plan(specs: dict[str, Any], existing: dict[str, dict[str, dict]], present: frozenset[str] | set[str]) -> list[Op]:
    """Ordered operations: database, datasets, charts, dashboard.

    `existing` maps kind to name to the object as `normalize` shapes it. A dataset whose `requires`
    source is not in `present` is skipped, and so is every chart on it. `seen` is what `decide`
    compares when it differs from the payload, as with a masked database password.
    """

    def decide(kind: str, name: str, payload: dict, seen: dict | None = None) -> Op:
        have = existing.get(kind, {}).get(name)
        action = "create" if have is None else ("unchanged" if covers(have, payload if seen is None else seen) else "update")
        return Op(action, kind, name, payload)

    database, dash = specs["database"], specs["dashboard"]
    absent = {d["name"]: SOURCE_NOTES.get(d["requires"], f"no {d['requires']}") for d in specs["datasets"] if d["requires"] not in present}
    datasets = [
        Op("skipped", "dataset", d["name"], {}, absent[d["name"]])
        if d["name"] in absent
        else decide("dataset", d["name"], {"database": d["database"], "sql": d["sql"].strip()})
        for d in specs["datasets"]
    ]
    charts = [
        Op("skipped", "chart", c["name"], {}, f"dataset {c['dataset']} skipped")
        if c["dataset"] in absent
        else decide("chart", c["name"], {"dataset": c["dataset"], "viz_type": c["viz_type"], "params": c["params"], "queries": queries_of(c["params"])})
        for c in specs["charts"]
    ]
    live = {o.name for o in charts if o.action != "skipped"}
    grid = [row for row in ([cell for cell in row if cell["chart"] in live] for row in dash["grid"]) if row]
    board = decide("dashboard", dash["name"], {"grid": grid}) if grid else Op("skipped", "dashboard", dash["name"], {}, "no charts")
    uri = database["sqlalchemy_uri"]
    return [decide("database", database["name"], {"sqlalchemy_uri": uri}, {"sqlalchemy_uri": mask_uri(uri)}), *datasets, *charts, board]


def position(grid: list[list[dict]], chart_ids: dict[str, int], title: str) -> dict[str, Any]:
    """Superset's position_json for a grid of rows of {chart, width} cells."""
    cells = {
        f"CHART-{i}-{j}": {
            "type": "CHART",
            "id": f"CHART-{i}-{j}",
            "children": [],
            "parents": ["ROOT_ID", "GRID_ID", f"ROW-{i}"],
            "meta": {"chartId": chart_ids[c["chart"]], "sliceName": c["chart"], "width": c["width"], "height": 50},
        }
        for i, row in enumerate(grid)
        for j, c in enumerate(row)
    }
    rows = {
        f"ROW-{i}": {
            "type": "ROW",
            "id": f"ROW-{i}",
            "children": [f"CHART-{i}-{j}" for j in range(len(row))],
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        }
        for i, row in enumerate(grid)
    }
    return {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": list(rows), "parents": ["ROOT_ID"]},
        "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID", "meta": {"text": title}},
        **rows,
        **cells,
    }


def grid_of(pos: dict[str, Any]) -> list[list[dict]]:
    """The inverse of `position`: rows of {chart, width} read back from position_json."""
    rows = pos.get("GRID_ID", {}).get("children", [])
    return [[{"chart": pos[c]["meta"]["sliceName"], "width": pos[c]["meta"]["width"]} for c in pos[r]["children"]] for r in rows]


def normalize(kind: str, detail: dict[str, Any], dataset_names: dict[int, str]) -> dict[str, Any]:
    """A server object in the same name-based shape `plan` builds its payloads in."""
    if kind == "database":
        return {"sqlalchemy_uri": mask_uri(detail.get("sqlalchemy_uri") or "")}
    if kind == "dataset":
        return {"database": detail["database"]["database_name"], "sql": (detail.get("sql") or "").strip()}
    if kind == "chart":
        params = loads(detail.get("params"))
        # GET /chart/<pk> has no datasource_id in Superset 4.x; params.datasource ("<id>__table") names it.
        given = detail.get("datasource_id")
        ds = given if given is not None else int(params["datasource"].split("__")[0]) if "datasource" in params else None
        return {"dataset": dataset_names.get(ds), "viz_type": detail["viz_type"], "params": params, "queries": loads(detail.get("query_context")).get("queries")}
    return {"grid": grid_of(json.loads(detail.get("position_json") or "{}"))}


def names_of(specs: dict[str, Any]) -> dict[str, set[str]]:
    return {
        "database": {specs["database"]["name"]},
        "dataset": {d["name"] for d in specs["datasets"]},
        "chart": {c["name"] for c in specs["charts"]},
        "dashboard": {specs["dashboard"]["name"]},
    }


def body(op: Op, ids: dict[str, dict[str, int]]) -> dict[str, Any]:
    """The API request body for a create or update, with names resolved to server ids."""
    p = op.payload
    if op.kind == "database":
        return {"database_name": op.name, "sqlalchemy_uri": p["sqlalchemy_uri"], "expose_in_sqllab": True}
    if op.kind == "dataset":
        return {"database": ids["database"][p["database"]], "table_name": op.name, "sql": p["sql"]}
    if op.kind == "chart":
        ds = ids["dataset"][p["dataset"]]
        params = {**p["params"], "viz_type": p["viz_type"], "datasource": f"{ds}__table"}
        return {
            "slice_name": op.name,
            "viz_type": p["viz_type"],
            "datasource_id": ds,
            "datasource_type": "table",
            "params": json.dumps(params),
            "query_context": json.dumps(query_context(p["queries"], ds)),
        }
    return {"dashboard_title": op.name, "published": True, "position_json": json.dumps(position(p["grid"], ids["chart"], op.name))}


def load_specs(path: Path) -> dict[str, Any]:
    return {k: v for p in sorted(path.glob("*.yaml")) for k, v in yaml.safe_load(p.read_text()).items()}


# Edge: the network, the environment and the filesystem.


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.headers = {"Content-Type": "application/json"}

    def call(self, method: str, path: str, data: dict | None = None) -> dict[str, Any]:
        req = urllib.request.Request(self.base + path, method=method, headers=self.headers, data=None if data is None else json.dumps(data).encode())
        with self.opener.open(req, timeout=60) as resp:
            return json.load(resp)

    def login(self, username: str, password: str) -> None:
        token = self.call("POST", "/api/v1/security/login", {"username": username, "password": password, "provider": "db", "refresh": False})
        self.headers.update({"Authorization": f"Bearer {token['access_token']}", "Referer": self.base})
        self.headers["X-CSRFToken"] = self.call("GET", "/api/v1/security/csrf_token/")["result"]


def list_all(client: Client, kind: str) -> list[dict]:
    rows, page = [], 0
    while True:
        batch = client.call("GET", f"/api/v1/{kind}/?q=" + urllib.parse.quote(f"(page:{page},page_size:100)"))["result"]
        rows += batch
        page += 1
        if len(batch) < 100:
            return rows


def fetch_existing(client: Client, specs: dict[str, Any]) -> tuple[dict[str, dict[str, dict]], dict[str, dict[str, int]]]:
    """Existing objects the specs name, normalized, and every server id by kind and name."""
    ids = {k: {r[NAME_FIELD[k]]: r["id"] for r in list_all(client, k)} for k in KINDS}
    dataset_names = {i: n for n, i in ids["dataset"].items()}
    wanted = names_of(specs)
    existing = {k: {n: normalize(k, client.call("GET", detail_path(k, i))["result"], dataset_names) for n, i in ids[k].items() if n in wanted[k]} for k in KINDS}
    return existing, ids


def present_sources(runs: Path) -> frozenset[str]:
    found = {"sqlite": (runs / "cox.db").is_file(), "parquet-traces": any(runs.glob("traces/*/*/*/*.parquet"))}
    return frozenset(name for name, here in found.items() if here)


def apply(client: Client, ops: list[Op], ids: dict[str, dict[str, int]]) -> None:
    for op in ops:
        if op.action in ("create", "update"):
            data = body(op, ids)
            if op.action == "create":
                ids[op.kind][op.name] = client.call("POST", f"/api/v1/{op.kind}/", data)["id"]
            elif op.kind == "dataset":
                client.call("PUT", f"/api/v1/dataset/{ids['dataset'][op.name]}?override_columns=true", {k: v for k, v in data.items() if k != "database"})
            else:
                client.call("PUT", f"/api/v1/{op.kind}/{ids[op.kind][op.name]}", data)
            if op.kind == "dashboard":
                for cell in (c for row in op.payload["grid"] for c in row):
                    client.call("PUT", f"/api/v1/chart/{ids['chart'][cell['chart']]}", {"dashboards": [ids["dashboard"][op.name]]})
        print(line(op), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--specs", required=True, help="directory of *.yaml specs")
    ap.add_argument("--url", default=os.environ.get("SUPERSET_URL"), help="Superset base URL, default $SUPERSET_URL")
    ap.add_argument("--runs", default="/data/runs", help="the runs directory whose sources decide what is skipped")
    args = ap.parse_args(argv)
    user, password = os.environ.get("SUPERSET_ADMIN_USERNAME"), os.environ.get("SUPERSET_ADMIN_PASSWORD")
    if not (args.url and user and password):
        print("error: need --url (or SUPERSET_URL), SUPERSET_ADMIN_USERNAME and SUPERSET_ADMIN_PASSWORD", file=sys.stderr)
        return 2
    specs = load_specs(Path(args.specs))
    client = Client(args.url)
    try:
        client.login(user, password)
        existing, ids = fetch_existing(client, specs)
        apply(client, plan(specs, existing, present_sources(Path(args.runs))), ids)
    except urllib.error.HTTPError as e:
        print(f"error: {e.code} {e.url}: {e.read().decode(errors='replace')[:300]}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"error: {e.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
