"""Every seat carries the frontmatter a harness routes on, and an explicit model.

The tool allowlist IS the write boundary, and `model: inherit` puts whatever
the main loop happens to be running on a job that did not ask for it. Both are
mechanical, so both are checked mechanically rather than trusted to review.
"""
import pathlib
import sys

REQUIRED = ("name", "description", "tools", "model")
problems = []

for path in sorted(pathlib.Path("seats").glob("*.md")):
    if path.name == "SEATS.md":
        continue
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        problems.append(f"{path}: no frontmatter block")
        continue
    front = text.split("---\n", 2)[1]
    keys = {line.split(":", 1)[0] for line in front.splitlines() if line and not line.startswith((" ", "\t", "-"))}
    for key in REQUIRED:
        if key not in keys:
            problems.append(f"{path}: frontmatter has no '{key}'")
    if "model: inherit" in front:
        problems.append(f"{path}: model must be explicit, never 'inherit'")
    name = next((l.split(":", 1)[1].strip() for l in front.splitlines() if l.startswith("name:")), "")
    if name != path.stem:
        problems.append(f"{path}: name '{name}' does not match the filename")
    if "voice is" not in text and "In a text session" not in text:
        problems.append(f"{path}: does not say the voice is optional")

print("\n".join(problems) if problems else f"{len(list(pathlib.Path('seats').glob('*.md'))) - 1} seats OK")
sys.exit(1 if problems else 0)
