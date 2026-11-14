from datetime import UTC, datetime, timedelta

from agent_tools import chair, chair_exec
from agent_tools.chair_exec import lease_guard

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
OWN = ("chair-loop", 42, "host-a")


def _record(
    session: str = "other", pid: int = 7, host: str = "host-a", until: timedelta | None = timedelta(hours=1), beat_age: timedelta = timedelta(days=1),
) -> dict:
    base = {"session": session, "pid": pid, "host": host, "heartbeat_at": (NOW - beat_age).isoformat()}
    return base if until is None else {**base, "until": (NOW + until).isoformat()}


def test_a_live_foreign_record_with_an_old_heartbeat_and_a_future_until_is_refused() -> None:
    assert lease_guard(_record(), OWN, True, NOW, "host-a") == "chair: held by other (live)"


def test_the_loops_own_record_is_not_refused() -> None:
    assert lease_guard(_record(session="chair-loop", pid=42), OWN, True, NOW, "host-a") == ""


def test_a_dead_pid_is_not_refused() -> None:
    assert lease_guard(_record(), OWN, False, NOW, "host-a") == ""


def test_a_passed_until_with_an_old_heartbeat_is_not_refused() -> None:
    assert lease_guard(_record(until=-timedelta(hours=1)), OWN, True, NOW, "host-a") == ""


def test_a_passed_until_with_a_fresh_heartbeat_is_not_refused_though_liveness_reads_live() -> None:
    lapsed = _record(until=-timedelta(hours=1), beat_age=timedelta(minutes=1))
    assert chair.liveness(lapsed, True, NOW, "host-a") == "live"
    assert lease_guard(lapsed, OWN, True, NOW, "host-a") == ""


def test_a_missing_record_is_not_refused() -> None:
    assert lease_guard(None, OWN, True, NOW, "host-a") == ""


def _deps(tmp_path, monkeypatch, alive: bool = True) -> tuple[chair_exec.Deps, list]:
    calls: list = []
    monkeypatch.setattr(chair_exec.chair, "acquire_lease", lambda runs_dir, session, pid, host, steal=False: calls.append(steal) or "")
    monkeypatch.setattr(chair_exec.chair, "pid_alive", lambda pid: alive)
    deps = chair_exec.edge_deps(
        tmp_path, tmp_path, OWN[0], OWN[1], run_id=lambda a: "", repo_for=lambda a: "", record=lambda a: None,
        host=OWN[2], harness_python="/venv/bin/python",
    )
    return deps, calls


def _fresh(**changes) -> dict:
    now = datetime.now(UTC)
    return {**_record(), "heartbeat_at": (now - timedelta(days=1)).isoformat(), "until": (now + timedelta(hours=1)).isoformat(), **changes}


def test_the_closure_refuses_a_live_foreign_session_and_does_not_acquire(tmp_path, monkeypatch) -> None:
    deps, calls = _deps(tmp_path, monkeypatch)
    chair.write(tmp_path, _fresh())
    assert deps.acquire_lease("h", "host-a") == "chair: held by other (live)"
    assert calls == []


def test_the_closure_acquires_with_no_record(tmp_path, monkeypatch) -> None:
    deps, calls = _deps(tmp_path, monkeypatch)
    assert deps.acquire_lease("h", "host-a") == ""
    assert calls == [False]


def test_the_closure_acquires_over_an_unreadable_record(tmp_path, monkeypatch) -> None:
    deps, calls = _deps(tmp_path, monkeypatch)
    (tmp_path / chair.CHAIR_FILENAME).write_text("{not json", encoding="utf-8")
    assert deps.acquire_lease("h", "host-a") == ""
    assert calls == [False]


def test_the_closure_acquires_over_the_loops_own_record(tmp_path, monkeypatch) -> None:
    deps, calls = _deps(tmp_path, monkeypatch)
    chair.write(tmp_path, _fresh(session=OWN[0], pid=OWN[1]))
    assert deps.acquire_lease("h", "host-a") == ""
    assert calls == [False]


def test_the_closure_steals_over_a_dead_foreign_pid(tmp_path, monkeypatch) -> None:
    deps, calls = _deps(tmp_path, monkeypatch, alive=False)
    chair.write(tmp_path, _fresh())
    assert deps.acquire_lease("h", "host-a") == ""
    assert calls == [True]


def test_the_closure_steals_over_a_live_but_lapsed_foreign_record(tmp_path, monkeypatch) -> None:
    deps, calls = _deps(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    chair.write(tmp_path, _fresh(heartbeat_at=now.isoformat(), until=(now - timedelta(hours=1)).isoformat()))
    assert deps.acquire_lease("h", "host-a", steal=True) == ""
    assert calls == [True]


def test_the_closure_refuses_a_steal_over_a_live_unexpired_foreign_record(tmp_path, monkeypatch) -> None:
    deps, calls = _deps(tmp_path, monkeypatch)
    chair.write(tmp_path, _fresh())
    assert deps.acquire_lease("h", "host-a", steal=True) == "chair: held by other (live)"
    assert calls == []


def test_the_closure_acquires_a_takeback_over_a_live_unexpired_foreign_record(tmp_path, monkeypatch) -> None:
    deps, calls = _deps(tmp_path, monkeypatch)
    chair.write(tmp_path, _fresh())
    assert deps.acquire_lease("h", "host-a", steal=True, takeback=True) == ""
    assert calls == [True]
