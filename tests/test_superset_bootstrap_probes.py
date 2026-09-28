import importlib.util
import json
import urllib.error
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = _load("superset_bootstrap_probes", DEPLOY / "bootstrap.py")


class _FakeClient:
    def __init__(self, respond):
        self._respond = respond

    def call(self, method, path, data=None):
        return self._respond(method, path, data)


class _RaisingHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body):
        import io

        super().__init__("http://x/api/v1/sqllab/execute/", code, "err", None, io.BytesIO(json.dumps(body).encode()))

    def read(self):
        return self.fp.read()


def test_every_probe_sql_ends_in_where_false_not_limit_0():
    for probe in bootstrap.CHAIR_PROBES + bootstrap.HOSTS_PROBES:
        assert probe.endswith("WHERE false")
        assert "LIMIT 0" not in probe


def test_a_500_response_whose_body_has_errors_reads_as_absent():
    def respond(method, path, data):
        raise _RaisingHTTPError(500, {"errors": [{"message": "syntax error"}]})

    assert bootstrap._probes_readable(_FakeClient(respond), 1, None, bootstrap.HOSTS_PROBES) is False


def test_a_400_response_whose_body_has_errors_reads_as_absent():
    def respond(method, path, data):
        raise _RaisingHTTPError(400, {"errors": [{"message": "relation does not exist"}]})

    assert bootstrap._probes_readable(_FakeClient(respond), 1, None, bootstrap.HOSTS_PROBES) is False


def test_a_200_response_with_no_errors_reads_as_present():
    def respond(method, path, data):
        return {"result": {"data": []}}

    assert bootstrap._probes_readable(_FakeClient(respond), 1, None, bootstrap.HOSTS_PROBES) is True


def test_a_401_response_raises():
    def respond(method, path, data):
        raise _RaisingHTTPError(401, {"errors": [{"message": "unauthorized"}]})

    try:
        bootstrap._probes_readable(_FakeClient(respond), 1, None, bootstrap.HOSTS_PROBES)
    except urllib.error.HTTPError as e:
        assert e.code == 401
    else:
        raise AssertionError("expected HTTPError to propagate")


def test_a_403_response_raises():
    def respond(method, path, data):
        raise _RaisingHTTPError(403, {"errors": [{"message": "forbidden"}]})

    try:
        bootstrap._probes_readable(_FakeClient(respond), 1, None, bootstrap.HOSTS_PROBES)
    except urllib.error.HTTPError as e:
        assert e.code == 403
    else:
        raise AssertionError("expected HTTPError to propagate")


def test_a_500_response_without_errors_propagates_with_its_body_still_readable():
    def respond(method, path, data):
        raise _RaisingHTTPError(500, {"message": "internal server error"})

    try:
        bootstrap._probes_readable(_FakeClient(respond), 1, None, bootstrap.HOSTS_PROBES)
    except urllib.error.HTTPError as e:
        assert (e.code, json.loads(e.read())) == (500, {"message": "internal server error"})
    else:
        raise AssertionError("expected HTTPError to propagate")


def test_a_body_that_is_not_json_is_not_a_query_error():
    assert bootstrap.query_error(b"<html>502 Bad Gateway</html>") is False


def test_a_callable_that_raises_a_network_error_propagates():
    def respond(method, path, data):
        raise OSError("connection refused")

    try:
        bootstrap._probes_readable(_FakeClient(respond), 1, None, bootstrap.HOSTS_PROBES)
    except OSError as e:
        assert str(e) == "connection refused"
    else:
        raise AssertionError("expected OSError to propagate")
