"""Pure login-watch rules: which active hosts are due for a `check_login`, which are blocked on a
lapsed login, the `needs_chair` line that names them, and the version-merge that records a check's
result. No I/O, no ssh, no store call: everything here operates on the row shape `run_store.hosts`
returns and `chair_read_lost.stale_hosts` already reads: `name`, `ssh`, `capacity`, `state` (active,
draining, offline), `beat_at`, `versions_json` (cox, graphs, cartridges, claude versions, `login_ok`,
and now `login_checked_at`), `updated_at`, `updated_by`. The edge that calls these against the real
store, performs the ssh check, and writes the result back is a separate module. `versions_json` is a
JSON string on SQLite and a mapping on Postgres; `_versions` below decodes both, independently of
`host_cmd`'s own private helper of the same purpose, so this module carries no dependency on it.
"""
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

Row = Mapping[str, Any]

DEFAULT_THRESHOLD_S = 1800
# An idle host is beaten only by the login check, so placement must tolerate two missed check intervals.
PLACEMENT_STALE_S = 2 * DEFAULT_THRESHOLD_S

# A host whose ssh check is unreachable UNREACHABLE_AFTER times in a row enters UNREACHABLE_STATE, and is
# then probed every PROBE_INTERVAL_S with one ssh connect bounded by PROBE_CONNECT_TIMEOUT_S.
UNREACHABLE_STATE = "unreachable"
UNREACHABLE_AFTER = 2
PROBE_INTERVAL_S = 1800
PROBE_CONNECT_TIMEOUT_S = 5
BACK_WINDOW_S = 3600

UNREACHABLE_FAILURES_KEY = "unreachable_failures"  # int count of consecutive unreachable checks
UNREACHABLE_SINCE_KEY = "unreachable_since"  # ISO UTC, set when the state is entered
PROBE_CHECKED_AT_KEY = "probe_checked_at"  # ISO UTC of the last probe
HOST_BACK_AT_KEY = "host_back_at"  # ISO UTC of the last return, written by the store on a beat and by the probe

_UNREACHABLE_CODES = (124, 255)
_UNREACHABLE_PHRASES = (
    "timed out",
    "connection refused",
    "no route to host",
    "network is unreachable",
    "could not resolve hostname",
)

_HTTPS_URL = re.compile(r"https://[^\s<>\"']+")


def login_url_from_output(output: str) -> str | None:
    """The first https URL in `output` (Tailscale SSH check mode prints its re-auth link there), minus
    trailing sentence punctuation; None when the text holds none."""
    found = _HTTPS_URL.search(output)
    return found.group().rstrip(".,;:)]") if found else None


