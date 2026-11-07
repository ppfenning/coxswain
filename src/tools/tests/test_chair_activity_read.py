from pathlib import Path

from agent_tools.chair_activity_read import (
    catalog_status,
    prices_for_profile,
    project_folder_name,
    read_agents,
    read_project_sessions,
    read_session_lines,
    resolve_by_content,
    resolve_session,
    session_status,
)

CATALOG = "models:\n  m1:\n    price: {input: 1.0, output: 2.0}\n"
OTHER = "models:\n  m2:\n    price: {input: 3.0, output: 4.0}\n"


def _tree(tmp_path: Path) -> Path:
    projects = tmp_path / "projects"
    subagents = projects / "proj" / "sess" / "subagents"
    subagents.mkdir(parents=True)
    (projects / "proj" / "sess.jsonl").write_text("s1\ns2\n")
    (subagents / "agent-b.meta.json").write_text('{"kind": "x"}')
    (subagents / "agent-b.jsonl").write_text("b1\nb2\n")
    (subagents / "agent-a.meta.json").write_text('{"kind": "y"}')
    (subagents / "agent-c.meta.json").write_text("not json")
    return projects


def test_read_agents_ids_order_and_skipped_file(tmp_path):
    agents = read_agents(_tree(tmp_path), "sess")
    assert [a["agent_id"] for a in agents] == ["a", "b"]
    assert agents[0] == {"agent_id": "a", "meta": {"kind": "y"}, "lines": []}
    assert agents[1]["lines"] == ["b1", "b2"]


def test_read_agents_empty_or_unknown_session(tmp_path):
    projects = _tree(tmp_path)
    assert read_agents(projects, "") == []
    assert read_agents(projects, "other") == []


def test_read_session_lines(tmp_path):
    projects = _tree(tmp_path)
    assert read_session_lines(projects, "sess") == ["s1", "s2"]
    assert read_session_lines(projects, "other") == []


def test_read_agents_skips_undecodable_unreadable_and_non_object_meta(tmp_path):
    projects = _tree(tmp_path)
    subagents = projects / "proj" / "sess" / "subagents"
    (subagents / "agent-d.meta.json").write_bytes(b"\xff\xfe\x00{")
    (subagents / "agent-e.meta.json").mkdir()
    (subagents / "agent-f.meta.json").write_text("[1, 2]")
    assert [a["agent_id"] for a in read_agents(projects, "sess")] == ["a", "b"]


def test_read_agents_undecodable_transcript_gives_no_lines(tmp_path):
    projects = _tree(tmp_path)
    (projects / "proj" / "sess" / "subagents" / "agent-a.jsonl").write_bytes(b"\xff\xfe")
    assert read_agents(projects, "sess")[0]["lines"] == []


def test_read_session_lines_picks_first_project_in_sorted_order(tmp_path):
    projects = _tree(tmp_path)
    (projects / "aaa").mkdir()
    (projects / "aaa" / "sess.jsonl").write_text("first\n")
    assert read_session_lines(projects, "sess") == ["first"]


def test_read_session_lines_empty_session(tmp_path):
    assert read_session_lines(_tree(tmp_path), "") == []


def _reader(files: dict[Path, str]):
    return files.get


PROFILE = {"provider_profile": "/p/profile.yaml", "cartridges_dir": "/c"}
FIRST = Path("/p/catalog.yaml")
FALLBACK = Path("/c/providers/catalog.yaml")


def test_prices_from_first_catalog():
    read = _reader({FIRST: CATALOG, FALLBACK: OTHER})
    assert prices_for_profile(PROFILE, read) == {"m1": {"input": 1.0, "output": 2.0}}


def test_prices_from_fallback_catalog():
    read = _reader({FALLBACK: OTHER})
    assert prices_for_profile(PROFILE, read) == {"m2": {"input": 3.0, "output": 4.0}}


def test_prices_from_neither_catalog():
    assert prices_for_profile(PROFILE, _reader({})) == {}


def test_prices_from_invalid_yaml():
    assert prices_for_profile(PROFILE, _reader({FIRST: "a: [unclosed"})) == {}


def test_prices_for_empty_profile():
    assert prices_for_profile(None) == {}
    assert prices_for_profile({}) == {}


def test_prices_default_reader_undecodable_catalog_falls_through(tmp_path):
    (tmp_path / "catalog.yaml").write_bytes(b"\xff\xfe\x00")
    cartridges = tmp_path / "cartridges"
    (cartridges / "providers").mkdir(parents=True)
    (cartridges / "providers" / "catalog.yaml").write_text(OTHER)
    profile = {"provider_profile": str(tmp_path / "profile.yaml"), "cartridges_dir": str(cartridges)}
    assert prices_for_profile(profile) == {"m2": {"input": 3.0, "output": 4.0}}
    assert prices_for_profile({"provider_profile": str(tmp_path / "profile.yaml")}) == {}


