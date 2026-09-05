import json
import sys
import tomllib
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "docs"))

import _cli  # noqa: E402
import _pull  # noqa: E402
import _shots  # noqa: E402
import _sprite  # noqa: E402


def _load_toml(name: str) -> dict:
    return tomllib.loads((ROOT / name).read_text())


class _MkdocsLoader(yaml.SafeLoader):
    """SafeLoader plus mkdocs-material's `!!python/name:` tag, kept as a string."""


_MkdocsLoader.add_multi_constructor("tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix)


def _load_mkdocs() -> dict:
    return yaml.load((ROOT / "mkdocs.yml").read_text(), Loader=_MkdocsLoader)


def test_mkdocs_status_matches_manifest_status():
    mkdocs = _load_mkdocs()
    manifest = _load_toml("manifest.toml")
    assert mkdocs["extra"]["status"] == manifest["coxswain"]["status"]


def _nav_targets(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _nav_targets(value)
    elif isinstance(node, list):
        for item in node:
            yield from _nav_targets(item)
    else:
        yield node


def test_every_doc_page_appears_in_nav():
    mkdocs = _load_mkdocs()
    nav_targets = set(_nav_targets(mkdocs["nav"]))

    docs_dir = ROOT / "docs"
    pages = set()
    for path in docs_dir.rglob("*.md"):
        parts = path.relative_to(docs_dir).parts
        if parts[0] == "components" and len(parts) >= 3:
            continue
        pages.add("/".join(parts))

    missing = pages - nav_targets
    assert not missing, f"pages missing from nav: {sorted(missing)}"


def _sprite_path(theme):
    return ROOT / "docs" / "assets" / f"shell-sprite-{theme}.svg"


def _frame_x_offset(g):
    transform = g.attrib.get("transform", "translate(0,0)")
    inside = transform[transform.index("(") + 1 : transform.index(")")]
    return int(inside.split(",")[0])


def _bounds(rects):
    tops = [int(r.attrib["y"]) for r in rects]
    bottoms = [int(r.attrib["y"]) + int(r.attrib["height"]) for r in rects]
    lefts = [int(r.attrib["x"]) for r in rects]
    rights = [int(r.attrib["x"]) + int(r.attrib["width"]) for r in rects]
    return min(tops), max(bottoms), min(lefts), max(rights)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_sprite_sheet_has_six_frames_with_correct_viewbox(theme):
    root = ET.parse(_sprite_path(theme)).getroot()

    assert root.attrib["viewBox"] == "0 0 1440 48"

    frames = root.findall("{http://www.w3.org/2000/svg}g")
    assert len(frames) == 6
    frame_ids = {g.attrib["id"] for g in frames}
    assert frame_ids == {"frame-1", "frame-2", "frame-3", "frame-4", "frame-5", "frame-6"}


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_sprite_sheet_contains_no_currentcolor(theme):
    assert "currentColor" not in _sprite_path(theme).read_text()


def test_sprite_sheets_use_their_own_palette_oar_colour():
    dark = _sprite_path("dark").read_text()
    light = _sprite_path("light").read_text()

    assert _sprite.PALETTES["dark"]["oar"] in dark
    assert _sprite.PALETTES["light"]["oar"] not in dark
    assert _sprite.PALETTES["light"]["oar"] in light
    assert _sprite.PALETTES["dark"]["oar"] not in light


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_sprite_sheet_oars_reach_above_and_below_the_hull(theme):
    ns = "{http://www.w3.org/2000/svg}"
    oar_color = _sprite.PALETTES[theme]["oar"]
    hull_color = _sprite.PALETTES[theme]["hull"]
    root = ET.parse(_sprite_path(theme)).getroot()

    frames = sorted(root.findall(f"{ns}g"), key=_frame_x_offset)
    assert [_frame_x_offset(g) for g in frames] == [0, 240, 480, 720, 960, 1200]

    for frame in frames:
        hull_rects = [r for r in frame.findall(f"{ns}rect") if r.attrib["fill"] == hull_color]
        hull_top, hull_bottom, _, _ = _bounds(hull_rects)

        oars = [r for r in frame.findall(f"{ns}rect") if r.attrib["fill"] == oar_color]
        port = [r for r in oars if int(r.attrib["y"]) + int(r.attrib["height"]) <= hull_top]
        starboard = [r for r in oars if int(r.attrib["y"]) >= hull_bottom]
        assert port, frame.attrib["id"]
        assert starboard, frame.attrib["id"]


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_sprite_sheet_cox_sits_at_the_stern_once_per_frame(theme):
    ns = "{http://www.w3.org/2000/svg}"
    cap_color = _sprite.FIXED["cap"]
    hull_color = _sprite.PALETTES[theme]["hull"]
    root = ET.parse(_sprite_path(theme)).getroot()

    for frame in root.findall(f"{ns}g"):
        hull_rects = [r for r in frame.findall(f"{ns}rect") if r.attrib["fill"] == hull_color]
        _, _, hull_min_x, hull_max_x = _bounds(hull_rects)
        hull_mid_x = (hull_min_x + hull_max_x) / 2

        cap_rects = [r for r in frame.findall(f"{ns}rect") if r.attrib["fill"] == cap_color]
        assert cap_rects, frame.attrib["id"]
        assert all(int(r.attrib["x"]) < hull_mid_x for r in cap_rects), frame.attrib["id"]


def test_sprite_sheets_are_generated_from_docs_sprite_py():
    for theme in _sprite.PALETTES:
        committed = _sprite_path(theme).read_text()
        assert _sprite.sheet(theme) == committed, "run python docs/_sprite.py"


def test_sprite_css_respects_reduced_motion():
    css = (ROOT / "docs" / "assets" / "sprite.css").read_text()
    assert "prefers-reduced-motion" in css


def test_reduced_motion_block_stops_the_animation():
    css = (ROOT / "docs" / "assets" / "sprite.css").read_text()
    media_block = css[css.index("prefers-reduced-motion") :]
    assert "animation: none" in media_block


def test_sprite_css_selects_a_sheet_per_color_scheme_at_native_size():
    root = ET.parse(_sprite_path("dark")).getroot()
    sheet_width, sheet_height = root.attrib["viewBox"].split()[2:]

    css = (ROOT / "docs" / "assets" / "sprite.css").read_text()
    assert '[data-md-color-scheme="slate"] .cox-shell' in css
    assert "shell-sprite-dark.svg" in css
    assert '[data-md-color-scheme="default"] .cox-shell' in css
    assert "shell-sprite-light.svg" in css
    assert f"background-size: {sheet_width}px {sheet_height}px" in css


def test_stroke_animation_steps_match_frame_count_and_sheet_width():
    root = ET.parse(_sprite_path("dark")).getroot()
    frame_count = len(root.findall("{http://www.w3.org/2000/svg}g"))
    sheet_width = root.attrib["viewBox"].split()[2]

    css = (ROOT / "docs" / "assets" / "sprite.css").read_text()
    stroke_block = css[css.index("@keyframes cox-stroke") : css.index("@keyframes cox-glide")]

    assert f"steps({frame_count})" in css
    assert f"background-position: -{sheet_width}px 0" in stroke_block


def test_mkdocs_lists_sprite_css():
    mkdocs = _load_mkdocs()
    assert "assets/sprite.css" in mkdocs["extra_css"]


def test_mkdocs_lists_palette_css():
    mkdocs = _load_mkdocs()
    assert "assets/palette.css" in mkdocs["extra_css"]


def test_palette_css_defines_both_schemes():
    css = (ROOT / "docs" / "assets" / "palette.css").read_text()
    assert '[data-md-color-scheme="slate"]' in css
    assert '[data-md-color-scheme="default"]' in css


def test_mkdocs_palette_is_slate_first_with_named_toggles():
    mkdocs = _load_mkdocs()
    palette = mkdocs["theme"]["palette"]

    assert isinstance(palette, list)
    assert len(palette) == 2
    assert palette[0]["scheme"] == "slate"
    assert palette[1]["scheme"] == "default"
    for entry in palette:
        assert entry["primary"] == "custom"
        assert entry["accent"] == "custom"
        assert entry["toggle"]["name"]


def test_main_html_includes_cox_shell():
    html = (ROOT / "docs" / "overrides" / "main.html").read_text()
    assert 'class="cox-shell"' in html


def test_cox_shell_is_confined_to_its_own_clipped_track():
    html = (ROOT / "docs" / "overrides" / "main.html").read_text()
    assert 'class="cox-shell-track"' in html

    css = (ROOT / "docs" / "assets" / "sprite.css").read_text()
    assert ".cox-shell-track" in css
    assert "overflow: hidden" in css


def test_main_html_has_prev_next_links():
    html = (ROOT / "docs" / "overrides" / "main.html").read_text()
    assert "page.previous_page" in html
    assert "page.next_page" in html


_CLI_HELP = """usage: cox [-h] {versions,setup,route,install} ...

positional arguments:
  {versions,setup,route,install}
                        sub-commands
    versions            Print versions
    setup               Setup commands
    route               Route commands
    install             Install components

options:
  -h, --help            show this help message and exit
"""


def test_parse_subcommands_reads_the_positional_arguments_block():
    assert _cli.parse_subcommands(_CLI_HELP) == ["versions", "setup", "route", "install"]


def test_clip_caps_a_long_capture_and_says_how_much_it_cut():
    assert _shots.clip("a\nb\nc", max_lines=10) == "a\nb\nc"
    clipped = _shots.clip("\n".join(str(n) for n in range(50)), max_lines=3)
    assert clipped.splitlines() == ["0", "1", "2", "... 47 more lines"]


def test_clip_also_truncates_a_line_wider_than_the_terminal():
    wide = _shots.clip("x" * 40, max_lines=5, max_cols=10)
    assert wide == "x" * 9 + "\u2026"
    assert _shots.clip("short", max_lines=5, max_cols=10) == "short"


def test_redact_replaces_the_home_directory_with_a_tilde():
    line = "profile ok /home/someone/.config/agent-tools/profile.yaml"
    assert _shots.redact(line, "/home/someone") == "profile ok ~/.config/agent-tools/profile.yaml"
    assert _shots.redact(line, "/home/someone/") == "profile ok ~/.config/agent-tools/profile.yaml"
    assert _shots.redact("nothing to do", "") == "nothing to do"


def test_shot_plan_has_the_four_named_commands():
    plan = _shots.shot_plan()
    argv_by_name = dict(plan)

    assert list(argv_by_name) == ["versions", "doctor", "route-status", "install-dry-run"]
    assert argv_by_name["versions"] == ["cox", "versions", "--manifest", "manifest.toml"]
    assert argv_by_name["doctor"] == ["cox", "setup", "doctor"]
    assert argv_by_name["route-status"] == ["cox", "route", "status"]
    assert argv_by_name["install-dry-run"] == [
        "cox",
        "install",
        "--dry-run",
        "--manifest",
        "manifest.toml",
        "--root",
        "/tmp/x",
    ]


def test_reference_index_is_in_nav():
    mkdocs = _load_mkdocs()
    nav_targets = set(_nav_targets(mkdocs["nav"]))
    assert "reference/cli/index.md" in nav_targets


def test_plan_yields_listed_docs_with_dests_and_skips_path_components():
    manifest = {
        "components": {
            "cartridges": {"repo": "ppfenning/coxswain-cartridges", "tag": "v0.1.0-beta.1", "docs": ["README.md"]},
            "desktop": {"path": "desktop/", "flag": "desktop"},
        }
    }

    assert _pull.plan(manifest) == [
        {
            "kind": "doc",
            "repo": "ppfenning/coxswain-cartridges",
            "entry": "README.md",
            "ref": "v0.1.0-beta.1",
            "url": "https://raw.githubusercontent.com/ppfenning/coxswain-cartridges/v0.1.0-beta.1/README.md",
            "dest": "docs/components/cartridges/README.md",
        }
    ]


def test_plan_treats_a_glob_entry_as_one_directory_listing_to_resolve():
    docs = ["graphs/delivery/epic-swarm.md", "docs/graphs/*.md"]
    manifest = {"components": {"graphs": {"repo": "ppfenning/coxswain-graphs", "tag": "v0.1.0-beta.1", "docs": docs}}}

    assert _pull.plan(manifest) == [
        {
            "kind": "doc",
            "repo": "ppfenning/coxswain-graphs",
            "entry": "graphs/delivery/epic-swarm.md",
            "ref": "v0.1.0-beta.1",
            "url": "https://raw.githubusercontent.com/ppfenning/coxswain-graphs/v0.1.0-beta.1/graphs/delivery/epic-swarm.md",
            "dest": "docs/components/graphs/graphs/delivery/epic-swarm.md",
        },
        {
            "kind": "glob",
            "repo": "ppfenning/coxswain-graphs",
            "tag": "v0.1.0-beta.1",
            "dir": "docs/graphs",
            "dest_dir": "docs/components/graphs/docs/graphs",
        },
    ]


def _extension(mkdocs, key):
    return next(e[key] for e in mkdocs["markdown_extensions"] if isinstance(e, dict) and key in e)


def test_mkdocs_enables_snippets_with_docs_base_path():
    assert _extension(_load_mkdocs(), "pymdownx.snippets")["base_path"] == ["docs"]


def test_mkdocs_enables_superfences_with_mermaid_fence():
    fences = _extension(_load_mkdocs(), "pymdownx.superfences")["custom_fences"]
    assert "mermaid" in {fence["name"] for fence in fences}


def test_every_graph_page_is_in_nav():
    nav_targets = set(_nav_targets(_load_mkdocs()["nav"]))
    pages = {f"methodology/graphs/{p.name}" for p in (ROOT / "docs" / "methodology" / "graphs").glob("*.md")}
    assert pages and not pages - nav_targets


def test_list_dir_falls_back_to_main_and_returns_the_ref_it_used(monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url.endswith("ref=v0.1.0-beta.1"):
            raise urllib.error.HTTPError(url, 404, "not found", None, None)
        return json.dumps([{"name": "epic-swarm.md"}, {"name": "README.rst"}]).encode()

    monkeypatch.setattr(_pull, "_fetch", fake_fetch)

    ref, names = _pull._list_dir("ppfenning/coxswain-graphs", "v0.1.0-beta.1", "docs/graphs")

    assert (ref, names) == ("main", ["epic-swarm.md"])
    assert calls == [
        "https://api.github.com/repos/ppfenning/coxswain-graphs/contents/docs/graphs?ref=v0.1.0-beta.1",
        "https://api.github.com/repos/ppfenning/coxswain-graphs/contents/docs/graphs?ref=main",
    ]


def test_list_dir_raises_instead_of_crashing_on_a_non_listing_response(monkeypatch):
    monkeypatch.setattr(_pull, "_fetch", lambda url: json.dumps({"name": "docs"}).encode())

    try:
        _pull._list_dir("ppfenning/coxswain-graphs", "v0.1.0-beta.1", "docs/graphs")
    except urllib.error.URLError:
        pass
    else:
        raise AssertionError("expected a URLError, not a silent return or a TypeError")


def test_glob_docs_uses_the_resolved_ref_for_each_raw_url():
    item = {
        "kind": "glob",
        "repo": "ppfenning/coxswain-graphs",
        "tag": "v0.1.0-beta.1",
        "dir": "docs/graphs",
        "dest_dir": "docs/components/graphs/docs/graphs",
    }

    assert _pull._glob_docs(item, "main", ["epic-swarm.md"]) == [
        {
            "kind": "doc",
            "url": "https://raw.githubusercontent.com/ppfenning/coxswain-graphs/main/docs/graphs/epic-swarm.md",
            "dest": "docs/components/graphs/docs/graphs/epic-swarm.md",
        }
    ]


def test_resolve_counts_an_empty_glob_listing_as_a_failure(monkeypatch):
    monkeypatch.setattr(_pull, "_list_dir", lambda repo, tag, directory: ("main", []))

    item = {"kind": "glob", "repo": "x/y", "tag": "v1", "dir": "docs/graphs", "dest_dir": "docs/components/x/docs"}
    docs, failures = _pull._resolve([item])

    assert (docs, failures) == ([], 1)


def test_run_counts_a_skipped_fetch_as_a_failure(monkeypatch):
    def fake_fetch(url):
        raise urllib.error.HTTPError(url, 404, "not found", None, None)

    monkeypatch.setattr(_pull, "_fetch", fake_fetch)
    monkeypatch.setattr(_pull, "_write", lambda dest, content: None)

    item = {"kind": "doc", "url": "https://raw.githubusercontent.com/x/y/main/README.md", "dest": "docs/x/README.md"}

    assert _pull._run([item]) == 1


def test_item_only_treats_an_exact_star_md_suffix_as_a_glob():
    assert _pull._item("graphs", "x/y", "v1", "docs/graphs/all*.md") == {
        "kind": "doc",
        "repo": "x/y",
        "entry": "docs/graphs/all*.md",
        "ref": "v1",
        "url": "https://raw.githubusercontent.com/x/y/v1/docs/graphs/all*.md",
        "dest": "docs/components/graphs/docs/graphs/all*.md",
    }


def test_a_listed_path_falls_back_to_main_exactly_as_a_glob_does():
    doc = _pull.plan(
        {"components": {"graphs": {"repo": "ppfenning/coxswain-graphs", "tag": "v9.9.9", "docs": ["docs/X.md"]}}}
    )[0]
    assert _pull.fallback_url(doc) == "https://raw.githubusercontent.com/ppfenning/coxswain-graphs/main/docs/X.md"
    assert _pull.fallback_url({**doc, "ref": "main"}) is None
    assert _pull.fallback_url({"url": "u", "dest": "d"}) is None


def test_fetch_retries_at_main_on_a_404_and_reraises_anything_else():
    doc = _pull.plan(
        {"components": {"graphs": {"repo": "ppfenning/coxswain-graphs", "tag": "v9.9.9", "docs": ["docs/X.md"]}}}
    )[0]
    seen = []

    def fetch_404_then_ok(url):
        seen.append(url)
        if url == doc["url"]:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return b"body"

    assert _pull._fetch_with_fallback(doc, fetch_404_then_ok) == b"body"
    assert seen == [doc["url"], _pull.fallback_url(doc)]

    def fetch_500(url):
        raise urllib.error.HTTPError(url, 500, "Server Error", None, None)

    with pytest.raises(urllib.error.HTTPError):
        _pull._fetch_with_fallback(doc, fetch_500)


def test_a_doc_that_resolves_at_its_tag_never_asks_for_main():
    doc = _pull.plan(
        {"components": {"graphs": {"repo": "ppfenning/coxswain-graphs", "tag": "v1", "docs": ["docs/X.md"]}}}
    )[0]
    seen = []

    def fetch_ok(url):
        seen.append(url)
        return b"body"

    assert _pull._fetch_with_fallback(doc, fetch_ok) == b"body"
    assert seen == [doc["url"]]
