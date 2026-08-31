"""Synthetic fixtures only. Never sample a real workspace — see docs/CLEAN-ROOM.md."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def write_cartridge(directory: Path, config: dict, context: dict[str, str] | None = None) -> Path:
    """Write a cartridge.yaml plus any context packs it declares."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cartridge.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    for name, body in (context or {}).items():
        pack = directory / name
        pack.parent.mkdir(parents=True, exist_ok=True)
        pack.write_text(body, encoding="utf-8")
    return directory


@pytest.fixture
def cartridges(tmp_path: Path) -> Path:
    """A minimal base + team pair, obviously fake, enough to resolve cleanly."""
    root = tmp_path / "cartridges"
    write_cartridge(
        root / "base",
        {
            "team": "base",
            "version": 1,
            "roles": {"required": ["plan", "build"], "optional": ["triage_classify"]},
            "write_kinds": {
                "ticket_create": {"risk": "low", "ramp": "deferred"},
                "draft_pr_create": {"risk": "low", "ramp": "eligible"},
                "merge": {"risk": "high", "ramp": "never"},
            },
            "policy": {"graduation_n": 3, "regraduation_multiplier": 2, "caps": {"ticket_create": 2}},
            "context": ["context/conventions.md"],
        },
        {"context/conventions.md": "base conventions\n"},
    )
    write_cartridge(
        root / "acme",
        {
            "team": "acme",
            "extends": "base",
            "version": 1,
            "skills": {"plan": "acme-skills:plan", "build": "acme-skills:build"},
            "write_kinds": {"ticket_create": {"apply_arm": "plan"}},
            "context": ["context/code-style.md"],
        },
        {"context/code-style.md": "acme style\n"},
    )
    return root


@pytest.fixture
def skill_index() -> dict[str, list[str]]:
    """Every binding the `acme` fixture makes, resolving to exactly one body."""
    return {"acme-skills:plan": ["/fake/plan/SKILL.md"], "acme-skills:build": ["/fake/build/SKILL.md"]}


def rows(*specs, sha: str = "sha-1", profile: str = "anthropic-default") -> list[dict]:
    """Build ledger rows from (kind, risk, outcome) triples, oldest first."""
    return [
        {
            "run_id": f"r{i}",
            "ts": f"2026-08-30T00:0{i}:00Z",
            "principal": "lifecycle-propose",
            "kind": kind,
            "risk": risk,
            "outcome": outcome,
            "cartridge_sha": sha,
            "provider_profile": profile,
        }
        for i, (kind, risk, outcome) in enumerate(specs)
    ]