def test_prices_default_reader_reads_file(tmp_path):
    (tmp_path / "catalog.yaml").write_text(CATALOG)
    profile = {"provider_profile": str(tmp_path / "profile.yaml")}
    assert prices_for_profile(profile) == {"m1": {"input": 1.0, "output": 2.0}}


UUID = "1a2b3c4d-1111-2222-3333-444455556666"
OTHER_UUID = "1a2b3c4d-9999-8888-7777-666655554444"


def _uuid_tree(tmp_path: Path, *uuids: str, project: str = "proj") -> Path:
    projects = tmp_path / "projects"
    for uuid in uuids:
        subagents = projects / project / uuid / "subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        (projects / project / f"{uuid}.jsonl").write_text(f"line-{uuid[-4:]}\n")
        (subagents / "agent-a.meta.json").write_text('{"kind": "x"}')
        (subagents / "agent-a.jsonl").write_text("a1\n")
    return projects


def test_resolve_session_values():
    assert resolve_session(["abc-1"], [], "") == ("none", "")
    assert resolve_session([], [], "abc") == ("not_found", "")
    assert resolve_session([], ["abc-1"], "abc") == ("not_found", "abc-1")
    assert resolve_session([], ["abc-1", "abc-2"], "abc") == ("ambiguous", "")
    assert resolve_session(["abc-1", "abc-1"], ["abc-1"], "abc") == ("ok", "abc-1")
    assert resolve_session(["abc-1"], [], "abc") == ("ok", "abc-1")
    assert resolve_session(["abc-1", "abc-2"], [], "abc") == ("ambiguous", "")
    assert resolve_session(["abc-1"], ["abc-1", "abc-2"], "abc") == ("ambiguous", "")
    assert resolve_session(["abc-1"], ["abc-2"], "abc") == ("ambiguous", "")


def test_short_id_reads_full_uuid_transcript_and_subagent_folder(tmp_path):
    projects = _uuid_tree(tmp_path, UUID)
    assert read_session_lines(projects, "1a2b3c4d") == ["line-6666"]
    agents = read_agents(projects, "1a2b3c4d")
    assert [a["agent_id"] for a in agents] == ["a"]
    assert agents[0]["lines"] == ["a1"]
    assert session_status(projects, "1a2b3c4d") == "ok"


def test_full_id_still_matches_itself(tmp_path):
    projects = _uuid_tree(tmp_path, UUID, OTHER_UUID)
    assert read_session_lines(projects, UUID) == ["line-6666"]
    assert [a["agent_id"] for a in read_agents(projects, UUID)] == ["a"]
    assert session_status(projects, UUID) == "ok"


def test_shared_prefix_is_ambiguous_and_reads_nothing(tmp_path):
    projects = _uuid_tree(tmp_path, UUID, OTHER_UUID)
    assert session_status(projects, "1a2b3c4d") == "ambiguous"
    assert read_session_lines(projects, "1a2b3c4d") == []
    assert read_agents(projects, "1a2b3c4d") == []


def test_ambiguous_subagent_folders_alone_make_session_ambiguous(tmp_path):
    projects = _uuid_tree(tmp_path, UUID)
    (projects / "proj" / OTHER_UUID / "subagents").mkdir(parents=True)
    assert read_session_lines(projects, "1a2b3c4d") == []
    assert read_agents(projects, "1a2b3c4d") == []
    assert session_status(projects, "1a2b3c4d") == "ambiguous"


def test_transcript_and_subagent_folder_of_different_sessions_is_ambiguous(tmp_path):
    projects = _uuid_tree(tmp_path, UUID)
    (projects / "proj" / f"{UUID}.jsonl").rename(projects / "proj" / f"{OTHER_UUID}.jsonl")
    assert session_status(projects, "1a2b3c4d") == "ambiguous"
    assert read_session_lines(projects, "1a2b3c4d") == []
    assert read_agents(projects, "1a2b3c4d") == []


def test_subagent_folder_without_transcript_still_reads_agents(tmp_path):
    projects = _uuid_tree(tmp_path, UUID)
    (projects / "proj" / f"{UUID}.jsonl").unlink()
    for session in (UUID, "1a2b3c4d"):
        assert [a["agent_id"] for a in read_agents(projects, session)] == ["a"]
        assert session_status(projects, session) == "not_found"
        assert read_session_lines(projects, session) == []


