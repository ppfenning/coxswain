import re
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent.parent / "docs" / "releases" / "index.md"


def test_the_documented_release_command_runs_frozen():
    (command,) = re.findall(r"^uv run .*python -m devtools release .*$", INDEX.read_text(), re.M)
    assert command.startswith("uv run --frozen ")
