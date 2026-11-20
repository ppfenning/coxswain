"""Recorded `lane_prereqs` probe replies for tests whose runner answers every argv itself: the lines the real script
prints on a host with docker up, every smoke image present, the PostgreSQL client tools and a real gh."""

from agent_tools import lane_prereqs

_MARK = "cox-lane-prereq"


def probe_output(
    *,
    docker: str = "0\t/usr/local/bin/docker\t27.3.1",
    tools: dict[str, str] | None = None,
    creds: str = "\t",
    images: tuple[str, ...] | None = None,
    gh: str = "0\t/usr/local/bin/gh\tgh version 2.60.1 (2026-10-01)",
    space: tuple[str, ...] = ("/tmp\ttmpfs 16262544 12992 16249552 1% /tmp",),
    memory: tuple[str, ...] = ("MemAvailable:   24433688 kB",),
) -> str:
    """The probe's stdout, each field overridable; `images` are whole `image` lines past the kind, `space` whole
    `space` lines and `memory` the meminfo or vm_stat lines."""
    found = tools if tools is not None else {name: f"/usr/local/bin/{name}" for name in lane_prereqs.CLIENT_TOOLS}
    image_lines = images if images is not None else tuple(f"present\t-\t{i}\t" for i in lane_prereqs.SMOKE_IMAGES)
    lines = [
        *(f"{_MARK}\ttool\t{name}\t{path}" for name, path in found.items()),
        f"{_MARK}\tdocker\t{docker}",
        f"{_MARK}\tcreds\t{creds}",
        *(f"{_MARK}\timage\t{line}" for line in image_lines),
        f"{_MARK}\tgh\t{gh}",
        *(f"{_MARK}\tspace\t{line}" for line in space),
        *(f"{_MARK}\tmemline\t{line}" for line in memory),
        f"{_MARK}\tend",
    ]
    return "\n".join(lines) + "\n"


PASSING = probe_output()
PASSING_VERSIONS = {"lane_ok": True, "room_ok": True, "room_failures": 0, "lane_capabilities": ["docker"]}
# The lines `remote_doctor.doctor_on_host` prints for PASSING, after its other rows.
PASSING_LINES = [
    *(lane_prereqs.row_line(row) for row in lane_prereqs.reply_rows(0, PASSING, remote=True)),
    "lane capabilities: docker",
]


def is_lane_probe(argv: list[str]) -> bool:
    """Whether `argv` (ssh-wrapped or not) runs the lane probe script."""
    return any("cox-lane-prereq" in part for part in argv)