def test_ambiguous_subagent_folders_without_transcript_are_ambiguous(tmp_path):
    projects = _uuid_tree(tmp_path, UUID, OTHER_UUID)
    for transcript in projects.glob("*/*.jsonl"):
        transcript.unlink()
    assert session_status(projects, "1a2b3c4d") == "ambiguous"
    assert read_agents(projects, "1a2b3c4d") == []


def test_dotted_neighbour_files_are_not_sessions(tmp_path):
    projects = _uuid_tree(tmp_path, UUID)
    (projects / "proj" / f"{UUID}.bak.jsonl").write_text("old\n")
    (projects / "proj" / f"{UUID}.bak" / "subagents").mkdir(parents=True)
    assert session_status(projects, "1a2b3c4d") == "ok"
    assert read_session_lines(projects, "1a2b3c4d") == ["line-6666"]


def test_same_session_in_two_projects_is_one_match_first_project_read(tmp_path):
    projects = _uuid_tree(tmp_path, UUID, project="zzz")
    _uuid_tree(tmp_path, UUID, project="aaa")
    (projects / "aaa" / f"{UUID}.jsonl").write_text("first\n")
    assert session_status(projects, "1a2b3c4d") == "ok"
    assert read_session_lines(projects, "1a2b3c4d") == ["first"]


def test_unknown_and_empty_session_status(tmp_path):
    projects = _uuid_tree(tmp_path, UUID)
    assert session_status(projects, "ffffffff") == "not_found"
    assert session_status(projects, "") == "none"
    assert read_agents(projects, "ffffffff") == []


def test_catalog_status_ok_and_missing():
    assert catalog_status(PROFILE, _reader({FIRST: CATALOG})) == "ok"
    assert catalog_status(PROFILE, _reader({})) == "missing"
    assert catalog_status(PROFILE, _reader({FIRST: "a: [unclosed"})) == "missing"
    assert catalog_status(PROFILE, _reader({FIRST: "models: {}\n"})) == "missing"
    assert catalog_status(None) == "missing"


LEASE = "lease-0000-aaaa"


def _resumed_tree(tmp_path: Path, name: str = "orig-name") -> Path:
    projects = tmp_path / "projects"
    subagents = projects / "proj" / name / "subagents"
    subagents.mkdir(parents=True)
    (projects / "proj" / f"{name}.jsonl").write_text(f'{{"sessionId": "{LEASE}"}}\nsecond\n')
    (subagents / "agent-a.meta.json").write_text('{"kind": "x"}')
    (subagents / "agent-a.jsonl").write_text("a1\n")
    return projects


def test_resolve_by_content_values():
    a, b, c = Path("p1/a.jsonl"), Path("p2/a.jsonl"), Path("p1/c.jsonl")
    assert resolve_by_content({a: True}, "") == ("none", None)
    assert resolve_by_content({}, LEASE) == ("not_found", None)
    assert resolve_by_content({a: False, c: False}, LEASE) == ("not_found", None)
    assert resolve_by_content({a: True, c: False}, LEASE) == ("ok", a)
    assert resolve_by_content({b: True, a: True}, LEASE) == ("ok", a)
    assert resolve_by_content({a: True, c: True}, LEASE) == ("ambiguous", None)


def test_resumed_session_resolves_by_content(tmp_path):
    projects = _resumed_tree(tmp_path)
    assert session_status(projects, LEASE) == "ok"
    assert read_session_lines(projects, LEASE) == [f'{{"sessionId": "{LEASE}"}}', "second"]
    agents = read_agents(projects, LEASE)
    assert [a["agent_id"] for a in agents] == ["a"]
    assert agents[0]["lines"] == ["a1"]


def test_id_in_no_file_is_not_found(tmp_path):
    projects = _resumed_tree(tmp_path)
    assert session_status(projects, "other-id") == "not_found"
    assert read_session_lines(projects, "other-id") == []
    assert read_agents(projects, "other-id") == []


def test_id_in_two_transcripts_is_ambiguous(tmp_path):
    projects = _resumed_tree(tmp_path)
    (projects / "proj" / "second-name.jsonl").write_text(f"{LEASE}\n")
    assert session_status(projects, LEASE) == "ambiguous"
    assert read_session_lines(projects, LEASE) == []
    assert read_agents(projects, LEASE) == []


def test_file_name_match_wins_over_content_match(tmp_path):
    projects = _resumed_tree(tmp_path)
    (projects / "proj" / f"{LEASE}.jsonl").write_text("named\n")
    assert session_status(projects, LEASE) == "ok"
    assert read_session_lines(projects, LEASE) == ["named"]
    assert read_agents(projects, LEASE) == []


