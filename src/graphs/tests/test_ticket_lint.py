from graphs.delivery.ticket_lint import (
    IdeaHeal,
    Problem,
    heal_idea_reach,
    idea_reach_problems,
    idea_reach_refusal,
    lint_refusal_text,
    lint_tickets,
)


def _task(**over):
    return {"id": "t1", "phase": "p1", "title": "x", "body": "", "needs": [], "surfaces": [], **over}


def test_a_path_outside_the_target_repo_is_a_reach_refusal():
    tasks = [_task(body="notes live at ~/scratch/notes.md")]
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    problems = lint_tickets(tasks, tree, [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "reach"
    assert problems[0].severity == "refusal"


def test_a_bare_slash_between_words_is_not_a_reach_problem():
    """tools-schema-version-1: "cartridges 1.0 / graphs 1.0" was refused as `names /, not inside`."""
    tasks = [_task(body="doctor prints `schema ok cartridges 1.0 / graphs 1.0 / tools 1.0`")]
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_a_path_shape_with_a_placeholder_is_notation_not_a_reach_problem():
    """tools-clean-guard-1..3, graphs-gate-landing-1: the seat wrote `runs/<run>/tasks/<phase>/<task>.json`
    to describe the record's shape and the corpus rule refused the whole decompose."""
    tasks = [_task(body="the record under `runs/<run>/tasks/<phase>/<task>.json` says landed")]
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_a_device_path_is_notation_not_a_reach_problem():
    """tools-chair-beater-1: "stdio to `/dev/null`" was refused as `names /dev/null, not inside`."""
    tasks = [_task(body="spawn the beater with `setsid`, stdio to `/dev/null`, its own process group")]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_a_url_route_is_notation_not_a_reach_problem():
    """graphs-openai-compatible-runner-1: `/v1/chat/completions` was refused as `names /v1/chat/completions, not inside`."""
    tasks = [_task(body="POST to `{base}` + `/v1/chat/completions`; the tracker lists at /rest/api/2/search")]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_a_rooted_path_under_a_real_filesystem_root_is_still_a_reach_refusal():
    tasks = [_task(body="read /etc/coxswain/settings.yaml and /tmp/notes.md")]
    assert [p.rule for p in lint_tickets(tasks, [], [], "graphs")] == ["reach", "reach"]


def test_an_absolute_spelling_of_a_repository_file_is_not_a_reach_problem():
    """graphs-advisory-not-surface-1: the seat wrote the repo file's absolute path and was refused."""
    tasks = [_task(body="the emit step in /home/user/repos/coxswain-graphs/graphs/delivery/ticket_lint.py merges advisories")]
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_a_real_corpus_path_is_still_a_reach_refusal():
    tasks = [_task(body="read `runs/tools-loop-fixes-3.log` for the outcome")]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert [p.rule for p in problems] == ["reach"]


def test_a_path_under_tests_is_repository_code_not_corpus():
    """runs-triage and ticket-lint, 2026-10-17: the help fixture folder of `cox runs` subcommands ends in runs/ and
    the corpus rule refused every decompose that named it."""
    tasks = [_task(body="add `tests/fixtures/help/runs/runs-triage.txt` and list the `tests/fixtures/help/runs/` folder")]
    assert [p for p in lint_tickets(tasks, [], [], "graphs") if p.rule == "reach"] == []


def test_a_command_the_sandbox_does_not_grant_is_a_grant_advisory():
    tasks = [_task(body="verify with `cox route lint t1`")]
    problems = lint_tickets(tasks, [], ["pytest", "git status", "git diff"], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "grant"
    assert problems[0].severity == "advisory"


def test_a_command_the_sandbox_grants_is_not_a_grant_advisory():
    tasks = [_task(body="verify with `pytest -q tests/test_x.py` then `git diff --stat`")]
    assert lint_tickets(tasks, [], ["pytest", "git status", "git diff"], "graphs") == []


def test_a_fenced_block_with_two_risky_commands_yields_two_grant_advisories():
    tasks = [_task(body="```bash\ncox route lint t1\ngh pr view 5\n```")]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert len(problems) == 2
    assert {p.rule for p in problems} == {"grant"}
    assert all(p.severity == "advisory" for p in problems)


def test_a_750_word_body_is_a_size_advisory():
    tasks = [_task(body=" ".join(["word"] * 750))]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "size"
    assert problems[0].severity == "advisory"


def test_two_tasks_sharing_a_test_file_with_no_needs_path_is_a_coupling_refusal():
    tasks = [
        _task(id="t1", surfaces=["tests/test_shared.py"]),
        _task(id="t2", surfaces=["tests/test_shared.py"]),
    ]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "coupling"
    assert problems[0].severity == "refusal"


def test_a_clean_dag_yields_no_problems():
    tasks = [_task(body="does nothing risky", surfaces=["graphs/delivery/ticket_lint.py"])]
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_an_in_repo_relative_path_containing_a_workspace_segment_is_not_a_reach_problem():
    tasks = [_task(body="see docs/workspace/notes.md for context", surfaces=["docs/workspace/notes.md"])]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_a_ticket_naming_workspace_runs_is_refused_with_the_corpus_correction():
    tasks = [_task(body="check workspace/runs/2026-09-08.json for the failure")]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "reach"
    assert problems[0].severity == "refusal"
    assert problems[0].fix == "route this to `cox stats` or the chair; the build seat sees one repository worktree"


def test_the_same_ticket_with_the_corpus_path_removed_passes():
    tasks = [_task(body="check the failure in the ticket's own history")]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_two_tasks_whose_named_modules_import_one_another_is_a_coupling_refusal():
    tasks = [
        _task(id="t1", surfaces=["graphs/delivery/a.py"]),
        _task(id="t2", surfaces=["graphs/delivery/b.py"]),
    ]
    tree = [
        {"path": "graphs/delivery/a.py", "repo": "graphs", "imports": ["graphs/delivery/b.py"]},
        {"path": "graphs/delivery/b.py", "repo": "graphs"},
    ]
    problems = lint_tickets(tasks, tree, [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "coupling"
    assert problems[0].severity == "refusal"


def test_the_same_import_pair_ordered_by_needs_is_not_a_coupling_problem():
    tasks = [
        _task(id="t1", surfaces=["graphs/delivery/a.py"]),
        _task(id="t2", needs=["t1"], surfaces=["graphs/delivery/b.py"]),
    ]
    tree = [
        {"path": "graphs/delivery/a.py", "repo": "graphs", "imports": ["graphs/delivery/b.py"]},
        {"path": "graphs/delivery/b.py", "repo": "graphs"},
    ]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_a_shared_test_file_ordered_by_needs_is_not_a_coupling_problem():
    tasks = [
        _task(id="t1", surfaces=["tests/test_shared.py"]),
        _task(id="t2", needs=["t1"], surfaces=["tests/test_shared.py"]),
    ]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_the_same_pair_with_the_needs_edge_removed_is_a_coupling_problem():
    tasks = [
        _task(id="t1", surfaces=["tests/test_shared.py"]),
        _task(id="t2", surfaces=["tests/test_shared.py"]),
    ]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "coupling"


def test_a_needs_chain_serializes_every_pair_transitively():
    tasks = [
        _task(id="t1", surfaces=["tests/test_shared.py"]),
        _task(id="t2", needs=["t1"], surfaces=["tests/test_shared.py"]),
        _task(id="t3", needs=["t2"], surfaces=["tests/test_shared.py"]),
    ]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_problem_severity_distinguishes_refusal_from_advisory():
    assert Problem("t1", "reach", "d", "f").severity == "refusal"
    assert Problem("t1", "coupling", "d", "f").severity == "refusal"
    assert Problem("t1", "grant", "d", "f").severity == "advisory"
    assert Problem("t1", "size", "d", "f").severity == "advisory"
    assert Problem("t1", "cross_repo", "d", "f").severity == "advisory"
    assert Problem("t1", "surface_missing", "d", "f").severity == "refusal"
    assert Problem("t1", "wrong_repo", "d", "f").severity == "refusal"


_TOOLS_TREE = [{"path": "agent_tools/run_store.py", "repo": "tools"}]


def test_a_tools_ticket_naming_a_graphs_file_gets_one_cross_repo_advisory():
    problems = lint_tickets([_task(body="edit harness/cli.py to add the flag")], _TOOLS_TREE, [], "tools")
    assert [(p.rule, p.severity, p.detail) for p in problems] == [
        ("cross_repo", "advisory", "names harness/cli.py, which lives in graphs")
    ]


def test_a_tools_ticket_naming_its_own_package_gets_no_cross_repo_advisory():
    assert lint_tickets([_task(body="edit agent_tools/run_store.py")], _TOOLS_TREE, [], "tools") == []


def test_docs_and_tests_paths_never_count_as_cross_repo():
    tasks = [_task(body="see tests/test_x.py and docs/x.md", surfaces=["tests/test_x.py", "docs/x.md"])]
    tree = [*_TOOLS_TREE, {"path": "tests/test_x.py", "repo": "tools"}, {"path": "docs/x.md", "repo": "tools"}]
    assert lint_tickets(tasks, tree, [], "tools") == []


def test_a_backticked_comma_terminated_token_still_names_the_other_repo():
    problems = lint_tickets([_task(body="change `runner/protocol.py`, then stop")], _TOOLS_TREE, [], "tools")
    assert [p.detail for p in problems] == ["names runner/protocol.py, which lives in graphs"]


def test_a_surface_naming_another_repo_is_flagged_once():
    problems = lint_tickets([_task(body="core/x.py", surfaces=["core/x.py"])], _TOOLS_TREE, [], "tools")
    assert [p.detail for p in problems] == ["names core/x.py, which lives in cartridges"]


def test_a_graphs_ticket_naming_a_new_file_under_its_own_roots_gets_no_cross_repo_advisory():
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    tasks = [_task(body="create harness/x.py and runner/y.py", surfaces=["graphs/ops/z.py (new)"])]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_the_target_repo_name_alone_marks_its_roots_as_own():
    tasks = [_task(body="create harness/x.py; see agent_tools/a.py")]
    assert [p.detail for p in lint_tickets(tasks, [], [], "~/repos/coxswain-graphs")] == [
        "names agent_tools/a.py, which lives in tools"
    ]


def test_an_unknown_target_with_no_tree_is_silent():
    assert lint_tickets([_task(body="edit harness/cli.py")], [], [], "") == []


def test_a_trailing_colon_or_quote_is_stripped_from_the_named_path():
    problems = lint_tickets([_task(body='see harness/cli.py: and "runner/protocol.py"')], _TOOLS_TREE, [], "tools")
    assert [p.detail for p in problems] == [
        "names harness/cli.py, which lives in graphs",
        "names runner/protocol.py, which lives in graphs",
    ]


def test_a_cross_repo_root_the_tree_lists_is_the_repositorys_own():
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    assert lint_tickets([_task(body="edit graphs/delivery/ticket_lint.py")], tree, [], "graphs") == []


def test_a_monorepo_ticket_naming_another_component_gets_no_cross_repo_advisory():
    tree = [{"path": "src/tools/agent_tools/route.py", "repo": "coxswain"}]
    tasks = [_task(body="edit src/graphs/harness/cli.py and src/cartridges/core/policy.py")]
    assert lint_tickets(tasks, tree, [], "~/repos/coxswain") == []


def test_a_monorepo_path_named_from_another_repo_is_flagged_as_coxswain():
    tree = [{"path": "coxswain_plugins/x.py", "repo": "plugins"}]
    problems = lint_tickets([_task(body="edit src/graphs/harness/cli.py")], tree, [], "plugins")
    assert [p.detail for p in problems] == ["names src/graphs/harness/cli.py, which lives in coxswain"]


_GRAPHS_TREE = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]


def test_a_plain_surface_absent_from_the_tree_is_a_surface_missing_refusal_with_an_add_new_fix():
    tasks = [_task(surfaces=["graphs/delivery/new_rule.py"])]
    problems = lint_tickets(tasks, _GRAPHS_TREE, [], "graphs")
    assert [(p.rule, p.severity) for p in problems] == [("surface_missing", "refusal")]
    assert "add (new)" in problems[0].fix
    assert "t1" in problems[0].detail
    assert "graphs/delivery/new_rule.py" in problems[0].detail
    assert "not in the repository" in problems[0].detail


def test_the_same_surface_marked_new_is_not_flagged():
    tasks = [_task(surfaces=["graphs/delivery/new_rule.py (new)"])]
    assert lint_tickets(tasks, _GRAPHS_TREE, [], "graphs") == []


def test_a_surface_the_tree_lists_is_not_flagged():
    tasks = [_task(surfaces=["graphs/delivery/ticket_lint.py"])]
    assert lint_tickets(tasks, _GRAPHS_TREE, [], "graphs") == []


def test_a_directory_entry_is_not_a_surface_missing_problem():
    tasks = [_task(surfaces=["graphs/delivery/"])]
    assert lint_tickets(tasks, _GRAPHS_TREE, [], "graphs") == []


def test_an_empty_tree_yields_no_surface_missing_problem():
    tasks = [_task(surfaces=["graphs/delivery/new_rule.py"])]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_a_surface_under_another_repos_roots_is_cross_repo_and_not_surface_missing():
    problems = lint_tickets([_task(surfaces=["core/x.py"])], _TOOLS_TREE, [], "tools")
    assert [p.rule for p in problems] == ["cross_repo"]


_COXSWAIN_TREE = [{"path": "src/graphs/graphs/delivery/ticket_lint.py", "repo": "coxswain"}]
_DASH_TREE = [
    {"path": "src/ui/regatta.rs", "repo": "coxswain-dash"},
    {"path": "src/tools/agent_tools/dash_feed.py", "repo": "coxswain-dash"},
]


def _wrong_repo(problems):
    return [p for p in problems if p.rule == "wrong_repo"]


def test_a_coxswain_plan_with_a_dash_surface_is_refused_naming_the_task_and_the_repository():
    tasks = [_task(id="t2", surfaces=["src/ui/regatta.rs"])]
    problems = lint_tickets(tasks, _COXSWAIN_TREE, [], "~/repos/coxswain")
    assert [(p.task, p.rule, p.severity) for p in problems] == [("t2", "wrong_repo", "refusal")]
    assert problems[0].detail == "task t2 names src/ui/regatta.rs, which belongs to coxswain-dash, not this repository"
    assert "move the task to an initiative" in problems[0].fix
    text = lint_refusal_text(problems)
    assert text is not None
    assert "t2" in text
    assert "coxswain-dash" in text


def test_the_same_dash_surface_in_a_coxswain_plan_is_not_also_surface_missing():
    problems = lint_tickets([_task(surfaces=["src/ui/regatta.rs"])], _COXSWAIN_TREE, [], "~/repos/coxswain")
    assert "surface_missing" not in [p.rule for p in problems]


def test_a_dash_plan_is_clean_for_a_dash_task_and_refused_for_a_coxswain_task():
    tasks = [_task(id="t1", surfaces=["src/ui/regatta.rs"]), _task(id="t2", surfaces=["src/tools/agent_tools/dash_feed.py"])]
    problems = lint_tickets(tasks, _DASH_TREE, [], "~/repos/coxswain-dash")
    assert [(p.task, p.rule) for p in problems] == [("t2", "wrong_repo")]
    assert "belongs to coxswain," in problems[0].detail


def test_one_task_naming_two_paths_of_one_foreign_repository_gets_one_problem():
    tasks = [_task(id="t2", surfaces=["src/ui/regatta.rs", "themes/a.toml", "src/graphs/x.py"])]
    problems = _wrong_repo(lint_tickets(tasks, _COXSWAIN_TREE, [], "~/repos/coxswain"))
    assert [p.detail for p in problems] == [
        "task t2 names src/ui/regatta.rs, themes/a.toml, which belongs to coxswain-dash, not this repository"
    ]


def test_a_plan_wholly_in_the_own_repository_has_no_wrong_repo_problem():
    tasks = [_task(surfaces=["src/graphs/graphs/delivery/ticket_lint.py", "src/tools/agent_tools/x.py (new)"])]
    assert _wrong_repo(lint_tickets(tasks, _COXSWAIN_TREE, [], "~/repos/coxswain")) == []


def test_an_unknown_repo_value_yields_no_wrong_repo_problem():
    tasks = [_task(surfaces=["src/ui/regatta.rs"])]
    assert _wrong_repo(lint_tickets(tasks, _COXSWAIN_TREE, [], "graphs")) == []
    assert _wrong_repo(lint_tickets(tasks, _COXSWAIN_TREE, [], "~/repos/other")) == []


def _contract_problems(tasks):
    return [p for p in lint_tickets(tasks, [], [], "") if p.rule == "contract"]


_PRODUCER = _task(id="p", repo="tools", interface="verb", side="producer", needs=["c"])
_CONSUMER = _task(id="k", repo="dash", interface="verb", side="consumer", needs=["c"])
_CONTRACT = _task(id="c", repo="tools", interface="verb", side="contract", surfaces=["tests/fixtures/verb.json"])


def test_a_cross_repo_consumer_with_no_contract_task_is_a_contract_refusal():
    problems = _contract_problems([{**_PRODUCER, "needs": []}, {**_CONSUMER, "needs": []}])
    assert [(p.task, p.rule, p.severity) for p in problems] == [("k", "contract", "refusal")]


def test_a_json_contract_task_in_the_producer_repo_that_both_sides_need_is_clean():
    assert _contract_problems([_PRODUCER, _CONSUMER, _CONTRACT]) == []


def test_a_consumer_missing_the_contract_id_in_needs_gets_one_problem_naming_it():
    problems = _contract_problems([_PRODUCER, {**_CONSUMER, "needs": []}, _CONTRACT])
    assert [(p.task, p.detail) for p in problems] == [("k", "interface verb: needs does not list contract task c")]


def test_a_contract_task_with_only_a_py_surface_gets_one_problem():
    contract = {**_CONTRACT, "surfaces": ["agent_tools/verb.py"]}
    assert [p.task for p in _contract_problems([_PRODUCER, _CONSUMER, contract])] == ["c"]


def test_a_new_json_fixture_surface_in_the_producer_repo_is_clean():
    contract = {**_CONTRACT, "surfaces": ["tests/fixtures/verb_v1.json (new)"]}
    assert _contract_problems([_PRODUCER, _CONSUMER, contract]) == []


def test_the_same_json_fixture_surface_without_the_new_suffix_is_clean():
    contract = {**_CONTRACT, "surfaces": ["tests/fixtures/verb_v1.json"]}
    assert _contract_problems([_PRODUCER, _CONSUMER, contract]) == []


def test_a_new_py_surface_alone_still_has_no_json_surface():
    contract = {**_CONTRACT, "surfaces": ["agent_tools/verb.py (new)"]}
    problems = _contract_problems([_PRODUCER, _CONSUMER, contract])
    assert [(p.task, "has no .json surface" in p.detail) for p in problems] == [("c", True)]


def test_a_new_non_json_surface_alone_still_has_no_json_surface():
    contract = {**_CONTRACT, "surfaces": ["notes.txt (new)"]}
    problems = _contract_problems([_PRODUCER, _CONSUMER, contract])
    assert [(p.task, "has no .json surface" in p.detail) for p in problems] == [("c", True)]


def test_two_tasks_with_one_interface_in_one_repo_give_no_contract_problem():
    assert _contract_problems([_PRODUCER, {**_CONSUMER, "repo": "tools", "needs": []}]) == []


_TREE = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]


def test_a_ticket_naming_a_file_under_an_allowed_prefix_gives_no_reach_problem(tmp_path):
    deploy = tmp_path / "deploy"
    tasks = [_task(body=f"the unit lives at {deploy / 'app' / 'unit.service'}")]
    assert lint_tickets(tasks, _TREE, [], "graphs", reach_allow=[str(deploy)]) == []


def test_a_ticket_naming_a_path_outside_the_allowed_prefix_gives_one_reach_refusal(tmp_path):
    tasks = [_task(body=f"the unit lives at {tmp_path / 'elsewhere' / 'unit.service'}")]
    problems = lint_tickets(tasks, _TREE, [], "graphs", reach_allow=[str(tmp_path / "deploy")])
    assert [(p.rule, p.severity) for p in problems] == [("reach", "refusal")]


def test_a_sibling_sharing_the_prefix_leading_characters_is_not_admitted(tmp_path):
    tasks = [_task(body=f"the unit lives at {tmp_path / 'deploy-other' / 'unit.service'}")]
    problems = lint_tickets(tasks, _TREE, [], "graphs", reach_allow=[str(tmp_path / "deploy")])
    assert [p.rule for p in problems] == ["reach"]


def test_an_allowed_prefix_with_a_trailing_slash_admits_the_prefix_and_its_children(tmp_path):
    deploy = str(tmp_path / "deploy")
    tasks = [_task(body=f"see {deploy} and {deploy}/unit.service")]
    assert lint_tickets(tasks, _TREE, [], "graphs", reach_allow=[deploy + "/"]) == []


def test_an_allowed_prefix_never_excuses_a_corpus_hit(tmp_path):
    tasks = [_task(body=f"read {tmp_path / 'deploy' / 'ledger.jsonl'}")]
    problems = lint_tickets(tasks, _TREE, [], "graphs", reach_allow=[str(tmp_path)])
    assert [p.detail.endswith("out of the build seat's reach") for p in problems] == [True]


def test_idea_prose_naming_a_path_outside_the_repo_is_one_reach_problem_for_task_idea(tmp_path):
    outside = tmp_path / "deploy" / "unit.service"
    problems = idea_reach_problems(f"ship the unit to {outside}.", _TREE, "graphs")
    assert [(p.task, p.rule) for p in problems] == [("idea", "reach")]


def test_idea_prose_naming_a_path_under_an_allowed_prefix_gives_no_problem(tmp_path):
    outside = tmp_path / "deploy" / "unit.service"
    assert idea_reach_problems(f"ship the unit to {outside}", _TREE, "graphs", [str(tmp_path / "deploy")]) == []


def test_idea_prose_with_only_notation_gives_no_problem():
    idea = "cartridges 1.0 / graphs 1.0 and the record under `runs/<run>/tasks/<task>.json`"
    assert idea_reach_problems(idea, _TREE, "graphs") == []


def test_idea_reach_refusal_for_a_tilde_path_leads_with_the_heading_and_names_the_repo():
    text = idea_reach_refusal("ship the unit to ~/deploy/unit.service", _TREE, "graphs")
    assert text is not None
    assert text.startswith("lint-refused: idea reach")
    assert "not inside" in text


def test_idea_reach_refusal_is_none_for_a_tilde_path_under_an_allowed_prefix():
    assert idea_reach_refusal("ship the unit to ~/deploy/unit.service", _TREE, "graphs", ["~/deploy"]) is None


def test_idea_reach_refusal_is_none_for_a_clean_body():
    assert idea_reach_refusal("tidy the parser and add a test", _TREE, "graphs") is None


def test_heal_rewrites_a_tilde_path_to_another_checkout_as_the_checkout_phrase_with_one_note():
    heal = heal_idea_reach("mirror ~/repos/sibling-service here", _TREE, "graphs")
    assert heal == IdeaHeal(
        "mirror the sibling-service checkout here",
        (
            'reach: idea named ~/repos/sibling-service, rewritten as "the sibling-service checkout" '
            "(the build seat reads one repository, so the path was dropped)",
        ),
        None,
    )


def test_heal_rewrites_an_absolute_home_path_with_a_suffix_as_the_file_phrase():
    heal = heal_idea_reach("install /opt/deploy/unit.service today", _TREE, "graphs")
    assert heal.text == "install the unit.service file today"
    assert heal.notes[0].startswith('reach: idea named /opt/deploy/unit.service, rewritten as "the unit.service file"')


def test_heal_strips_a_trailing_slash_before_taking_the_last_component():
    assert heal_idea_reach("see ~/repos/other/", _TREE, "graphs").text == "see the other checkout"


def test_heal_leaves_an_in_tree_path_alone():
    body = "edit /opt/work/graphs/delivery/ticket_lint.py now"
    assert heal_idea_reach(body, _TREE, "graphs") == IdeaHeal(body, (), None)


def test_heal_leaves_a_path_under_an_allowed_prefix_alone():
    body = "ship to ~/deploy/unit.service"
    assert heal_idea_reach(body, _TREE, "graphs", ["~/deploy"]) == IdeaHeal(body, (), None)


def test_heal_leaves_a_device_path_alone():
    body = "send output to /dev/null"
    assert heal_idea_reach(body, _TREE, "graphs") == IdeaHeal(body, (), None)


def test_heal_leaves_a_url_route_alone():
    body = "call /v1/chat/completions first"
    assert heal_idea_reach(body, _TREE, "graphs") == IdeaHeal(body, (), None)


def test_heal_rewrites_a_repeated_path_each_time_and_notes_it_once():
    heal = heal_idea_reach("copy ~/repos/other and then diff ~/repos/other", _TREE, "graphs")
    assert heal.text == "copy the other checkout and then diff the other checkout"
    assert len(heal.notes) == 1


def test_heal_keeps_backticks_trailing_punctuation_and_whitespace():
    heal = heal_idea_reach("see `~/repos/other`.\n\n  also (~/repos/x/y.py),  ok", _TREE, "graphs")
    assert heal.text == "see `the other checkout`.\n\n  also (the y.py file),  ok"


def test_heal_refuses_a_body_that_is_only_a_path():
    heal = heal_idea_reach("~/repos/other", _TREE, "graphs")
    assert heal.refusal == (
        "lint-refused: idea reach\n"
        "idea: reach — nothing left to plan once the paths outside graphs are rewritten"
    )


def test_heal_gives_no_refusal_for_a_whitespace_only_body():
    assert heal_idea_reach("  \n ", _TREE, "graphs") == IdeaHeal("  \n ", (), None)
