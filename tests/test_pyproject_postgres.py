import tomllib
from pathlib import Path


def test_the_dev_venv_carries_the_postgres_driver():
    """A lane building here runs `cox` from this venv, and the fleet's store is Postgres."""
    groups = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())["dependency-groups"]
    deps = [d for group in groups.values() for d in group if isinstance(d, str)]
    assert any(d.startswith("psycopg[binary]") for d in deps)
