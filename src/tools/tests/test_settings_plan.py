from agent_tools import chair_cap, route
from agent_tools.settings_plan import _CHAIR_READS, Plan, PlanError, plan_set

OLD = "policy:\n  dispatch:\n    max_in_flight: 2\n"
NEW = "policy:\n  dispatch:\n    max_in_flight: 4\n"
DIFF = (
    "--- a/cartridge.yaml\n"
    "+++ b/cartridge.yaml\n"
    "@@ -1,3 +1,3 @@\n"
    " policy:\n"
    "   dispatch:\n"
    "-    max_in_flight: 2\n"
    "+    max_in_flight: 4\n"
)


def _valid(text: str) -> list[str]:
    return []


def _rejects(text: str) -> list[str]:
    return ["max_in_flight must be at most 3"]


def test_valid_change_returns_new_text_and_diff():
    assert plan_set("cartridge", "policy.dispatch.max_in_flight", "4", OLD, _valid) == Plan(NEW, DIFF, False)


def test_schema_violation_returns_error_with_no_new_text():
    result = plan_set("cartridge", "policy.dispatch.max_in_flight", "4", OLD, _rejects)
    assert result == PlanError("cartridge.yaml violates the schema: max_in_flight must be at most 3")
    assert not hasattr(result, "new_text")


def test_unknown_key_is_an_error():
    assert plan_set("cartridge", "policy.nope", "1", OLD, _valid) == PlanError("unknown setting cartridge:policy.nope")


def test_value_that_fails_coercion_is_an_error():
    result = plan_set("cartridge", "policy.dispatch.max_in_flight", "abc", OLD, _valid)
    assert result == PlanError("cartridge:policy.dispatch.max_in_flight: expected int, got 'abc'")


PROFILE = (
    "team: example-team\n"
    "skills_roots: [a, b]\n"
    'sources: {"gh": {"repo": "o/r"}}\n'
    'repo_map: {"r": "/x"}\n'
    "spend:\n"
    "  window_ceiling_usd: 50.0\n"
    "  weekly_reset: Sun 04:00 America/New_York\n"
    "chair:\n"
    "  stale_days: 3.0\n"
    "lane_hosts:\n"
    "  - name: web\n"
    "    capacity: 2\n"
)


def test_chair_profile_parser_rejection_is_its_message():
    result = plan_set("profile", "window_ceiling_usd", "abc", "team: example-team\n")
    assert result == PlanError("profile:window_ceiling_usd: line 3:   window_ceiling_usd: abc")


def test_chair_cartridge_reader_rejection_names_the_rule():
    result = plan_set("cartridge", "policy.dispatch.max_in_flight", "0", OLD, _valid)
    assert result == PlanError("policy.dispatch.max_in_flight: the chair ignores this value, expected a positive integer")


def test_profile_set_keeps_the_chair_format_for_every_other_key():
    result = plan_set("profile", "team", "other", PROFILE)
    assert isinstance(result, Plan) and result.new_text == PROFILE.replace("team: example-team", "team: other")


def test_profile_spend_key_is_written_under_spend():
    result = plan_set("profile", "window_ceiling_usd", "5", "team: example-team\n")
    assert isinstance(result, Plan) and result.new_text == "team: example-team\nspend:\n  window_ceiling_usd: 5\n"


def test_chair_reads_covers_every_chair_cap_reader():
    readers = {reader for reader, _ in _CHAIR_READS.values()}
    assert readers == {getattr(chair_cap, n) for n in dir(chair_cap) if n.endswith("_from_cartridge")}


def test_operator_only_flag_is_carried():
    result = plan_set("cartridge", "policy.review_tier", "3", "policy:\n  review_tier: 2\n", _valid)
    assert isinstance(result, Plan) and result.operator_only is True


def test_absent_default_key_is_inserted_at_its_path():
    result = plan_set("cartridge", "epic_threshold", "3", OLD, _valid)
    assert result == Plan(
        OLD + "epic_threshold: 3\n",
        "--- a/cartridge.yaml\n"
        "+++ b/cartridge.yaml\n"
        "@@ -1,3 +1,4 @@\n"
        " policy:\n"
        "   dispatch:\n"
        "     max_in_flight: 2\n"
        "+epic_threshold: 3\n",
        False,
    )


def test_absent_role_model_key_is_inserted_and_flagged_operator_only():
    result = plan_set("cartridge", "policy.review_tier", "3", OLD, _valid)
    assert result == Plan(
        OLD + "  review_tier: 3\n",
        "--- a/cartridge.yaml\n"
        "+++ b/cartridge.yaml\n"
        "@@ -1,3 +1,4 @@\n"
        " policy:\n"
        "   dispatch:\n"
        "     max_in_flight: 2\n"
        "+  review_tier: 3\n",
        True,
    )


