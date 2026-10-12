"""The blank markers a notes draft leaves for a human, and the pure functions that find and fill them."""

OPENER_BLANK = "<!-- BLANK:opener write an opener of 3 to 5 sentences introducing this release -->"
GAPS_BLANK = "<!-- BLANK:known-gaps write one paragraph on what this release does not do yet -->"

_BLANKS = (("opener", OPENER_BLANK), ("known-gaps", GAPS_BLANK))


def find_blanks(text: str) -> tuple[str, ...]:
    """Names of the blanks still present, in the fixed order opener then known-gaps."""
    return tuple(name for name, marker in _BLANKS if marker in text)


def replace_blank(text: str, name: str, content: str) -> str:
    """Text with the named marker replaced by content; unchanged when the name or marker is absent."""
    marker = dict(_BLANKS).get(name)
    if marker is None:
        return text
    return text.replace(marker, content)
