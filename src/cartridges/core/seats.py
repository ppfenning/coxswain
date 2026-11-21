"""The crew seats' old person names, kept for one release so existing cartridges still resolve.

The seats are named by function (crew/seats/SEATS.md). A cartridge's `crew:`
(or `cast:`) block that still binds an old name resolves through
`SEAT_ALIASES` to the function-named seat; a binding for a seat in
`RETIRED_SEATS` is dropped. Both are named in the resolved cartridge's
`deprecations`. SEATS.md's "Old names" table says the same thing, and a test
holds the two equal. Remove both, and `rename_seats`, next release.
"""

from __future__ import annotations

from collections.abc import Mapping

SEAT_ALIASES: Mapping[str, str] = {
    "sarah": "builder",
    "lewis": "reviewer",
    "nova": "recon",
    "michael": "ops",
    "emma": "planner",
    "echo": "writer",
    "daniel": "retro",
    "sky": "triage",
}

# A retired seat -> where its work went, as the deprecation says it.
RETIRED_SEATS: Mapping[str, str] = {
    "model-steward": "model choice is retro's; bind its skills under retro",
    "alice": "writer routes plans and recaps to visual-plan and visual-recap",
    "liam": "courier events are typed",
}


def rename_seats(crew: Mapping[str, object], label: str) -> tuple[dict[str, object], list[str], list[str]]:
    """`crew` with every old seat name resolved: (renamed crew, deprecations, problems).

    Pure. A layer binding both an old name and the seat it now resolves to is
    a problem rather than a silent pick of one, the same refusal a layer
    declaring both `cast` and `crew` gets. Seat order is kept.
    """
    renamed: dict[str, object] = {}
    deprecations: list[str] = []
    problems: list[str] = []
    for seat, entry in crew.items():
        if seat in RETIRED_SEATS:
            deprecations.append(f"{label}: crew seat '{seat}' is retired ({RETIRED_SEATS[seat]}); its binding is ignored")
            continue
        new = SEAT_ALIASES.get(seat, seat)
        if new != seat:
            deprecations.append(f"{label}: rename crew seat '{seat}' to '{new}'")
        if new in renamed:
            old = seat if new != seat else next(k for k, v in SEAT_ALIASES.items() if v == new and k in crew)
            problems.append(f"{label}: binds both '{old}' and '{new}'; '{old}' is a deprecated alias for '{new}'")
            continue
        renamed[new] = entry
    return renamed, deprecations, problems
