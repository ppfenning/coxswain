"""Generates the coxswain header sprite: one six-frame SVG sheet per theme.

sprite.css loads this file with `background-image: url(...)`, and an SVG
referenced that way is an independent document. It cannot inherit the host
page's `color` or custom properties, so `currentColor` never worked here —
every fill below is a literal hex, and each theme gets its own generated
sheet. Run this module directly to regenerate both sheets after an edit.
"""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"

FRAME_WIDTH = 240
FRAME_HEIGHT = 48
FRAME_COUNT = 6

# Hull and oar colours, one set per theme, taken from docs/assets/palette.css.
PALETTES = {
    "dark": {"bg": "#0b1622", "hull": "#2a9d8f", "oar": "#e9c46a", "oar_shadow": "#b8912f"},
    "light": {"bg": "#f6f9fb", "hull": "#1d6f66", "oar": "#b8860b", "oar_shadow": "#8a6508"},
}

# Colours that read against either ground, so they do not vary by theme.
FIXED = {
    "cap": "#e63946",
    "brim": "#8c1e29",
    "skin": "#f0b083",
    "skin_shadow": "#d18f63",
    "outline": "#1b2430",
    "shirt": "#2b3a55",
    "shadow": "#1b2536",
    "seat": "#a8dad5",
    "wake_outer": "#4a90d9",
    "wake_inner": "#bfe0fb",
    "ripple": "#bfe0fb",
}

# Hull cross-section, stern to bow: (x, width, outline_y, outline_h, fill_y, fill_h).
# The 1px gap between an outline row and its fill row is the hull's border.
HULL_SEGMENTS = [
    (21, 6, 21, 6, 22, 4),  # stern-tip
    (25, 10, 19, 10, 20, 8),  # stern
    (33, 172, 17, 14, 18, 12),  # main: 14px beam
    (203, 12, 19, 10, 20, 8),  # bow
    (213, 6, 21, 6, 22, 4),  # bow-tip
]

# Per-frame oar x positions and blade width: catch, drive-early, drive,
# finish, recovery-early (feathered), recovery-late (feathered).
OAR_FRAMES = [
    ([45, 89, 133, 177], [67, 111, 155, 199], 2),
    ([43, 87, 131, 175], [65, 109, 153, 197], 2),
    ([42, 86, 130, 174], [64, 108, 152, 196], 2),
    ([39, 83, 127, 171], [61, 105, 149, 193], 2),
    ([41, 85, 129, 173], [63, 107, 151, 195], 1),
    ([44, 88, 132, 176], [66, 110, 154, 198], 1),
]
PORT_SHAFT_Y, SHAFT_H = 8, 9
PORT_BLADE_Y, BLADE_H = 5, 3
STARBOARD_SHAFT_Y = 31
STARBOARD_BLADE_Y = 40
WAKE_Y = 23
RIPPLE = (205, 17, 3, 1)

# The cox, 19 columns by 17 rows, one character per pixel, on deck at the
# stern in every frame: a red cap over a darker brim, two separated eyes, an
# open mouth mid-yell (three pixels tapering to one on the row below), a
# flaring megaphone at mouth height held by an arm to the shoulder, and a
# navy shirt with a shadow that stays clear of the teal hull. Runs of one
# character on a row collapse to a single rect.
COX_GRID = [
    ".....ehhhhhe.......",
    "....ehhhhhhhe......",
    "...eHHHHHHHHHe.....",
    "....esssssssss.....",
    "....ssesssses......",
    "....sssssssss..bb..",
    "....ssssooosss.bbbb",
    "....sssssossssBbbbb",
    "...eSSSSSSSSSes.bbb",
    "...ettttttttteb.bb.",
    "..sttttttttttts....",
    "..sttttttttttts....",
    "...ttttttttttt.....",
    "...eTTTTTTTTTe.....",
    "....eTT..TTe.......",
    "...................",
    "...................",
]
COX_X0, COX_Y0 = 24, 14
COX_LEGEND = {
    "h": "cap",
    "H": "brim",
    "s": "skin",
    "S": "skin_shadow",
    "e": "outline",
    "o": "outline",
    "t": "shirt",
    "T": "shadow",
    "b": "oar",
    "B": "oar_shadow",
}


def _rows_to_rects(rows, x0, y0, legend, colors):
    rects = []
    for row_index, row in enumerate(rows):
        y = y0 + row_index
        col = 0
        while col < len(row):
            char = row[col]
            if char == ".":
                col += 1
                continue
            start = col
            while col < len(row) and row[col] == char:
                col += 1
            rects.append((x0 + start, y, col - start, 1, colors[legend[char]]))
    return rects


def _hull_rects(colors):
    outlines = [(x, y_out, w, h_out, colors["outline"]) for x, w, y_out, h_out, _, _ in HULL_SEGMENTS]
    fills = [(x, y_fill, w, h_fill, colors["hull"]) for x, w, _, _, y_fill, h_fill in HULL_SEGMENTS]
    return outlines + fills


def _oar_rects(frame_index, colors):
    port_x, starboard_x, blade_w = OAR_FRAMES[frame_index]
    rects = []
    for x in port_x:
        rects.append((x, PORT_SHAFT_Y, 2, SHAFT_H, colors["oar"]))
        rects.append((x, PORT_BLADE_Y, blade_w, BLADE_H, colors["oar"]))
    for x in starboard_x:
        rects.append((x, STARBOARD_SHAFT_Y, 2, SHAFT_H, colors["oar"]))
        rects.append((x, STARBOARD_BLADE_Y, blade_w, BLADE_H, colors["oar"]))
    return rects


def _wake_rects(frame_index, colors):
    return [
        (12 - frame_index, WAKE_Y, 4, 2, colors["wake_outer"]),
        (16 - frame_index, WAKE_Y, 2, 2, colors["wake_inner"]),
    ]


def _frame_rects(frame_index, colors):
    rx, ry, rw, rh = RIPPLE
    return (
        _wake_rects(frame_index, colors)
        + _hull_rects(colors)
        + [(rx, ry, rw, rh, colors["ripple"])]
        + _oar_rects(frame_index, colors)
        + _rows_to_rects(COX_GRID, COX_X0, COX_Y0, COX_LEGEND, colors)
    )


def sheet(theme: str) -> str:
    """Render the whole six-frame sheet for one theme, every fill a literal hex."""
    colors = {**FIXED, **PALETTES[theme]}
    frames = []
    for i in range(FRAME_COUNT):
        rects = "".join(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}"/>'
            for x, y, w, h, fill in _frame_rects(i, colors)
        )
        frames.append(f'<g id="frame-{i + 1}" transform="translate({i * FRAME_WIDTH},0)">{rects}</g>')
    body = "\n  ".join(frames)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {FRAME_WIDTH * FRAME_COUNT} {FRAME_HEIGHT}" shape-rendering="crispEdges">\n'
        f"  {body}\n"
        "</svg>\n"
    )


def main() -> None:
    for theme in PALETTES:
        (ASSETS / f"shell-sprite-{theme}.svg").write_text(sheet(theme))


if __name__ == "__main__":
    main()
