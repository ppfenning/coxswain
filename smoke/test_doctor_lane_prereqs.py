"""`cox setup doctor` names a lane host's smoke-gate gaps (`agent_tools.lane_prereqs`), end to end.

A real installed `cox`, a real profile, and a `docker` on the PATH ahead of any other whose daemon does not answer, the
way Docker Desktop's CLI answers while the app is stopped. The doctor must name that gap, advertise no capability, and
answer the same on a second run. The verdicts themselves have unit tests in src/tools/tests/test_lane_prereqs.py.
"""

import json
import stat
from dataclasses import replace

LANE_CHECKS = ("docker daemon", "smoke image postgres", "smoke image forgejo", "smoke image garage", "pg_dump", "psql", "gh")
DOCKER_DOWN = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"


def _lane_rows(stdout: str) -> list[dict]:
    return [row for row in json.loads(stdout)["rows"] if row["check"] in LANE_CHECKS]


def test_the_doctor_names_a_stopped_docker_daemon_and_advertises_no_capability(sandbox, tmp_path):
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    docker = fake / "docker"
    docker.write_text(f'#!/bin/sh\necho "{DOCKER_DOWN}" >&2\nexit 1\n', encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    stopped = replace(sandbox, env={**sandbox.env, "PATH": f"{fake}:{sandbox.env['PATH']}"})

    first, second = (stopped.cox("setup", "doctor", "--json", timeout=300) for _ in range(2))

    report = json.loads(first.stdout)
    rows = {row["check"]: row for row in report["rows"]}
    assert first.returncode == 1
    assert rows["docker daemon"] == {
        "check": "docker daemon", "ok": False,
        "detail": f"the docker daemon did not answer (exit 1): {DOCKER_DOWN}; start it",
    }
    assert rows["smoke image postgres"]["detail"] == "not checked: the docker daemon did not answer"
    assert report["lane_capabilities"] == []
    assert _lane_rows(second.stdout) == _lane_rows(first.stdout)
