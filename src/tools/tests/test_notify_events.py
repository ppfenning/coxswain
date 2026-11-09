from agent_tools.chair_plan import plan_held_files
from agent_tools.notify_core import Event, render
from agent_tools.notify_events import SELF_CLEARING_CAUSES, events_from_tick

STRANDED = {"kind": "needs_chair", "initiative": "init-a", "cause": "stranded", "reason": "no patch"}
UNRELATED = {"kind": "needs_chair", "initiative": "init-b", "cause": "odd"}
LOGIN = {"kind": "needs_chair", "host": "lane1", "cause": "login_lapsed"}
UNREACHABLE = {"kind": "needs_chair", "host": "lane2", "cause": "host_unreachable", "since": "2026-11-09T04:00Z"}
BACK = {"kind": "needs_chair", "host": "lane2", "cause": "host_back", "back_at": "2026-11-09T05:00Z"}
STALLED = {
    "kind": "needs_chair",
    "initiative": "init-c",
    "run": "run-9",
    "cause": "stalled",
    "reason": "build t1, idle 45m",
}
IDLE = {
    "kind": "needs_chair",
    "initiative": "init-d",
    "cause": "idle_stall",
    "reason": "nothing moved",
    "signature": "sig1",
}


def test_stranded_gives_one_needs_chair_event():
    events = events_from_tick({}, [STRANDED])
    assert [(e.kind, e.key) for e in events] == [("needs_chair", "needs_chair:init-a:stranded")]
    assert "init-a" in events[0].title and "stranded" in events[0].body and "no patch" in events[0].body


def test_ticket_cause_gives_one_event_with_cause_and_reason():
    action = {"kind": "needs_chair", "initiative": "init-t", "cause": "ticket", "reason": "ticket body is vague"}
    (event,) = events_from_tick({}, [action])
    assert event.key == "needs_chair:init-t:ticket"
    assert "ticket" in event.body and "ticket body is vague" in event.body


def test_review_cause_gives_one_event():
    action = {"kind": "needs_chair", "initiative": "init-r", "cause": "review", "reason": "revise"}
    assert [e.key for e in events_from_tick({}, [action])] == ["needs_chair:init-r:review"]


def test_self_clearing_causes_give_no_event():
    actions = [{"kind": "needs_chair", "initiative": f"init-{c}", "cause": c, "reason": "r"}
               for c in ("no_run", "steer_deferred")]
    real = {"kind": "needs_chair", "initiative": "init-t", "cause": "ticket", "reason": "r"}
    assert [e.key for e in events_from_tick({}, [*actions, real])] == ["needs_chair:init-t:ticket"]


def test_steer_head_stuck_gives_one_stuck_event_with_the_reason():
    action = {
        "kind": "needs_chair",
        "initiative": "init-s",
        "cause": "steer_head_stuck",
        "reason": "head cannot launch",
    }
    [event] = events_from_tick({}, [action])
    assert (event.kind, event.key, event.title) == ("stuck", "stuck:init-s", "Stuck: init-s")
    assert "head cannot launch" in event.body


def test_two_steer_head_stuck_actions_for_one_initiative_give_one_event():
    first = {"kind": "needs_chair", "initiative": "init-s", "cause": "steer_head_stuck", "reason": "a"}
    second = {**first, "reason": "b"}
    assert [e.key for e in events_from_tick({}, [first, second])] == ["stuck:init-s"]


def test_a_held_file_gives_one_needs_attention_event_with_its_reason_keyed_by_path_and_blob():
    held = {"kind": "needs_chair", "initiative": "init-h", "cause": "held_file", "path": "work/a.md", "blob": "b1",
            "reason": "work/a.md: changed with no matching store change (state changed)"}
    (event,) = events_from_tick({}, [held, dict(held)])
    assert (event.kind, event.key, event.body) == ("needs_attention", "needs_attention:work/a.md|b1", held["reason"])
    (again,) = events_from_tick({}, [{**held, "blob": "b2"}])
    assert again.key == "needs_attention:work/a.md|b2"


def test_a_workspace_only_edit_planned_by_the_chair_pushes_one_needs_attention_with_the_diff_summary():
    row = {"path": "work/a.md", "initiative": "init-h", "reason": "edited by hand", "blob": "b1", "diff_summary": "state changed"}
    actions = plan_held_files({"held_files": [row]})
    (event,) = events_from_tick({}, actions)
    assert (event.kind, event.key) == ("needs_attention", "needs_attention:work/a.md|b1")
    assert "changed with no matching store change (state changed)" in event.body
    assert plan_held_files({"held_files": [row], "held_reported": ["work/a.md|b1"]}) == []


