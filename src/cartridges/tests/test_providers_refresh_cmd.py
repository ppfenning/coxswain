import io
from pathlib import Path

from core import cartridge, providers_refresh
from core.providers_refresh import render_report, run
from core.refresh import refresh_report

CATALOG = """\
models:
  - id: claude-opus-5
    aliases: [opus]
    provider: anthropic
    price: {input: 4.0, output: 20.0, cache_write: 5.0, cache_read: 0.4}
    effort: [low, high]
    classes: [reason]
    context_window: null
"""


def _page(*ids: str) -> dict:
    return {"data": [{"id": i} for i in ids], "has_more": False}


def _invoke(catalog: Path, get, argv=()):
    out, err = io.StringIO(), io.StringIO()
    code = run(argv, catalog_path=catalog, api_key="k", get=get, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _catalog(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(CATALOG, encoding="utf-8")
    return path


def test_render_report_names_the_newer_id_and_an_unpriced_row_stub():
    text = render_report(refresh_report(["claude-opus-5"], ["claude-opus-5-5"]))
    assert "newer in opus:" in text
    assert "  - id: claude-opus-5-5" in text
    assert "price: {input: unset, output: unset, cache_write: unset, cache_read: unset}" in text


def test_render_report_says_so_when_nothing_is_newer():
    assert render_report(refresh_report(["claude-opus-5"], ["claude-opus-5"])) == (
        "no vendor model is newer than the catalog"
    )


def test_render_report_lists_unknown_ids():
    text = render_report(refresh_report(["claude-opus-5"], ["claude-fable-9"]))
    assert "  - claude-fable-9" in text


def test_the_command_prints_the_newer_id_and_its_stub_and_exits_zero(tmp_path):
    code, out, err = _invoke(_catalog(tmp_path), lambda url, headers, params: _page("claude-opus-5-5", "claude-opus-5"))
    assert (code, err) == (0, "")
    assert "  - id: claude-opus-5-5" in out
    assert "  - id: claude-opus-5\n" not in out


def test_the_catalog_file_bytes_are_unchanged_after_the_run(tmp_path):
    path = _catalog(tmp_path)
    before = path.read_bytes()
    _invoke(path, lambda url, headers, params: _page("claude-opus-5-5"))
    assert path.read_bytes() == before


def test_a_raising_client_prints_the_reason_and_no_report(tmp_path):
    def boom(url, headers, params):
        raise OSError("down")

    code, out, err = _invoke(_catalog(tmp_path), boom)
    assert code == 1
    assert out == ""
    assert "anthropic models request failed: OSError" in err


def test_an_error_body_prints_the_reason_and_no_report(tmp_path):
    body = {"type": "error", "error": {"type": "authentication_error"}}
    code, out, err = _invoke(_catalog(tmp_path), lambda url, headers, params: body)
    assert (code, out) == (1, "")
    assert "authentication_error" in err


def test_an_invalid_catalog_exits_nonzero_with_the_errors_and_never_calls_the_vendor(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("models: []\n", encoding="utf-8")
    calls = []
    code, out, err = _invoke(bad, lambda url, headers, params: calls.append(url) or _page())
    assert (code, out, calls) == (2, "", [])
    assert "non-empty 'models' list" in err


def test_a_missing_catalog_file_exits_nonzero(tmp_path):
    code, out, err = _invoke(tmp_path / "absent.yaml", lambda url, headers, params: _page())
    assert (code, out) == (2, "")
    assert "cannot read" in err


def test_the_catalog_flag_overrides_the_default_path(tmp_path):
    other = tmp_path / "other.yaml"
    other.write_text(CATALOG, encoding="utf-8")
    code, out, _ = _invoke(tmp_path / "absent.yaml", lambda url, headers, params: _page("claude-opus-5-5"), ["--catalog", str(other)])
    assert code == 0
    assert "claude-opus-5-5" in out


def test_main_without_an_api_key_exits_nonzero(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert providers_refresh.main([]) == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_cartridge_main_dispatches_providers_refresh(monkeypatch):
    seen = []
    monkeypatch.setattr(providers_refresh, "main", lambda argv: seen.append(list(argv)) or 7)
    assert cartridge._main(["providers", "refresh", "--catalog", "x.yaml"]) == 7
    assert seen == [["--catalog", "x.yaml"]]
