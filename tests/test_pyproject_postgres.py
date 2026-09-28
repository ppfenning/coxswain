import tomllib
from pathlib import Path


def test_the_umbrella_installs_cox_with_the_postgres_extra():
    """A lane building in the umbrella runs `cox` from this venv, and the fleet's store is Postgres."""
    groups = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())["dependency-groups"]
    deps = [d for group in groups.values() for d in group if isinstance(d, str)]
    assert any(d.startswith("coxswain-tools[postgres]") for d in deps)