def test_a_remedy_command_is_carried_in_the_body():
    action = {**UNRELATED, "reason": "stuck", "remedy": "carry", "command": ["cox", "runs", "land", "r1"]}
    (event,) = events_from_tick({}, [action])
    assert event.body == "init-b needs you: odd.\nRemedy: cox runs land r1\nstuck"


def test_no_remedy_command_leaves_no_remedy_line():
    (absent,) = events_from_tick({}, [{**UNRELATED, "reason": "stuck"}])
    (empty,) = events_from_tick({}, [{**UNRELATED, "reason": "stuck", "command": []}])
    assert absent.body == empty.body == "init-b needs you: odd.\nstuck"


def test_a_long_reason_cannot_push_the_remedy_out_of_the_pushed_body():
    action = {**UNRELATED, "reason": "x" * 600, "command": ["cox", "runs", "land", "r1"]}
    (event,) = events_from_tick({}, [action])
    assert "Remedy: cox runs land r1" in render(event)[1]


def test_a_harness_cause_with_no_failures_still_gives_one_event():
    action = {"kind": "needs_chair", "initiative": "init-h", "cause": "harness", "harness_failures": 0}
    assert [e.key for e in events_from_tick({}, [action])] == ["needs_chair:init-h:harness"]


def test_same_initiative_and_cause_share_one_key():
    first = {**UNRELATED, "reason": "one"}
    second = {**UNRELATED, "reason": "two"}
    assert [e.key for e in events_from_tick({}, [first, second])] == ["needs_chair:init-b:odd"]


def test_a_later_action_with_a_remedy_beats_an_earlier_one_without():
    bare = {**UNRELATED, "reason": "one"}
    fixed = {**UNRELATED, "reason": "two", "command": ["cox", "go"]}
    (event,) = events_from_tick({}, [bare, fixed])
    assert event.body == "init-b needs you: odd.\nRemedy: cox go\ntwo"


def test_an_action_with_no_initiative_is_named_by_its_task_and_keyed_apart_from_another():
    one = {"kind": "needs_chair", "initiative": "", "task_id": "t1", "cause": "conflict"}
    two = {"kind": "needs_chair", "initiative": "", "task_id": "t2", "cause": "conflict"}
    events = events_from_tick({}, [one, two])
    assert [e.key for e in events] == ["needs_chair:t1:conflict", "needs_chair:t2:conflict"]
    assert events[0].title == "Needs you: t1"


def test_login_with_url_clicks_through():
    events = events_from_tick({}, [{**LOGIN, "url": "https://login.example/x"}])
    assert events == [
        Event("host_login", "host_login:lane1", "Login lapsed on lane1", events[0].body, "https://login.example/x")
    ]


def test_login_without_url_names_the_login():
    (event,) = events_from_tick({}, [LOGIN])
    assert event.click == "" and "claude login" in event.body and "lane1" in event.body


def test_host_unreachable_gives_one_event_naming_unreachable_not_login():
    (event,) = events_from_tick({}, [UNREACHABLE])
    assert (event.kind, event.key) == ("host_unreachable", "host_unreachable:lane2:2026-11-09T04:00Z")
    assert "unreachable" in event.title and "lane2" in event.title
    assert "lapsed" not in event.body and "login" not in event.body


def test_the_same_host_unreachable_action_twice_in_one_tick_gives_one_event():
    assert len(events_from_tick({}, [UNREACHABLE, UNREACHABLE])) == 1


def test_a_second_outage_on_the_same_host_keys_apart():
    later = {**UNREACHABLE, "since": "2026-11-09T09:00Z"}
    assert len({e.key for e in events_from_tick({}, [UNREACHABLE, later])}) == 2
    (bare,) = events_from_tick({}, [{k: v for k, v in UNREACHABLE.items() if k != "since"}])
    assert bare.key == "host_unreachable:lane2:"


def test_host_back_gives_one_event_titled_host_back():
    (event,) = events_from_tick({}, [BACK])
    assert (event.kind, event.key) == ("host_back", "host_back:lane2:2026-11-09T05:00Z")
    assert event.title == "Host back: lane2"


