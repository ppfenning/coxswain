import io

from devtools.install_proof import check_doctor, check_version, main, previous_tag

_PASSING = (
    '{"rows": [{"check": "git", "ok": true, "detail": "git version 2.56.0"}, '
    '{"check": "profile", "ok": true, "detail": "<profile path>"}, '
    '{"check": "store", "ok": true, "detail": "postgresql, 2200 runs"}, '
    '{"check": "cast", "ok": true, "detail": "not gathered"}], "ok": true, '
    '"parquet_traces": "parquet traces: readable via http://host:3900"}'
)

_NO_PROFILE = (
    '{"rows": [{"check": "git", "ok": true, "detail": "git version 2.56.0"}, '
    '{"check": "forge", "ok": true, "detail": "local: plain git, no pull-request host"}, '
    '{"check": "profile", "ok": false, "detail": "missing: <temp dir>/profile.yaml"}, '
    '{"check": "profile paths", "ok": false, "detail": "skipped: no profile"}, '
    '{"check": "plugins", "ok": true, "detail": "sources: none; forges: none; trackers: none; '
    'system_one: not checked; runners: not checked"}, '
    '{"check": "store", "ok": false, "detail": "skipped: no profile"}], "ok": false, "parquet_traces": null}'
)


def test_previous_tag_sorts_numerically_and_keeps_the_spelling():
    assert previous_tag(["v0.9.0", "v0.10.0", "v0.11.0"], "0.11.0") == "v0.10.0"


def test_previous_tag_of_the_first_calver_release_is_the_last_semver_one():
    tags = ["v0.46.0", "v0.47.0", "v2026.10.0", "v2026.10.9", "v2026.10.10"]
    assert previous_tag(tags, "2026.10.0") == "v0.47.0"
    assert previous_tag(tags, "2026.11.0") == "v2026.10.10"


def test_check_version_matches_a_calver_version_line():
    assert check_version("cox 2026.10.0\n", "2026.10.0") is None
    assert check_version("cox 2026.10.10\n", "2026.10.1") is not None


def test_previous_tag_is_none_without_a_lower_tag():
    assert previous_tag(["v0.11.0", "nightly", "v0.12.0"], "0.11.0") is None


def test_check_version_matches_the_version_line():
    assert check_version("towpath 0.34.0", "0.34.0") is None


def test_check_version_matches_a_later_line_after_the_alias_notice():
    out = "coxtop is now towpath; this alias goes away in 0.37\ntowpath 0.34.0\n"
    assert check_version(out, "0.34.0") is None


def test_check_version_refuses_a_longer_version_than_expected():
    assert "0.27" in check_version("cox 0.27.1", "0.27")


def test_check_version_refuses_a_shorter_version_than_expected():
    assert "0.27.1" in check_version("cox 0.27", "0.27.1")


def test_check_doctor_passes_the_passing_run():
    assert check_doctor(_PASSING) is None


def test_check_doctor_passes_a_fresh_install_whose_only_failures_await_setup():
    assert check_doctor(_NO_PROFILE) is None


def test_check_doctor_fails_a_setup_independent_row_on_a_fresh_install():
    rows = (
        '{"rows": [{"check": "git", "ok": false, "detail": "git not found"}, '
        '{"check": "profile", "ok": false, "detail": "missing: <temp dir>/profile.yaml"}, '
        '{"check": "store", "ok": false, "detail": "skipped: no profile"}], "ok": false}'
    )
    assert check_doctor(rows) == "doctor: git failed: git not found"


def test_check_doctor_fails_when_no_row_passes():
    rows = '{"rows": [{"check": "profile", "ok": false, "detail": "missing: x"}], "ok": false}'
    assert check_doctor(rows) == "doctor: no row passed"


def test_check_doctor_fails_a_failing_row_under_a_true_top_level():
    assert "store" in check_doctor('{"rows": [{"check": "store", "ok": false, "detail": "venv missing"}], "ok": true}')


def test_check_doctor_fails_empty_rows():
    assert check_doctor('{"rows": [], "ok": true}') == "doctor: no rows"


def test_check_doctor_fails_invalid_json():
    assert check_doctor("not json").startswith("doctor: invalid JSON")


def test_main_returns_1_and_writes_stderr_for_a_failing_doctor(monkeypatch, capsys):
    failing = '{"rows": [{"check": "git", "ok": false, "detail": "git not found"}], "ok": false}'
    monkeypatch.setattr("sys.stdin", io.StringIO(failing))
    assert main(["check-doctor"]) == 1
    assert "git" in capsys.readouterr().err


def test_towpath_version_reads_the_dash_release_the_formula_downloads_from():
    from devtools.install_proof import towpath_version
    formula = ('  resource "towpath" do\n    url "https://github.com/ppfenning/coxswain-dash/releases/download/v0.38.0/'
               'towpath-v0.38.0-aarch64-apple-darwin.tar.gz"\n  end\n')
    assert towpath_version(formula, "0.39.0") == "0.38.0"
    assert towpath_version('  resource "towpath" do\n    url "u"\n  end\n', "0.39.0") == "0.39.0"
