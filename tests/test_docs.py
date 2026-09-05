import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "docs"))

import _pull  # noqa: E402


def _load_toml(name: str) -> dict:
    return tomllib.loads((ROOT / name).read_text())


def _load_mkdocs() -> dict:
    return yaml.safe_load((ROOT / "mkdocs.yml").read_text())


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
        if len(parts) == 3 and parts[0] == "components" and parts[2] == "README.md":
            continue
        pages.add("/".join(parts))

    missing = pages - nav_targets
    assert not missing, f"pages missing from nav: {sorted(missing)}"


def test_shell_sprite_has_six_frames_with_correct_viewbox():
    svg_path = ROOT / "docs" / "assets" / "shell-sprite.svg"
    root = ET.parse(svg_path).getroot()

    assert root.attrib["viewBox"] == "0 0 1440 32"

    frames = root.findall("{http://www.w3.org/2000/svg}g")
    assert len(frames) == 6
    frame_ids = {g.attrib["id"] for g in frames}
    assert frame_ids == {
        "frame-1",
        "frame-2",
        "frame-3",
        "frame-4",
        "frame-5",
        "frame-6",
    }


def test_sprite_css_respects_reduced_motion():
    css = (ROOT / "docs" / "assets" / "sprite.css").read_text()
    assert "prefers-reduced-motion" in css


def test_reduced_motion_block_stops_the_animation():
    css = (ROOT / "docs" / "assets" / "sprite.css").read_text()
    media_block = css[css.index("prefers-reduced-motion") :]
    assert "animation: none" in media_block


def test_sprite_css_paints_the_sprite_sheet_at_native_size():
    svg_path = ROOT / "docs" / "assets" / "shell-sprite.svg"
    sheet_width = ET.parse(svg_path).getroot().attrib["viewBox"].split()[2]

    css = (ROOT / "docs" / "assets" / "sprite.css").read_text()
    assert "shell-sprite.svg" in css
    assert f"background-size: {sheet_width}px 32px" in css


def test_stroke_animation_steps_match_frame_count_and_sheet_width():
    svg_path = ROOT / "docs" / "assets" / "shell-sprite.svg"
    root = ET.parse(svg_path).getroot()
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


def test_plan_yields_one_pair_per_repo_component_and_skips_path_components():
    manifest = {
        "components": {
            "cartridges": {"repo": "ppfenning/coxswain-cartridges", "tag": "v0.1.0-beta.1"},
            "tools": {"repo": "ppfenning/coxswain-tools", "tag": "v0.1.0-beta.1"},
            "desktop": {"path": "desktop/", "flag": "desktop"},
        }
    }

    result = _pull.plan(manifest)

    assert result == [
        (
            "https://raw.githubusercontent.com/ppfenning/coxswain-cartridges/v0.1.0-beta.1/README.md",
            "docs/components/cartridges/README.md",
        ),
        (
            "https://raw.githubusercontent.com/ppfenning/coxswain-tools/v0.1.0-beta.1/README.md",
            "docs/components/tools/README.md",
        ),
    ]