def test_stalled_gives_stall_by_run():
    (event,) = events_from_tick({}, [STALLED])
    assert (event.kind, event.key) == ("stall", "stall:run-9")
    assert "run-9" in event.body and "init-c" in event.body and "idle 45m" in event.body


def test_idle_stall_keys_on_signature_then_initiative():
    (signed,) = events_from_tick({}, [IDLE])
    assert (signed.kind, signed.key, signed.body) == ("stall", "stall:idle:sig1", "nothing moved")
    (bare,) = events_from_tick({}, [{k: v for k, v in IDLE.items() if k != "signature"}])
    assert bare.key == "stall:idle:init-d"


def test_login_stalled_and_idle_never_give_needs_chair():
    facts = {"quarantines": [{"initiative": "init-c", "harness_failures": 9}, {"initiative": "init-d", "harness_failures": 9}]}
    events = events_from_tick(facts, [LOGIN, STALLED, IDLE])
    assert [e.kind for e in events] == ["host_login", "stall", "stall"]


def test_a_dependency_wait_gives_no_event_beside_a_ticket_cause():
    wait = {"kind": "needs_chair", "initiative": "init-w", "cause": "waiting on x"}
    ticket = {"kind": "needs_chair", "initiative": "init-t", "cause": "ticket", "reason": "ticket body is vague"}
    assert [e.key for e in events_from_tick({}, [wait, ticket])] == ["needs_chair:init-t:ticket"]


def test_healthy_tick_gives_nothing():
    assert events_from_tick({}, [{"kind": "mark_lost", "initiative": "init-a"}, {"kind": "noop"}]) == []
    assert events_from_tick({}, []) == []


def test_action_order_kept_and_repeated_key_once():
    events = events_from_tick({}, [STALLED, LOGIN, STRANDED, LOGIN, STRANDED])
    assert [e.key for e in events] == ["stall:run-9", "host_login:lane1", "needs_chair:init-a:stranded"]


EMPTY_FACTS = {
    "initiatives": [{"id": "init-a", "ready_tasks": []}],
    "intake": [],
    "approved": [],
    "dispatch": {"max_in_flight": 2, "live_runs": 0, "hosts": [{"name": "lane1", "live_runs": 0}]},
}
QUEUE_EMPTY_BODY = "The lanes are idle: no ready task, intake, approved task or live run. File the next intake."


def test_an_empty_queue_with_idle_lanes_gives_one_queue_empty_event():
    (event,) = events_from_tick(EMPTY_FACTS, [])
    assert (event.key, event.title, event.body) == ("queue_empty", "Queue empty", QUEUE_EMPTY_BODY)
    assert events_from_tick({k: v for k, v in EMPTY_FACTS.items() if k != "approved"}, []) == [event]


def test_each_thing_still_to_run_suppresses_queue_empty():
    dispatch = EMPTY_FACTS["dispatch"]
    suppressors = [
        {"initiatives": [{"id": "init-a", "ready_tasks": [{"id": "t1"}]}]},
        {"intake": [{"id": "i1"}]},
        {"approved": [{"id": "t2"}]},
        {"dispatch": {**dispatch, "live_runs": 1}},
        {"dispatch": {**dispatch, "hosts": [{"name": "lane1", "live_runs": 1}]}},
    ]
    assert [events_from_tick({**EMPTY_FACTS, **s}, []) for s in suppressors] == [[]] * 5


def test_missing_facts_never_fire_queue_empty():
    for missing in ("initiatives", "intake", "dispatch"):
        assert events_from_tick({k: v for k, v in EMPTY_FACTS.items() if k != missing}, []) == []


def test_a_needs_chair_event_suppresses_queue_empty():
    assert [e.key for e in events_from_tick(EMPTY_FACTS, [STRANDED])] == ["needs_chair:init-a:stranded"]


def test_idle_ready_is_not_self_clearing_and_pushes_keyed_on_subject_and_cause():
    action = {
        "kind": "needs_chair",
        "initiative": "init-a",
        "cause": "idle_ready",
        "signature": "idle_ready:init-a,init-b",
        "reason": "init-a waits on init-b",
    }
    assert "idle_ready" not in SELF_CLEARING_CAUSES
    (event,) = events_from_tick({}, [action])
    assert (event.kind, event.key) == ("needs_chair", "needs_chair:init-a:idle_ready")
    assert "init-a waits on init-b" in event.body
