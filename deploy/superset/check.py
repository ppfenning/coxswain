"""Confirm every Superset chart returns data: fetch each chart's data endpoint and print its row count.

Run it from the host with the .env values exported. It reads SUPERSET_ADMIN_USERNAME and
SUPERSET_ADMIN_PASSWORD, and SUPERSET_URL when set (default http://127.0.0.1:8088). It exits 1 if any chart errors.
The standard library only, so it needs no install. `report` is pure; everything else is the edge.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


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


# Edge: the network and the environment.


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
