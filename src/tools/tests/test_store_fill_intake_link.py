from agent_tools import store_fill

_INTAKE = {
    "kind": "intake", "initiative": "", "task_id": "alpha", "phase": "", "state": "queued", "needs": [],
    "title": "A", "surfaces": [], "body": "", "extra": {},
}
_INITIATIVE = {
    "kind": "initiative", "initiative": "alpha", "task_id": "alpha", "phase": "", "state": "todo", "needs": [],
    "title": "Alpha", "surfaces": [], "body": "why", "extra": {"id": "alpha"},
}


def test_an_id_naming_an_initiative_is_decomposed_and_linked():
    assert store_fill.intake_state("alpha", frozenset({"alpha"})) == ("decomposed", {"initiative": "alpha"})


def test_an_id_naming_no_initiative_stays_queued_without_a_link():
    state, extra = store_fill.intake_state("beta", frozenset({"alpha"}))
    assert (state, "initiative" in extra) == ("queued", False)


def test_initiative_ids_takes_only_initiative_rows():
    assert store_fill.initiative_ids([_INITIATIVE, {**_INTAKE, "task_id": "x"}]) == frozenset({"alpha"})


def test_plan_fill_inserts_a_matching_intake_as_decomposed():
    (write,) = store_fill.plan_fill([_INTAKE], [_INITIATIVE]).writes
    assert (write["state"], write["extra"]["initiative"]) == ("decomposed", "alpha")


def test_plan_fill_inserts_an_unmatched_intake_as_queued():
    (write,) = store_fill.plan_fill([{**_INTAKE, "task_id": "beta"}], [_INITIATIVE]).writes
    assert (write["state"], "initiative" in write["extra"]) == ("queued", False)


def test_plan_fill_links_a_stored_queued_intake():
    plan = store_fill.plan_fill([_INTAKE], [_INITIATIVE, _INTAKE])
    (write,) = plan.writes
    assert (write["state"], write["extra"]["initiative"]) == ("decomposed", "alpha")
    assert (plan.inserted, plan.filled, plan.unchanged) == (0, 1, 0)


def test_a_stored_intake_links_by_task_id():
    (write,) = store_fill.intake_links([_INTAKE], [_INITIATIVE])
    assert (write["state"], write["extra"]) == ("decomposed", {"initiative": "alpha"})


def test_a_stored_intake_links_by_the_stem_of_extra_intake():
    initiative = {**_INITIATIVE, "task_id": "alpha-2", "extra": {"intake": "intake/alpha.md", "id": "alpha-2"}}
    (write,) = store_fill.intake_links([_INTAKE], [initiative])
    assert write["extra"]["initiative"] == "alpha-2"


def test_an_unmatched_intake_is_not_linked():
    assert store_fill.intake_links([{**_INTAKE, "task_id": "beta"}], [_INITIATIVE]) == ()


def test_an_initiative_inserted_in_the_same_fill_links_a_stored_intake():
    plan = store_fill.plan_fill([_INITIATIVE], [_INTAKE])
    link = next(w for w in plan.writes if w["kind"] == "intake")
    assert (link["state"], link["extra"]["initiative"]) == ("decomposed", "alpha")
    assert (len(plan.writes), plan.inserted, plan.filled, plan.unchanged) == (2, 1, 1, 0)


def test_a_stored_done_intake_is_untouched():
    assert store_fill.intake_links([{**_INTAKE, "state": "done"}], [_INITIATIVE]) == ()


def test_a_stored_intake_already_linked_is_untouched():
    linked = {**_INTAKE, "extra": {"initiative": "alpha"}}
    assert store_fill.intake_links([linked], [_INITIATIVE]) == ()


def test_a_link_drops_claim_keys_and_leaves_its_input_unchanged():
    held = {**_INTAKE, "extra": {"repo": "r"}, "holder": "me", "epoch": 2, "expires_at": "t"}
    before = {**held, "extra": dict(held["extra"])}
    (write,) = store_fill.intake_links([held], [_INITIATIVE])
    assert (held, "holder" in write, write["extra"]) == (before, False, {"repo": "r", "initiative": "alpha"})


def test_a_stored_intake_both_filled_and_linked_yields_one_write():
    bare = {**_INTAKE, "title": None}
    plan = store_fill.plan_fill([_INTAKE], [_INITIATIVE, bare])
    (write,) = plan.writes
    assert (write["title"], write["state"]) == ("A", "decomposed")
    assert (plan.inserted, plan.filled, plan.unchanged) == (0, 1, 0)


def test_plan_fill_keeps_a_done_intake_done():
    (write,) = store_fill.plan_fill([{**_INTAKE, "state": "done"}], [_INITIATIVE]).writes
    assert write["state"] == "done"
