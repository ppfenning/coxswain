import importlib.util
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load("superset_check", DEPLOY / "check.py")
bootstrap = check.load_bootstrap()
SPECS = {"datasets": [{"name": "d", "sql": "select * from {{store:runs}}", "requires": "sqlite"}]}
URL = check.SENTINEL_URL


def _ops(sql, uri="duckdb:///:memory:", action="create"):
    return [
        bootstrap.Op("create", "database", "db", {"sqlalchemy_uri": uri}),
        bootstrap.Op(action, "dataset", "d", {"database": "db", "sql": sql}),
    ]


def test_clean_ops_pass_in_both_modes():
    assert check.mode_failures("default", None, SPECS, _ops("select * from sqlite_scan('/data/runs/cox.db', 'runs')")) == []
    assert check.mode_failures("postgres", URL, SPECS, _ops("select * from store.public.runs")) == []


def test_an_unexpanded_placeholder_is_flagged():
    assert check.mode_failures("default", None, SPECS, _ops("select * from {{store:runs}}"))[0] == "default d: ERROR placeholder left unexpanded"


def test_postgres_mode_flags_a_sqlite_scan():
    sql = "select * from sqlite_scan('/data/runs/cox.db', 'runs')"
    assert check.mode_failures("postgres", URL, SPECS, _ops(sql)) == ["postgres d: ERROR does not scan the postgres store"]


def test_default_mode_flags_the_attached_catalog():
    assert check.mode_failures("default", None, SPECS, _ops("select * from store.public.runs")) == ["default d: ERROR does not scan the default store"]


def test_the_sentinel_url_in_dataset_sql_is_flagged():
    sql = f"select * from store.public.runs -- {URL}"
    assert check.mode_failures("postgres", URL, SPECS, _ops(sql)) == ["postgres d: ERROR store URL appears in the dataset", "postgres d: ERROR store password appears in the dataset"]


def test_the_sentinel_password_in_the_database_is_flagged():
    ops = _ops("select * from store.public.runs", uri=f"duckdb:///:memory:?pw={check.SENTINEL_PASSWORD}")
    assert check.mode_failures("postgres", URL, SPECS, ops) == ["postgres db: ERROR store password appears in the database"]


def test_an_error_op_is_reported_with_its_reason():
    ops = [bootstrap.Op("error", "dataset", "d", {}, "unknown store placeholder {{store}}")]
    assert check.mode_failures("default", None, SPECS, ops) == ["default d: ERROR unknown store placeholder {{store}}"]


def test_the_real_specs_pass_both_modes():
    specs = bootstrap.load_specs(DEPLOY / "dashboards")
    assert check.plan_failures(bootstrap.plan, specs) == ([], 0)


def test_the_real_postgres_plan_keeps_the_sentinel_out_of_every_saved_field():
    specs = bootstrap.load_specs(DEPLOY / "dashboards")
    ops = bootstrap.plan(specs, {}, {d["requires"] for d in specs["datasets"]}, URL)
    saved = repr([(op.payload, bootstrap.body(op, {}) if op.kind == "database" else None) for op in ops])
    assert check.SENTINEL_PASSWORD not in saved and URL not in saved


def test_a_failing_plan_exits_nonzero():
    lines, code = check.plan_failures(lambda specs, existing, present, url: _ops("select * from {{store:runs}}"), SPECS)
    assert code == 1 and lines
