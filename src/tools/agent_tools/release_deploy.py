"""Pure per-host release deploy: every process call goes through the injected `run`, which maps an argv to (exit code, output)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent_tools.dash_binary import installed_version
from agent_tools.events import Event
from agent_tools.remote_argv import doctor_argv

__all__ = ["HostResult", "deploy_events", "deploy_host", "result_line"]

Run = Callable[[Sequence[str]], tuple[int, str]]
Failure = tuple[str, str]  # (failing step, detail)


@dataclass(frozen=True)
class HostResult:
    host: str
    version: str
    ok: bool
    failing_step: str  # upgrade, version_cox, version_towpath, doctor, refresh, or none
    detail: str


def _upgrade_argv(version: str) -> list[str]:
    return ["cox", "upgrade", "--to", version]


# Wrong belief: "a bare pull refreshes main". It pulls whatever branch the checkout is on, so main is checked out first.
# The edge decides where these run (the checkout directory); this module only fixes their order and spelling.
def _refresh_argvs() -> list[list[str]]:
    return [["git", "checkout", "main"], ["git", "pull", "--ff-only", "origin", "main"]]


_VERSION_CHECKS: tuple[tuple[str, list[str]], ...] = (
    ("version_cox", ["cox", "--version"]),
    ("version_towpath", ["towpath", "--version"]),
)


def _bare(version: str) -> str:
    return version.removeprefix("v")


def _step(run: Run, argv: list[str], step: str) -> Failure | None:
    code, out = run(argv)
    return None if code == 0 else (step, f"exit {code}: {out.strip()}")


def _check_versions(run: Run, version: str) -> Failure | None:
    """The last token of each binary's version output must equal `version`; `2026.11.60` and `2026.11.6+abc` do not."""
    for step, argv in _VERSION_CHECKS:
        code, out = run(argv)
        if code != 0 or installed_version(out) != _bare(version):
            return step, f"exit {code}: {out.strip()}"
    return None


def _upgrade_and_verify(run: Run, version: str) -> Failure | None:
    return _step(run, _upgrade_argv(version), "upgrade") or _check_versions(run, version)


def _upgrade_with_retry(run: Run, version: str) -> Failure | None:
    """One more upgrade and check after a version mismatch. A failed upgrade is not retried."""
    first = _upgrade_and_verify(run, version)
    return _upgrade_and_verify(run, version) if first is not None and first[0] != "upgrade" else first


def _refresh(run: Run) -> Failure | None:
    return next(filter(None, (_step(run, argv, "refresh") for argv in _refresh_argvs())), None)


def deploy_host(host: str, version: str, run: Run, development: bool) -> HostResult:
    """A failed step stops the sequence. A development host refreshes main and skips the version match and retry."""
    prepared = _refresh(run) if development else _upgrade_with_retry(run, version)
    failure = prepared or _step(run, doctor_argv(), "doctor")
    if failure is None:
        return HostResult(host, version, True, "none", "")
    return HostResult(host, version, False, failure[0], failure[1])


def result_line(result: HostResult) -> str:
    status = "ok" if result.ok else f"failed at {result.failing_step}"
    return f"{result.host} {result.version} {status}"


def deploy_events(version: str, results: Sequence[HostResult]) -> list[Event]:
    """One event per host in order, then `released` only when every result is ok. No results means no `released`."""
    run = f"release-{version}"
    per_host = [
        Event(run, "deployed", i, {"host": r.host})
        if r.ok
        else Event(run, "deploy_failed", i, {"host": r.host, "step": r.failing_step})
        for i, r in enumerate(results)
    ]
    released = (
        [Event(run, "released", len(per_host), {"version": version})] if results and all(r.ok for r in results) else []
    )
    return per_host + released
