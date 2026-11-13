"""MkDocs hook: generated pages join the nav under the page they belong to, so they get previous and next links.

`docs/_pull.py` copies every component's docs to docs/components/<name>/..., and `docs/_cli.py` writes one page
per command under docs/reference/cli/, but the nav in mkdocs.yml lists only each component's index page and the
CLI reference index. MkDocs gives a page outside the nav no previous or next page, so none of them could be
stepped through (2026-10-23). `on_nav` turns each such nav entry into a section whose index is that page (the
theme's `navigation.indexes` makes the section title link to it, so it is not listed twice), followed by its
generated pages, and recomputes the previous and next links over the whole nav.

A component's pulled README is titled README, not its repository-style lowercase heading, which read as a second
copy of the section. Pages under a directory in `SUBSECTIONS` are grouped into a named subsection (the graphs'
generated reference pages, and their specifications), so a graph's two pages no longer sit side by side under the
same name.

`generated_dir`, `under`, `grouped` and `page_title` are the pure part: which directory belongs to a page, in what
order its pages come, which subsection each joins, and what each is called.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from mkdocs.structure.nav import Navigation, Section
from mkdocs.structure.pages import Page

COMPONENTS_DIR = "components"
README = "readme.md"
# The CLI reference index; docs/_cli.py writes one page per command beside it at build time.
CLI_INDEX = "reference/cli/index.md"
# (path prefix under a component's directory, subsection title, a suffix its page headings carry that the
# subsection title already says). First match wins.
SUBSECTIONS: tuple[tuple[str, str, str], ...] = (
    ("docs/graphs/", "Graph reference", ""),
    ("graphs/", "Specifications", " — specification"),
)
# A subsection's own index page is titled this, so it never repeats the component's name.
SUBSECTION_INDEX_TITLE = "All graphs"


def component_of(src_uri: str) -> str | None:
    """`tools` for components/tools/index.md, the page a component's pulled docs sit under; None otherwise."""
    parts = src_uri.split("/")
    return parts[1] if len(parts) == 3 and parts[0] == COMPONENTS_DIR and parts[2] == "index.md" else None


def generated_dir(src_uri: str) -> str | None:
    """The directory whose generated pages sit under this nav page: components/<name>/ for a component's index,
    reference/cli/ for the CLI reference index; None for any other page."""
    component = component_of(src_uri)
    if component is not None:
        return f"{COMPONENTS_DIR}/{component}/"
    return posixpath.dirname(CLI_INDEX) + "/" if src_uri == CLI_INDEX else None


def under(prefix: str, src_uris: Iterable[str]) -> list[str]:
    """The pages under prefix, its readme.md first, then by path."""
    pulled = [uri for uri in src_uris if uri.startswith(prefix) and uri.endswith(".md")]
    return sorted(pulled, key=lambda uri: (uri != prefix + README, uri))


def _subsection(prefix: str, uri: str) -> str | None:
    rest = uri[len(prefix):]
    return next((title for start, title, _ in SUBSECTIONS if rest.startswith(start)), None)


def grouped(prefix: str, src_uris: Sequence[str]) -> list[tuple[str | None, list[str]]]:
    """src_uris (already ordered) as (subsection title, or None for the top level, and its pages): the top level
    first, then each subsection in SUBSECTIONS order with its own index.md leading it; empty groups left out."""
    top = [uri for uri in src_uris if _subsection(prefix, uri) is None]
    subs = [
        (title, sorted((u for u in src_uris if _subsection(prefix, u) == title), key=lambda u: not u.endswith("/index.md")))
        for _, title, _ in SUBSECTIONS
    ]
    return [(None, top), *((title, pages) for title, pages in subs if pages)]


def page_title(src_uri: str, heading: str | None) -> str | None:
    """The nav title for a generated page, or None to keep its own heading: README for a readme, the subsection
    index title for a subsection's index.md, a heading without the suffix its subsection title already says."""
    if src_uri.endswith("/" + README):
        return "README"
    if src_uri.endswith("/index.md") and component_of(src_uri) is None and src_uri != CLI_INDEX:
        return SUBSECTION_INDEX_TITLE
    for _, _, suffix in SUBSECTIONS:
        if suffix and heading and heading.endswith(suffix):
            return heading[: -len(suffix)]
    return None


def _heading(page: Page) -> str | None:
    """The page's first `# ` heading, read from its source: a page's own title is not known until it renders."""
    try:
        text = Path(page.file.abs_src_path).read_text(encoding="utf-8")
    except (OSError, TypeError):
        return None
    match = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def _pages(items: Sequence) -> list[Page]:
    found: list[Page] = []
    for item in items:
        if isinstance(item, Page):
            found.append(item)
        elif isinstance(item, Section):
            found.extend(_pages(item.children))
    return found


def _link(pages: Sequence[Page]) -> None:
    for before, page, after in zip([None, *pages[:-1]], pages, [*pages[1:], None], strict=True):
        page.previous_page = before
        page.next_page = after


def _section(title: str, children: list, parent) -> Section:
    section = Section(title, children)
    section.parent = parent
    for child in children:
        child.parent = section
    return section


def on_nav(nav: Navigation, config, files) -> Navigation:
    by_uri = {f.src_uri: f.page for f in files.documentation_pages() if f.page is not None}
    in_nav = {page.file.src_uri for page in nav.pages}
    for section in (item for item in nav.items if isinstance(item, Section)):
        for index, child in enumerate(section.children):
            prefix = generated_dir(child.file.src_uri) if isinstance(child, Page) else None
            uris = [uri for uri in under(prefix, by_uri) if uri not in in_nav] if prefix else []
            if not uris:
                continue
            for uri in uris:
                title = page_title(uri, _heading(by_uri[uri]))
                if title is not None:
                    by_uri[uri].title = title
            group = _section(child.title or prefix.rstrip("/"), [child], section)
            for title, pages in grouped(prefix, uris):
                members = [by_uri[uri] for uri in pages]
                if title is None:
                    group.children.extend(members)
                    for member in members:
                        member.parent = group
                else:
                    group.children.append(_section(title, members, group))
            section.children[index] = group
    nav.pages = _pages(nav.items)
    _link(nav.pages)
    return nav