def test_invalid_value_for_an_absent_default_key_is_refused():
    result = plan_set("profile", "chair.stale_days", "abc", "team: example-team\n")
    assert result == PlanError("profile:chair.stale_days: line 3:   stale_days: abc")


NOTIFY = "notify: {ntfy: http://ntfy.example.test/coxswain}\n"
FLOW_PROFILE = (
    "team: example-team\n"
    "# spend limits\n"
    "spend:\n"
    "  window_ceiling_usd: 50.0\n"
    "\n"
    + NOTIFY
    + "chair:\n"
    "  stale_days: 3.0\n"
)


def _changed(diff: str) -> list[str]:
    return [ln for ln in diff.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]


def test_profile_set_leaves_a_flow_style_notify_line_untouched():
    result = plan_set("profile", "window_ceiling_usd", "75", FLOW_PROFILE)
    assert isinstance(result, Plan)
    assert NOTIFY in result.new_text
    assert result.new_text == FLOW_PROFILE.replace("window_ceiling_usd: 50.0", "window_ceiling_usd: 75.0")
    assert _changed(result.diff) == ["-  window_ceiling_usd: 50.0", "+  window_ceiling_usd: 75.0"]


def test_absent_profile_key_is_added_at_the_end_of_its_block():
    result = plan_set("profile", "weekly_ceiling_usd", "200", FLOW_PROFILE)
    assert isinstance(result, Plan)
    assert result.new_text == FLOW_PROFILE.replace(
        "  window_ceiling_usd: 50.0\n", "  window_ceiling_usd: 50.0\n  weekly_ceiling_usd: 200\n"
    )
    assert _changed(result.diff) == ["+  weekly_ceiling_usd: 200"]


def test_profile_without_a_trailing_newline_gets_the_new_key_on_its_own_line():
    result = plan_set("profile", "log_retention_days", "7", "team: example-team")
    assert isinstance(result, Plan) and result.new_text == "team: example-team\nlog_retention_days: 7\n"


def test_profile_refusal_names_the_target_not_the_untouched_line():
    result = plan_set("profile", "window_ceiling_usd", "abc", FLOW_PROFILE)
    assert isinstance(result, PlanError)
    assert result.reason.startswith("profile:window_ceiling_usd: ")
    assert result.reason != NOTIFY.strip()


def test_profile_with_a_scalar_where_a_block_belongs_is_refused_with_its_target():
    result = plan_set("profile", "window_ceiling_usd", "5", "spend: 5\n")
    assert result == PlanError("profile:window_ceiling_usd: a parent key is not a mapping")


def test_profile_store_mode_refusal_names_the_target():
    result = plan_set("profile", "store.mode", "cluster", "")
    assert result == PlanError("profile:store.mode: store.mode must be one of files, shared, got 'cluster'")


def test_profile_that_does_not_parse_is_refused_with_its_target():
    result = plan_set("profile", "window_ceiling_usd", "5", "team: example-team\nnote: [oops\n")
    assert isinstance(result, PlanError)
    assert result.reason.startswith("profile:window_ceiling_usd: current file does not parse")


def test_profile_set_keeps_lane_hosts_and_a_comment_ending_the_block():
    old = "lane_hosts:\n  - name: web\n    capacity: 2\nchair:\n  stale_days: 3.0\n  # keep\nteam: t\n"
    result = plan_set("profile", "chair.housekeeping_hours", "4", old)
    assert isinstance(result, Plan)
    assert result.new_text == old.replace("  stale_days: 3.0\n", "  stale_days: 3.0\n  housekeeping_hours: 4\n")


def test_profile_flat_legacy_spend_key_is_left_and_the_spend_block_is_the_one_read():
    old = "team: t\nwindow_ceiling_usd: 9\n"
    result = plan_set("profile", "window_ceiling_usd", "5", old)
    assert isinstance(result, Plan)
    assert result.new_text == old + "spend:\n  window_ceiling_usd: 5\n"
    assert route.parse_profile(result.new_text)["window_ceiling_usd"] == 5.0


def test_profile_key_inside_an_inline_block_is_refused_with_its_target():
    result = plan_set("profile", "window_ceiling_usd", "5", "spend: {window_ceiling_usd: 1}\n")
    assert isinstance(result, PlanError) and result.reason.startswith("profile:window_ceiling_usd: spend is written inline")
