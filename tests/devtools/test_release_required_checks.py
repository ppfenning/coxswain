import json

from devtools.release_required_checks import Judgement, judge, render_missing, required_names


def reported(**buckets: str) -> str:
    return json.dumps([{"name": n, "bucket": b} for n, b in buckets.items()])


def test_required_names_reads_the_checks_list_context_field():
    doc = json.dumps({"strict": True, "checks": [{"context": "ci", "app_id": 1}, {"context": "docs"}]})
    assert required_names(doc) == ["ci", "docs"]


def test_required_names_reads_the_legacy_contexts_list():
    assert required_names(json.dumps({"contexts": ["ci", "docs"]})) == ["ci", "docs"]


def test_required_names_dedupes_across_both_shapes_in_first_seen_order():
    doc = json.dumps({"checks": [{"context": "ci"}, {"context": "docs"}], "contexts": ["docs", "lint", "ci"]})
    assert required_names(doc) == ["ci", "docs", "lint"]


def test_required_names_is_empty_for_an_empty_required_set():
    assert required_names(json.dumps({"checks": [], "contexts": []})) == []


def test_required_names_is_none_for_text_that_is_not_json():
    assert required_names("gh: Branch not protected") is None


def test_required_names_is_none_when_neither_key_is_present():
    assert required_names(json.dumps({"strict": True})) is None


def test_judge_counts_pass_and_skipping_as_green():
    assert judge(["ci", "docs"], reported(ci="pass", docs="skipping")) == Judgement("green", ("ci", "docs"))


def test_judge_counts_fail_as_failed():
    assert judge(["ci", "docs"], reported(ci="fail", docs="pass")) == Judgement("failed", ("ci",))


def test_judge_counts_cancel_as_failed():
    assert judge(["ci"], reported(ci="cancel")) == Judgement("failed", ("ci",))


def test_judge_counts_pending_as_pending():
    assert judge(["ci", "docs"], reported(ci="pending", docs="pass")) == Judgement("pending", ("ci",))


def test_judge_reports_a_required_check_absent_while_others_are_green():
    assert judge(["ci", "docs"], reported(ci="pass")) == Judgement("missing", ("docs",))


def test_judge_treats_unreadable_reported_json_as_nothing_reported():
    assert judge(["ci", "docs"], "not json") == Judgement("missing", ("ci", "docs"))


def test_judge_ranks_failed_over_missing():
    assert judge(["ci", "docs"], reported(ci="fail")) == Judgement("failed", ("ci",))


def test_judge_ranks_missing_over_pending():
    assert judge(["ci", "docs"], reported(ci="pending")) == Judgement("missing", ("docs",))


def test_judge_ranks_pending_over_green():
    assert judge(["ci", "docs"], reported(ci="pass", docs="pending")) == Judgement("pending", ("docs",))


def test_judge_is_green_for_an_empty_required_list():
    assert judge([], reported(ci="fail")) == Judgement("green", ())


def test_render_missing_is_one_line_with_whole_seconds_and_every_name():
    message = render_missing(["ci", "docs"], 600.0)
    assert message == "required checks not reported within 600s: ci, docs"
    assert "\n" not in message
