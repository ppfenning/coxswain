from datetime import UTC, datetime, timedelta

from agent_tools import chair, chair_exec
from agent_tools.chair_exec import foreign_holder_gone

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
OWN = ("chair-loop", 42, "host-a")
REFUSAL = "chair: held by other (live)"


def _record(session: str = "other", pid: int = 7, host: str = "host-a", until: timedelta = timedelta(hours=1)) -> dict:
    return {"session": session, "pid": pid, "host": host, "heartbeat_at": (NOW - timedelta(days=1)).isoformat(), "until": (NOW + until).isoformat()}


def test_a_live_unexpired_foreign_holder_is_not_gone() -> None:
    assert foreign_holder_gone(_record(), OWN, True, NOW) is False


def test_a_dead_pid_on_this_host_is_gone() -> None:
    assert foreign_holder_gone(_record(), OWN, False, NOW) is True


def test_a_dead_pid_on_another_host_is_not_gone() -> None:
    assert foreign_holder_gone(_record(host="host-b"), OWN, False, NOW) is False


def test_a_passed_until_is_gone_even_with_a_live_pid() -> None:
    assert foreign_holder_gone(_record(until=-timedelta(hours=1)), OWN, True, NOW) is True


def test_no_record_and_the_loops_own_record_are_not_gone() -> None:
    assert foreign_holder_gone(None, OWN, False, NOW) is False
    assert foreign_holder_gone(_record(session=OWN[0], pid=OWN[1]), OWN, False, NOW) is False


def _fresh(until: timedelta = timedelta(hours=1), **changes) -> dict:
    now = datetime.now(UTC)
    return {**_record(), "heartbeat_at": (now - timedelta(days=1)).isoformat(), "until": (now + until).isoformat(), **changes}


def _deps(tmp_path, monkeypatch, acquire, alive: bool = True) -> chair_exec.Deps:
    monkeypatch.setattr(chair_exec.chair, "acquire_lease", acquire)
    monkeypatch.setattr(chair_exec.chair, "pid_alive", lambda pid: alive)
    return chair_exec.edge_deps(
        tmp_path, tmp_path, OWN[0], OWN[1], run_id=lambda a: "", repo_for=lambda a: "", record=lambda a: None,
        host=OWN[2], harness_python="/venv/bin/python",
    )


def _recording(tmp_path, monkeypatch, alive: bool = True) -> tuple[chair_exec.Deps, list]:
    calls: list = []
    deps = _deps(tmp_path, monkeypatch, lambda runs_dir, session, pid, host, steal=False: calls.append(steal) or "", alive)
    return deps, calls


def test_a_live_unexpired_foreign_record_is_refused_without_calling_the_store(tmp_path, monkeypatch) -> None:
    deps, calls = _recording(tmp_path, monkeypatch)
    chair.write(tmp_path, _fresh())
    assert deps.acquire_lease("h", "host-a") == REFUSAL
    assert calls == []


def test_a_dead_foreign_pid_is_taken_with_steal(tmp_path, monkeypatch) -> None:
    deps, calls = _recording(tmp_path, monkeypatch, alive=False)
    chair.write(tmp_path, _fresh())
    assert deps.acquire_lease("h", "host-a") == ""
    assert calls == [True]


def test_a_passed_until_is_taken_with_steal(tmp_path, monkeypatch) -> None:
    deps, calls = _recording(tmp_path, monkeypatch)
    chair.write(tmp_path, _fresh(until=-timedelta(hours=1), heartbeat_at=datetime.now(UTC).isoformat()))
    assert deps.acquire_lease("h", "host-a") == ""
    assert calls == [True]


def test_no_record_acquires_without_steal(tmp_path, monkeypatch) -> None:
    deps, calls = _recording(tmp_path, monkeypatch)
    assert deps.acquire_lease("h", "host-a") == ""
    assert calls == [False]


def test_an_unreadable_record_acquires_without_steal(tmp_path, monkeypatch) -> None:
    deps, calls = _recording(tmp_path, monkeypatch)
    (tmp_path / chair.CHAIR_FILENAME).write_text("{not json", encoding="utf-8")
    assert deps.acquire_lease("h", "host-a") == ""
    assert calls == [False]


def test_the_loops_own_record_acquires_without_steal(tmp_path, monkeypatch) -> None:
    deps, calls = _recording(tmp_path, monkeypatch)
    chair.write(tmp_path, _fresh(session=OWN[0], pid=OWN[1]))
    assert deps.acquire_lease("h", "host-a") == ""
    assert calls == [False]


class _LiveRowStore:
    """A store whose row is held live by another holder: a plain acquire is refused, a steal is granted."""

    def __init__(self) -> None:
        self.steals: list[bool] = []

    def acquire(self, runs_dir, session, pid, host, steal=False) -> str:
        self.steals.append(steal)
        return "" if steal else "lease: held by other holder"


def test_a_stale_session_record_over_a_live_store_row_is_taken_end_to_end(tmp_path, monkeypatch) -> None:
    store = _LiveRowStore()
    deps = _deps(tmp_path, monkeypatch, store.acquire, alive=False)
    assert store.acquire(tmp_path, "s", 1, "host-a") == "lease: held by other holder"
    store.steals.clear()
    chair.write(tmp_path, _fresh())
    assert deps.acquire_lease("h", "host-a") == ""
    assert store.steals == [True]


def _take(reason: str = "") -> dict:
    return {"kind": "take_lease", "holder": "h", "host": "host-a", "reason": reason}


class _FakeLease:
    def __init__(self, line: str = "") -> None:
        self.line, self.calls = line, []

    def acquire(self, holder, host, **kwargs) -> str:
        self.calls.append(kwargs)
        return self.line


def _exec_deps(fake: _FakeLease):
    return chair_exec.Deps(run=lambda *a, **k: (0, ""), delete_branches=lambda r, p: ([], ""), acquire_lease=fake.acquire,
                           record=lambda a: None, run_id=lambda a: "", repo_for=lambda a: "")


def test_a_takeback_reason_acquires_with_steal_and_a_plain_take_does_not() -> None:
    takeback, plain = _FakeLease(), _FakeLease()
    result = chair_exec._lease(_take("takeback from other@host-a:7"), _exec_deps(takeback))
    chair_exec._lease(_take(), _exec_deps(plain))
    assert takeback.calls[0]["steal"] is True
    assert "steal" not in plain.calls[0]
    assert result["status"] == "done" and result["action"]["reason"] == "takeback from other@host-a:7"


def test_a_refused_takeback_stays_refused_with_the_refusal_line() -> None:
    result = chair_exec._lease(_take("takeback from other@host-a:7"), _exec_deps(_FakeLease(REFUSAL)))
    assert (result["status"], result["reason"]) == ("refused", REFUSAL)