WORKSPACE = Path("/home/user/my.repo/a_b")
FOLDER = "-home-user-my-repo-a-b"


def _project_tree(tmp_path: Path) -> tuple[Path, Path]:
    projects = tmp_path / "projects"
    folder = projects / FOLDER
    for session, tag in (("s2", "y"), ("s1", "x")):
        subagents = folder / session / "subagents"
        subagents.mkdir(parents=True)
        (folder / f"{session}.jsonl").write_text(f"{session}-1\n{session}-2\n")
        (subagents / "agent-b.meta.json").write_text(f'{{"kind": "{tag}b"}}')
        (subagents / "agent-b.jsonl").write_text(f"{session}-b\n")
        (subagents / "agent-a.meta.json").write_text(f'{{"kind": "{tag}a"}}')
    return projects, folder


def test_project_folder_name_replaces_non_alphanumerics():
    assert project_folder_name(WORKSPACE) == FOLDER


def test_read_project_sessions_two_sessions_with_agents(tmp_path):
    projects, _ = _project_tree(tmp_path)
    assert read_project_sessions(projects, WORKSPACE) == [
        {
            "session_id": "s1",
            "lines": ["s1-1", "s1-2"],
            "agents": [
                {"agent_id": "a", "meta": {"kind": "xa"}, "lines": []},
                {"agent_id": "b", "meta": {"kind": "xb"}, "lines": ["s1-b"]},
            ],
        },
        {
            "session_id": "s2",
            "lines": ["s2-1", "s2-2"],
            "agents": [
                {"agent_id": "a", "meta": {"kind": "ya"}, "lines": []},
                {"agent_id": "b", "meta": {"kind": "yb"}, "lines": ["s2-b"]},
            ],
        },
    ]


def test_read_project_sessions_ignores_other_project_folder(tmp_path):
    projects, _ = _project_tree(tmp_path)
    (projects / "-srv-other").mkdir()
    (projects / "-srv-other" / "s9.jsonl").write_text("other\n")
    assert [s["session_id"] for s in read_project_sessions(projects, WORKSPACE)] == ["s1", "s2"]


def test_read_project_sessions_skips_dotted_neighbour(tmp_path):
    projects, folder = _project_tree(tmp_path)
    (folder / "s1.bak.jsonl").write_text("bak\n")
    assert [s["session_id"] for s in read_project_sessions(projects, WORKSPACE)] == ["s1", "s2"]


def test_read_project_sessions_missing_folder(tmp_path):
    assert read_project_sessions(tmp_path / "projects", WORKSPACE) == []
    (tmp_path / "projects").mkdir()
    assert read_project_sessions(tmp_path / "projects", WORKSPACE) == []


def test_read_project_sessions_skips_bad_meta_files(tmp_path):
    projects, folder = _project_tree(tmp_path)
    subagents = folder / "s1" / "subagents"
    (subagents / "agent-c.meta.json").write_text("not json")
    (subagents / "agent-d.meta.json").write_text("[1, 2]")
    (subagents / "agent-e.meta.json").write_bytes(b"\xff\xfe\x00{")
    (subagents / "agent-f.meta.json").mkdir()
    sessions = read_project_sessions(projects, WORKSPACE)
    assert [a["agent_id"] for a in sessions[0]["agents"]] == ["a", "b"]


def test_read_project_sessions_undecodable_transcript_gives_empty_lines(tmp_path):
    projects, folder = _project_tree(tmp_path)
    (folder / "s1.jsonl").write_bytes(b"\xff\xfe")
    sessions = read_project_sessions(projects, WORKSPACE)
    assert [(s["session_id"], s["lines"]) for s in sessions] == [("s1", []), ("s2", ["s2-1", "s2-2"])]


def test_read_project_sessions_subagent_folder_without_transcript_is_not_a_session(tmp_path):
    projects, folder = _project_tree(tmp_path)
    (folder / "s3" / "subagents").mkdir(parents=True)
    (folder / "s3" / "subagents" / "agent-a.meta.json").write_text("{}")
    assert [s["session_id"] for s in read_project_sessions(projects, WORKSPACE)] == ["s1", "s2"]


def test_read_agents_unchanged_and_shared_with_project_sessions(tmp_path):
    expected = [
        {"agent_id": "a", "meta": {"kind": "y"}, "lines": []},
        {"agent_id": "b", "meta": {"kind": "x"}, "lines": ["b1", "b2"]},
    ]
    projects = _tree(tmp_path)
    assert read_agents(projects, "sess") == expected
    (projects / "proj").rename(projects / FOLDER)
    assert read_project_sessions(projects, WORKSPACE)[0]["agents"] == expected
