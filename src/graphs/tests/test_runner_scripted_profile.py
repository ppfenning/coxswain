"""`runner: scripted` in a provider profile builds the offline runner, so a launch that passes no flag runs offline."""

import json

import pytest

from harness.runners import build_runner
from runner import ScriptedRunner


def _profile(tmp_path, **extra) -> str:
    path = tmp_path / "provider.yaml"
    path.write_text(json.dumps({"tiers": {}, "runner": "scripted", **extra}), encoding="utf-8")
    return str(path)


def test_the_profile_names_the_answers_and_the_runner_replays_them(tmp_path):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"plan": {"steps": ["one"]}}), encoding="utf-8")
    runner = build_runner(scripted=None, provider_profile=_profile(tmp_path, scripted=str(answers)))
    assert isinstance(runner, ScriptedRunner)
    assert runner.run(role="plan", schema={}, prompt="p") == {"steps": ["one"]}


def test_a_scripted_runner_with_no_answers_file_named_is_refused(tmp_path):
    with pytest.raises(ValueError, match="needs `scripted:`"):
        build_runner(scripted=None, provider_profile=_profile(tmp_path))
