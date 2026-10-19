from pathlib import Path

from core.catalog import load_catalog
from core.refresh import RefreshReport, RowStub, refresh_report


def test_a_newer_id_is_reported_with_an_unpriced_row_stub():
    report = refresh_report(["claude-opus-5"], ["claude-opus-5-5"])
    assert report.newer == {"opus": (RowStub(id="claude-opus-5-5", family="opus"),)}
    stub = report.newer["opus"][0]
    assert (stub.input, stub.output, stub.cache_write, stub.cache_read) == (None, None, None, None)
    assert report.unknown == ()


def test_an_older_or_equal_id_is_not_reported():
    report = refresh_report(["claude-opus-5-5"], ["claude-opus-5", "claude-opus-4-5", "claude-opus-5-5"])
    assert report == RefreshReport(newer={}, unknown=())


def test_an_equal_version_with_a_trailing_date_is_not_newer():
    report = refresh_report(["claude-haiku-4-5"], ["claude-haiku-4-5-20251001"])
    assert report.newer == {}


def test_a_two_digit_minor_is_newer_than_a_single_digit_minor():
    assert refresh_report(["claude-opus-5-9"], ["claude-opus-5-10"]).newer == {
        "opus": (RowStub(id="claude-opus-5-10", family="opus"),)
    }
    assert refresh_report(["claude-opus-5-10"], ["claude-opus-5-9"]).newer == {}


def test_each_family_is_compared_to_its_own_newest():
    report = refresh_report(
        ["claude-opus-5-5", "claude-sonnet-4-5"], ["claude-sonnet-5", "claude-opus-5-6", "claude-opus-5-7"]
    )
    assert report.newer == {
        "opus": (RowStub(id="claude-opus-5-6", family="opus"), RowStub(id="claude-opus-5-7", family="opus")),
        "sonnet": (RowStub(id="claude-sonnet-5", family="sonnet"),),
    }


def test_unparseable_and_unrecognised_family_ids_are_listed_separately():
    report = refresh_report(["claude-opus-5"], ["opus-latest", "claude-mythos-9", "claude-opus-5-1"])
    assert report.unknown == ("claude-mythos-9", "opus-latest")
    assert list(report.newer) == ["opus"]


def test_the_report_leaves_the_catalog_and_its_files_untouched(tmp_path: Path):
    path = tmp_path / "catalog.yaml"
    path.write_text(
        "models:\n"
        "  - id: claude-opus-5\n"
        "    provider: anthropic\n"
        "    price: {input: 5, output: 25, cache_write: 6.25, cache_read: 0.5}\n"
        "    effort: [high]\n"
        "    classes: [frontier]\n",
        encoding="utf-8",
    )

    def files() -> dict[str, tuple[bytes, int]]:
        return {
            str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(tmp_path.rglob("*"))
            if p.is_file()
        }

    catalog = load_catalog(path)
    assert not isinstance(catalog, list)
    ids = [e.id for e in catalog.entries]
    before = (catalog, list(ids), files())
    report = refresh_report(ids, ["claude-opus-5-5", "claude-sonnet-5", "opus-latest"])
    assert report.newer["opus"][0].id == "claude-opus-5-5"
    assert (catalog, ids, files()) == before
