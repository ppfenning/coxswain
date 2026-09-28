import re
from pathlib import Path

import yaml

DOCS = Path(__file__).resolve().parent.parent / "docs" / "customize" / "dashboards.md"
DATASETS_FILE = Path(__file__).resolve().parent.parent / "deploy" / "superset" / "dashboards" / "datasets.yaml"

_FEEDS = re.compile(r"`([a-z_][a-z0-9_]*)` feeds \*\*.+?\*\*:\n\n```sql\n(.*?)\n```", re.DOTALL)
_BARE = re.compile(r"reads the\s+`([a-z_][a-z0-9_]*)`\s+dataset")


def _section(heading: str) -> str:
    text = DOCS.read_text()
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"no '## {heading}' section in {DOCS}"
    return match.group(1)


def _datasets() -> dict[str, dict]:
    specs = yaml.safe_load(DATASETS_FILE.read_text())
    return {d["name"]: d for d in specs["datasets"]}


def _named(section: str) -> dict[str, str | None]:
    named: dict[str, str | None] = dict(_FEEDS.findall(section))
    for name in _BARE.findall(section):
        named.setdefault(name, None)
    return named


def _assert_section_matches_datasets(heading: str, expected_names: set[str]) -> None:
    section = _section(heading)
    datasets = _datasets()
    named = _named(section)
    assert set(named) == expected_names
    for name, sql in named.items():
        assert name in datasets, f"{name} is not a dataset in {DATASETS_FILE}"
        if sql is not None:
            assert sql.strip() == datasets[name]["sql"].strip()


def test_the_fleet_section_quotes_its_new_datasets_sql_verbatim():
    _assert_section_matches_datasets(
        "Fleet",
        {
            "chair_lanes_by_host_hour",
            "fleet_host_state",
            "fleet_needs_chair_by_cause",
            "fleet_weekly_spend",
            "chair_status",
        },
    )


def test_the_chair_section_names_only_datasets_that_exist():
    _assert_section_matches_datasets(
        "Chair",
        {"chair_actions_by_hour", "chair_spend_by_day", "chair_needs_backlog"},
    )
