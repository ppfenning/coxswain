import ast
import re
import runpy
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "superset"
COMPOSE_TEXT = (DEPLOY / "docker-compose.yml").read_text()
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
CONFIG = DEPLOY / "superset_config.py"
INIT_SCRIPT = COMPOSE["services"]["superset-init"]["command"][-1]
ADMIN_VARS = ("SUPERSET_ADMIN_USERNAME", "SUPERSET_ADMIN_PASSWORD", "SUPERSET_ADMIN_EMAIL", "SUPERSET_ADMIN_FIRSTNAME", "SUPERSET_ADMIN_LASTNAME")
REQUIRED = ("SUPERSET_SECRET_KEY", *ADMIN_VARS)


def _load_config(monkeypatch, env):
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return runpy.run_path(str(CONFIG))


def test_compose_parses_with_the_superset_and_init_services():
    assert {"superset", "superset-init"} <= set(COMPOSE["services"])


def test_runs_directory_is_mounted_read_only_at_data_runs():
    mounts = [v for s in COMPOSE["services"].values() for v in s["volumes"] if ":/data/runs" in v]
    assert mounts
    assert all(re.fullmatch(r"\$\{COXSWAIN_RUNS_DIR[^}]*\}:/data/runs:ro", m) for m in mounts)


def test_every_port_binds_to_loopback_only():
    ports = [p for s in COMPOSE["services"].values() for p in s.get("ports", [])]
    assert ports
    assert all(str(p).startswith("127.0.0.1:") for p in ports)


def test_every_compose_variable_appears_in_env_example():
    used = set(re.findall(r"\$\{(\w+)", COMPOSE_TEXT))
    declared = set(re.findall(r"^(\w+)=", (DEPLOY / ".env.example").read_text(), re.M))
    assert used
    assert used <= declared


def test_every_compose_variable_is_required_so_compose_refuses_an_unset_one():
    refs = re.findall(r"\$\{(\w+)([^}]*)\}", COMPOSE_TEXT)
    assert {name for name, _ in refs} >= set(REQUIRED) | {"COXSWAIN_RUNS_DIR"}
    assert all(mod.startswith(":?") for _, mod in refs)


@pytest.mark.parametrize("missing", REQUIRED)
def test_config_fails_fast_naming_each_missing_variable(monkeypatch, missing):
    with pytest.raises(RuntimeError, match=missing):
        _load_config(monkeypatch, {n: f"value-of-{n}" for n in REQUIRED if n != missing})


def test_config_rejects_the_env_example_placeholder(monkeypatch):
    with pytest.raises(RuntimeError, match="SUPERSET_ADMIN_PASSWORD"):
        _load_config(monkeypatch, {n: "change-me" if n == "SUPERSET_ADMIN_PASSWORD" else f"value-of-{n}" for n in REQUIRED})


def test_secret_key_comes_only_from_the_environment(monkeypatch):
    (assign,) = [n for n in ast.walk(ast.parse(CONFIG.read_text())) if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "SECRET_KEY" for t in n.targets)]
    assert ast.unparse(assign.value) == "_require('SUPERSET_SECRET_KEY')"
    assert _load_config(monkeypatch, {n: f"value-of-{n}" for n in REQUIRED})["SECRET_KEY"] == "value-of-SUPERSET_SECRET_KEY"


def test_init_fails_when_the_admin_is_absent_after_create_admin():
    assert "|| echo" not in INIT_SCRIPT
    assert "set -e" in INIT_SCRIPT
    assert re.search(r"if ! admin_exists; then\s+echo [^\n]*\n\s+exit 1", INIT_SCRIPT)


def test_dockerfile_pins_its_base_image_tag():
    (base,) = re.findall(r"^FROM\s+(\S+)", (DEPLOY / "Dockerfile").read_text(), re.M)
    assert re.fullmatch(r"apache/superset:[45]\.\d+\.\d+", base)
