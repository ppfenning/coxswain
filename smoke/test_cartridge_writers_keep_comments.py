"""The installed `cox` and `cartridge` against a team cartridge with comments: what writes it keeps them, and what
would replace it refuses.

`cox settings set` and `cox host orchestrate` edit the one line they change. `cartridge init` on an existing file exits
non-zero and leaves it byte-identical.
"""

from __future__ import annotations

import subprocess

CARTRIDGE = (
    "# Team cartridge for smoke.\n"
    "team: smoke\n"
    "policy:\n"
    "  dispatch:\n"
    "    # Cap on lanes in flight.\n"
    "    max_in_flight: 2  # keep small\n"
    "    local_lanes: any\n"
    "other: kept\n"
)
COMMENTS = ["# Team cartridge for smoke.", "    # Cap on lanes in flight.", "    max_in_flight: 2  # keep small"]


def _ok(done: subprocess.CompletedProcess[str], what: str) -> None:
    assert done.returncode == 0, f"{what}: {done.stdout}{done.stderr}"


def _kept(path, local_lanes: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if "#" in line] == COMMENTS, lines
    assert "other: kept" in lines and f"    local_lanes: {local_lanes}" in lines, lines


def test_settings_set_and_host_orchestrate_keep_comments_and_init_refuses_to_replace(sandbox):
    cartridges = sandbox.home / "cartridges"
    path = cartridges / "smoke" / "cartridge.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(CARTRIDGE, encoding="utf-8")
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + f"cartridges_dir: {cartridges}\n", encoding="utf-8")

    _ok(sandbox.cox("settings", "set", "cartridge", "policy.dispatch.local_lanes", "decompose"), "settings set")
    _kept(path, "decompose")

    # `on` needs another active lane host besides this machine
    for name in ("lane-a", "lane-b"):
        _ok(sandbox.cox("host", "add", name, "--ssh", f"{name}.invalid", "--capacity", "2"), f"host add {name}")
    _ok(sandbox.cox("host", "orchestrate", "off"), "host orchestrate off")
    _kept(path, "any")
    _ok(sandbox.cox("host", "orchestrate", "on"), "host orchestrate on")
    _kept(path, "decompose")

    before = path.read_bytes()
    init = subprocess.run(
        [str(sandbox.install.bin / "cartridge"), "init", "smoke", "--cartridges-dir", str(cartridges)],
        env=sandbox.env, cwd=sandbox.home, capture_output=True, text=True, timeout=120, check=False,
    )
    assert init.returncode != 0, init.stdout + init.stderr
    assert "already exists" in init.stderr, init.stderr
    assert path.read_bytes() == before

    helped = subprocess.run(
        [str(sandbox.install.bin / "cartridge"), "init", "--help"],
        env=sandbox.env, cwd=sandbox.home, capture_output=True, text=True, timeout=120, check=False,
    )
    assert helped.returncode == 0, helped.stderr
    assert "lose its values and comments" in " ".join(helped.stdout.split()), helped.stdout

    # last, because it destroys the fixture
    forced = subprocess.run(
        [str(sandbox.install.bin / "cartridge"), "init", "smoke", "--cartridges-dir", str(cartridges), "--force"],
        env=sandbox.env, cwd=sandbox.home, capture_output=True, text=True, timeout=120, check=False,
    )
    _ok(forced, "init --force")
    replaced = path.read_text(encoding="utf-8")
    lines = replaced.splitlines()
    assert COMMENTS[0] not in lines and "other: kept" not in lines, replaced
    keys = [line for line in lines if not line.startswith(("#", "description:"))]
    assert keys == ["team: smoke", "extends: local", "version: 1"], replaced
