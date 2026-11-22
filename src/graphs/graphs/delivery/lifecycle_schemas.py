"""lifecycle-propose's node schemas: the structured output each node of the loop is asked for."""

from __future__ import annotations

SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "phases": {"type": "array", "items": {"type": "string"}},
        "tickets": {"type": "array", "items": {"type": "string"}},
        "repos": {"type": "array", "items": {"type": "string"}},
        "state": {"type": "string", "enum": ["active", "planned", "future"]},
        "parent_epic": {"type": "string", "description": "existing epic to attach to, or empty"},
        "rationale": {"type": "string"},
    },
    "required": ["phases", "tickets", "repos", "state", "parent_epic", "rationale"],
    "additionalProperties": False,
}


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "items": {"type": "string"}},
        "files_expected": {"type": "array", "items": {"type": "string"}},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["steps", "files_expected", "out_of_scope"],
    "additionalProperties": False,
}


BUILD_SCHEMA = {
    "type": "object",
    "properties": {
        "patch": {"type": "string", "description": "unified diff, applied by the shell in its own worktree"},
        "summary": {"type": "string"},
        "files_touched": {"type": "array", "items": {"type": "string"}},
        "commands_run": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "output": {"type": "string"}},
                "required": ["command", "output"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["patch", "summary", "files_touched", "commands_run"],
    "additionalProperties": False,
}


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "charter_principle": {"type": "string"},
                    "detail": {"type": "string"},
                    "file": {"type": "string"},
                },
                "required": ["charter_principle", "detail", "file"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["verdict", "findings", "rationale"],
    "additionalProperties": False,
}


HANDOFF_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        # Not every refusal costs the same thing, so the shuttle has to say which
        # kind it is. Missing ARTIFACT or missing INPUT — no patch, a file the
        # plan needed that nobody produced — is blocking: there is nothing for a
        # builder to improve and the line stops. Missing EVIDENCE or a thin
        # summary, with the artifact present, is not: the work exists and what
        # it lacks is exactly what one more build attempt can add.
        "blocking": {
            "type": "boolean",
            "description": (
                "true when the artifact itself or an input the plan needed is missing; "
                "false when the artifact is present and only its evidence or summary is incomplete"
            ),
        },
        "missing": {"type": "array", "items": {"type": "string"}},
        "brief": {"type": "string", "description": "the small thing the next step actually needs"},
    },
    "required": ["complete", "blocking", "missing", "brief"],
    "additionalProperties": False,
}


ADVERSARY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"claim": {"type": "string"}, "why_wrong": {"type": "string"}},
                "required": ["claim", "why_wrong"],
                "additionalProperties": False,
            },
        },
        "strongest_objection": {"type": "string"},
    },
    "required": ["verdict", "objections", "strongest_objection"],
    "additionalProperties": False,
}


ARBITRATE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "sided_with": {"type": "string", "enum": ["charter", "adversary", "neither"]},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "sided_with", "reasoning"],
    "additionalProperties": False,
}


PLAN_CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "chosen": {"type": "string", "enum": ["first", "second", "merged"]},
        # Not required: a pick hands the source plan over verbatim, so a plan
        # emitted alongside `first` or `second` is a plan nobody reads, and a
        # deep-tier model should not have to write one to be discarded. A
        # merge with no plan is enforced in _plan_competition, not here: this
        # schema goes to the model's structured-output subset, which has no
        # if/then, and a merge that names no plan is a stopped graph, not a
        # fallback to either planner's.
        "plan": {**PLAN_SCHEMA, "description": "the merged plan; required when chosen is 'merged', omitted on a pick"},
        "reasoning": {"type": "string"},
        "price": {"type": "string", "description": "what choosing this plan costs, in one sentence"},
    },
    "required": ["chosen", "reasoning", "price"],
    "additionalProperties": False,
}


PLAN_ATTACK_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["proceed", "revise"]},
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"claim": {"type": "string"}, "why_wrong": {"type": "string"}},
                "required": ["claim", "why_wrong"],
                "additionalProperties": False,
            },
        },
        "strongest_objection": {"type": "string"},
    },
    "required": ["verdict", "objections", "strongest_objection"],
    "additionalProperties": False,
}