def _versions(row: Row) -> dict:
    """`versions_json` as a dict: a JSON string from SQLite, already a mapping from a Postgres JSONB
    column."""
    raw = row.get("versions_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parsed(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def due_for_login_check(hosts: Sequence[Row], now: str, threshold_s: int = DEFAULT_THRESHOLD_S) -> list[str]:
    """Names of `active` rows whose `versions_json` has no `login_checked_at`, or one more than
    `threshold_s` seconds before `now`."""
    now_parsed = _parsed(now) or datetime.min.replace(tzinfo=UTC)
    return [
        str(row.get("name") or "")
        for row in hosts
        if row.get("state") == "active" and _checked_long_ago(_versions(row).get("login_checked_at"), now_parsed, threshold_s)
    ]


def _checked_long_ago(checked_at: object, now: datetime, threshold_s: int) -> bool:
    """True when `checked_at` is blank, unparsable, or more than `threshold_s` seconds before `now`."""
    parsed = _parsed(checked_at) if checked_at else None
    return parsed is None or (now - parsed).total_seconds() > threshold_s


def login_blocked(hosts: Sequence[Row]) -> set[str]:
    """Names of `active` rows whose `versions_json` `login_ok` is `False` exactly. Missing or `None`
    is not blocked: a host never checked has not failed."""
    return {
        str(row.get("name") or "")
        for row in hosts
        if row.get("state") == "active" and _versions(row).get("login_ok") is False
    }


def login_needs_chair(hosts: Sequence[Row]) -> list[dict]:
    """One `{"kind": "needs_chair", "host": name, "cause": "login_lapsed"}` action per name in
    `login_blocked(hosts)`, never more than one per host. The action gains a `url` key when the host's
    `versions_json` holds a `login_url`, and has no such key otherwise."""
    blocked = login_blocked(hosts)
    urls = {
        str(row.get("name") or ""): _versions(row).get("login_url")
        for row in hosts
        if str(row.get("name") or "") in blocked
    }
    return [
        {"kind": "needs_chair", "host": name, "cause": "login_lapsed", **({"url": urls[name]} if urls.get(name) else {})}
        for name in sorted(blocked)
    ]


def is_unreachable_reply(returncode: int, output: str) -> bool:
    """True only for exit 124 or 255 with an unreachable phrase in `output`, case-insensitive."""
    lowered = output.lower()
    return returncode in _UNREACHABLE_CODES and any(phrase in lowered for phrase in _UNREACHABLE_PHRASES)


def due_for_probe(hosts: Sequence[Row], now: str, interval_s: int = PROBE_INTERVAL_S) -> list[str]:
    """Names of `unreachable` rows whose `probe_checked_at` is missing, unparsable, or more than
    `interval_s` seconds before `now`."""
    now_parsed = _parsed(now) or datetime.min.replace(tzinfo=UTC)
    return [
        str(row.get("name") or "")
        for row in hosts
        if row.get("state") == UNREACHABLE_STATE
        and _checked_long_ago(_versions(row).get(PROBE_CHECKED_AT_KEY), now_parsed, interval_s)
    ]


def host_unreachable_needs_chair(hosts: Sequence[Row]) -> list[dict]:
    """One `host_unreachable` needs_chair action per `unreachable` row, sorted by name. `since` is
    omitted when the row holds no `unreachable_since`."""
    rows = sorted((row for row in hosts if row.get("state") == UNREACHABLE_STATE), key=lambda row: str(row.get("name") or ""))
    return [
        {
            "kind": "needs_chair",
            "host": str(row.get("name") or ""),
            "cause": "host_unreachable",
            **({"since": since} if (since := _versions(row).get(UNREACHABLE_SINCE_KEY)) else {}),
        }
        for row in rows
    ]


def host_back_needs_chair(hosts: Sequence[Row], now: str) -> list[dict]:
    """One `host_back` needs_chair action per `active` row whose `host_back_at` parses and lies within
    `BACK_WINDOW_S` seconds before `now`, sorted by name. A `host_back_at` after `now` is not within."""
    now_parsed = _parsed(now) or datetime.min.replace(tzinfo=UTC)
    returned = [
        (str(row.get("name") or ""), back_at)
        for row in hosts
        if row.get("state") == "active"
        for back_at in [_versions(row).get(HOST_BACK_AT_KEY)]
        if back_at and (parsed := _parsed(back_at)) is not None and 0 <= (now_parsed - parsed).total_seconds() <= BACK_WINDOW_S
    ]
    return [{"kind": "needs_chair", "host": name, "cause": "host_back", "back_at": back_at} for name, back_at in sorted(returned)]


def merge_login_versions(
    versions: Mapping[str, Any],
    login_ok: bool,
    checked_at: str,
    check: str = "claude_auth",
    reason: str | None = None,
    *,
    login_url: str | None = None,
) -> dict:
    """A new dict carrying every key of `versions` plus `login_ok`, `login_checked_at` and `check` set to
    the given values. `reason` and `login_url` are added only when not `None`; a `None` is dropped, never
    stored as a null. A stored `login_url` is dropped when the new one is `None`, so a passing check
    clears it. `versions` itself is never mutated."""
    kept = {key: value for key, value in versions.items() if key != "login_url"}
    merged = {**kept, "login_ok": login_ok, "login_checked_at": checked_at, "check": check}
    with_reason = merged if reason is None else {**merged, "reason": reason}
    return with_reason if login_url is None else {**with_reason, "login_url": login_url}
