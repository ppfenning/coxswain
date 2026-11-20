from agent_tools.stale_draft import Plan, Refusal, plan_stale_draft

SINCE = "2026-09-01T00:00:00+00:00"
NOW = "2026-09-27T00:00:00+00:00"
REASON = "no activity"
BODY = "\n# Title  \nBody: with colon\n\ttabbed\n"
HEAD = "---\ntitle: T\n"


def initiative(draft: str | None = None) -> str:
    head = HEAD + (f"draft: {draft}\n" if draft is not None else "")
    return head + "---\nProse.\n"


def ticket(task_id: str, state: str) -> str:
    return f"---\nid: {task_id}\nstate: {state}\n---{BODY}"


def test_blocked_and_approved_stale_tickets_become_todo_and_initiative_gains_frontmatter():
    tickets = {"a": ticket("a", "blocked"), "b": ticket("b", "approved")}
    plan = plan_stale_draft(initiative(), tickets, ["a", "b"], REASON, SINCE, NOW)
    assert plan == Plan(
        initiative_text=HEAD
        + f'draft: true\nproposed_by: "chair (stale since {SINCE}: {REASON})"\nproposed_at: "{NOW}"\nstale_tasks: [a, b]\n---\nProse.\n',
        tickets={"a": ticket("a", "todo"), "b": ticket("b", "todo")},
    )


def test_ticket_not_named_stale_is_returned_byte_for_byte():
    tickets = {"a": ticket("a", "blocked"), "c": ticket("c", "ready")}
    plan = plan_stale_draft(initiative(), tickets, ["a"], REASON, SINCE, NOW)
    assert isinstance(plan, Plan)
    assert plan.tickets["c"] == ticket("c", "ready")


def test_already_draft_initiative_refuses():
    tickets = {"a": ticket("a", "blocked")}
    assert isinstance(plan_stale_draft(initiative("true"), tickets, ["a"], REASON, SINCE, NOW), Refusal)


def test_empty_stale_task_ids_refuses():
    tickets = {"a": ticket("a", "blocked")}
    assert isinstance(plan_stale_draft(initiative(), tickets, [], REASON, SINCE, NOW), Refusal)


def test_unknown_task_id_refuses():
    tickets = {"a": ticket("a", "blocked")}
    assert plan_stale_draft(initiative(), tickets, ["zz"], REASON, SINCE, NOW) == Refusal("no ticket named zz")


def test_a_held_initiative_is_refused_and_nothing_is_rewritten():
    held = HEAD + "held: 'true'\nhold_reason: 'paused'\n---\nProse.\n"
    assert plan_stale_draft(held, {"a": ticket("a", "ready")}, ["a"], REASON, SINCE, NOW) == Refusal(
        "initiative is held; a person already decides it"
    )


def test_a_held_false_initiative_is_still_drafted():
    plan = plan_stale_draft(HEAD + "held: 'false'\n---\nProse.\n", {"a": ticket("a", "ready")}, ["a"], REASON, SINCE, NOW)
    assert isinstance(plan, Plan)
