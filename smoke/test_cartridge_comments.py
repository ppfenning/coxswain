"""The installed `cartridge init`, `cox settings set` and `cox host orchestrate` against a commented team cartridge.

`init` refuses to replace the file and `--force` replaces it. The two writers change their one line and keep every
comment and other key.
"""

from __future__ import annotations

import subprocess

HEADER = "# Team cartridge for smoke."
CARTRIDGE = (
    f"{HEADER}\n"
    "team: smoke\n"
    "policy:\n"
    "  dispatch:\n"
    "    # Cap on lanes in flight.\n"
    "    max_in_flight: 2  # keep small\n"
    "    local_lanes: any\n"
    "other: kept\n"
)


def _cartridge(sandbox, *args: str) -> subprocess.CompletedProcess[str]:
    """The installed `cartridge` executable, from the install's bin folder."""
    return subprocess.run([str(sandbox.install.bin / "cartridge"), *args], env=sandbox.env, cwd=sandbox.home,
                          capture_output=True, text=True, timeout=120, check=False)


def _changed(before: str, after: str) -> list[tuple[str, str]]:
    """The (old, new) pairs of lines that differ; a changed line count is its own failure."""
    old, new = before.splitlines(), after.splitlines()
    assert len(old) == len(new), (old, new)
    return [(a, b) for a, b in zip(old, new, strict=True) if a != b]


def test_init_refuses_and_force_replaces_while_settings_and_host_orchestrate_keep_comments(sandbox):
    cartridges = sandbox.home / "cartridges"
    path = cartridges / "smoke" / "cartridge.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(CARTRIDGE, encoding="utf-8")
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    text = profile.read_text(encoding="utf-8")
    extra = ("" if "\nteam:" in f"\n{text}" else "team: smoke\n") + f"cartridges_dir: {cartridges}\n"
    profile.write_text(text + extra, encoding="utf-8")

    # (1) init refuses, leaves the file byte-identical, and its help warns what --force costs
    before = path.read_bytes()
    refused = _cartridge(sandbox, "init", "smoke", "--cartridges-dir", str(cartridges))
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "already exists" in refused.stderr, refused.stderr
    assert path.read_bytes() == before
    helped = _cartridge(sandbox, "init", "--help")
    assert helped.returncode == 0, helped.stderr
    assert "lose its values and comments" in " ".join(helped.stdout.split()), helped.stdout

    # (2) settings set changes the one line
    done = sandbox.cox("settings", "set", "cartridge", "policy.dispatch.local_lanes", "decompose")
    assert done.returncode == 0, done.stdout + done.stderr
    after_set = path.read_text(encoding="utf-8")
    assert _changed(CARTRIDGE, after_set) == [("    local_lanes: any", "    local_lanes: decompose")], after_set

    # (3) host orchestrate off changes the same line back
    done = sandbox.cox("host", "orchestrate", "off")
    assert done.returncode == 0, done.stdout + done.stderr
    after_off = path.read_text(encoding="utf-8")
    assert _changed(after_set, after_off) == [("    local_lanes: decompose", "    local_lanes: any")], after_off
    assert after_off == CARTRIDGE

    # (4) init --force replaces the file, comments and all
    forced = _cartridge(sandbox, "init", "smoke", "--cartridges-dir", str(cartridges), "--force")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert HEADER not in path.read_text(encoding="utf-8")
