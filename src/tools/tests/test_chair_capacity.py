

def test_a_lane_cap_on_a_shipped_cartridge_is_read_when_the_workspace_lacks_it(tmp_path):
    """The workspace holds only its team cartridge; the extends chain reaches the shipped ones (2026-10-23)."""
    from agent_tools import chair_capacity

    workspace = tmp_path / "cartridges"
    (workspace / "team").mkdir(parents=True)
    (workspace / "team" / "cartridge.yaml").write_text("extends: base\n")
    shipped = tmp_path / "shipped"
    (shipped / "base").mkdir(parents=True)
    (shipped / "base" / "cartridge.yaml").write_text("policy:\n  dispatch:\n    max_in_flight: 3\n")
    assert chair_capacity._cartridge_text(workspace, "base", shipped) is not None
    assert chair_capacity._cartridge_text(workspace, "nope", shipped) is None
