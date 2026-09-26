"""Confirm the dataset plan is sound in both store modes, then that every Superset chart returns data.

First it plans the database and datasets offline, once with no store URL and once with a sentinel Postgres URL,
and exits 1 on any failure. Then it fetches each chart's data endpoint and prints its row count.

Run it from the host with the .env values exported. It reads SUPERSET_ADMIN_USERNAME and
SUPERSET_ADMIN_PASSWORD, and SUPERSET_URL when set (default http://127.0.0.1:8088). It exits 1 if any chart errors.
The offline plan reads bootstrap.py and the YAML specs, so it needs PyYAML; the chart check uses the standard library.
`report`, `mode_failures` and `plan_failures` are pure; everything else is the edge.
"""

from __future__ import annotations

import http.cookiejar
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
SENTINEL_PASSWORD = "sentinel-pw-7c1e"
SENTINEL_URL = f"postgresql://sentinel:{SENTINEL_PASSWORD}@sentinel-host:5432/sentinel"
MODES = (("default", None), ("postgres", SENTINEL_URL))


def message(answer: dict[str, Any]) -> str:
    return answer.get("message") or "; ".join(e.get("message", "") for e in answer.get("errors", [])) or "no result"


def verdict(name: str, answer: dict[str, Any]) -> tuple[str, bool]:
    """The chart's line and whether it returned data."""
    first = (answer.get("result") or [None])[0]
    if first is None:
        return f"{name}: ERROR {message(answer)}", False
    if first.get("error"):
        return f"{name}: ERROR {first['error']}", False
    return f"{name}: {len(first.get('data') or [])} rows", True


def report(answers: dict[str, dict[str, Any]]) -> tuple[list[str], int]:
    """One line per chart, in the order given, and the exit code: 1 if any chart errored, else 0."""
    verdicts = [verdict(n, a) for n, a in answers.items()]
    return [line for line, _ in verdicts], int(not all(ok for _, ok in verdicts))


def mode_failures(mode: str, store_url: str | None, specs: dict[str, Any], ops: list[Any]) -> list[str]:
    """One line per fault in a mode's planned ops: an error op, a placeholder left, the wrong store, or a leaked secret."""
    sources = {d["name"]: d["sql"] for d in specs["datasets"]}
    scan, catalog = "sqlite_scan('/data/runs/cox.db'", "store.public."
    store = "default" if store_url is None else "postgres"

    def dataset(op: Any) -> list[str]:
        sql = op.payload["sql"]
        wrong = (scan not in sql or catalog in sql) if store_url is None else (catalog not in sql or scan in sql)
        checks = (("placeholder left unexpanded", "{{" in sql), (f"does not scan the {store} store", "{{store:" in sources[op.name] and wrong))
        return [f"{mode} {op.name}: ERROR {reason}" for reason, bad in checks if bad]

    def leaks(op: Any) -> list[str]:
        saved = json.dumps(op.payload)
        secrets = (("store URL", SENTINEL_URL), ("store password", SENTINEL_PASSWORD))
        return [f"{mode} {op.name}: ERROR {label} appears in the {op.kind}" for label, secret in secrets if secret in saved]

    def faults(op: Any) -> list[str]:
        if op.action == "error":
            return [f"{mode} {op.name}: ERROR {op.reason}"]
        return [*(dataset(op) if op.kind == "dataset" and op.action != "skipped" else []), *(leaks(op) if store_url else [])]

    return [line for op in ops if op.kind in ("database", "dataset") for line in faults(op)]


def plan_failures(plan: Callable[..., list[Any]], specs: dict[str, Any]) -> tuple[list[str], int]:
    """Failure lines from planning every mode with nothing skipped, and the exit code: 1 if any, else 0."""
    present = {d["requires"] for d in specs["datasets"]}
    lines = [line for mode, url in MODES for line in mode_failures(mode, url, specs, plan(specs, {}, present, url))]
    return lines, int(bool(lines))


# Edge: the network, the filesystem and the environment.


def load_bootstrap() -> ModuleType:
    spec = importlib.util.spec_from_file_location("superset_bootstrap", HERE / "bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call(opener: urllib.request.OpenerDirector, base: str, headers: dict[str, str], method: str, path: str, data: dict | None = None) -> dict[str, Any]:
    req = urllib.request.Request(base + path, method=method, headers=headers, data=None if data is None else json.dumps(data).encode())
    with opener.open(req, timeout=120) as resp:
        return json.load(resp)


def fetch_answers(base: str, username: str, password: str) -> dict[str, dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    headers = {"Content-Type": "application/json"}
    token = call(opener, base, headers, "POST", "/api/v1/security/login", {"username": username, "password": password, "provider": "db", "refresh": False})
    headers["Authorization"] = f"Bearer {token['access_token']}"
    charts, page = [], 0
    while True:
        batch = call(opener, base, headers, "GET", "/api/v1/chart/?q=" + urllib.parse.quote(f"(page:{page},page_size:100)"))["result"]
        charts += batch
        page += 1
        if len(batch) < 100:
            break
    answers: dict[str, dict[str, Any]] = {}
    for chart in charts:
        try:
            answers[chart["slice_name"]] = call(opener, base, headers, "GET", f"/api/v1/chart/{chart['id']}/data/")
        except urllib.error.HTTPError as e:
            answers[chart["slice_name"]] = {"message": f"{e.code} {e.read().decode(errors='replace')[:200]}"}
    return answers


def main() -> int:
    base = os.environ.get("SUPERSET_URL", "http://127.0.0.1:8088").rstrip("/")
    user, password = os.environ.get("SUPERSET_ADMIN_USERNAME"), os.environ.get("SUPERSET_ADMIN_PASSWORD")
    bootstrap = load_bootstrap()
    failures, code = plan_failures(bootstrap.plan, bootstrap.load_specs(HERE / "dashboards"))
    if code:
        print("\n".join(failures), file=sys.stderr)
        return code
    print(f"plan: {' and '.join(m for m, _ in MODES)} modes ok")
    if not (user and password):
        print("error: export SUPERSET_ADMIN_USERNAME and SUPERSET_ADMIN_PASSWORD from .env", file=sys.stderr)
        return 2
    try:
        lines, code = report(fetch_answers(base, user, password))
    except (urllib.error.URLError, OSError) as e:
        print(f"error: {getattr(e, 'reason', e)}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
