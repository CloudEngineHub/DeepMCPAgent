"""Search titles for the docs pages.

MkDocs sets ``page.title`` from the nav label when the nav names one, so a page
listed as "Runtime" in the nav would be titled "Runtime" in search results even
though its heading says "Runtime API Reference". This hook gives every page a
title built from the page itself, which ``overrides/main.html`` uses for
``<title>``, Open Graph, Twitter and JSON-LD:

* ``seo_title`` front matter, if the page sets one (the search title, without
  the site suffix);
* else ``title`` front matter;
* else the page's first ``# `` heading;
* else the nav title.

" | Promptise Docs" is appended when the whole title stays within 65
characters, so a search for "promptise docs" matches the page; a longer title
gets " | Promptise", and one that is already 65 characters gets nothing (search
engines cut titles there). The home page uses ``seo_title`` as is.
"""

import re

SUFFIXES = (" | Promptise Docs", " | Promptise")
MAX_TITLE = 65
HOME_TITLE = "Promptise Docs: Build AI Agents and MCP Servers in Python"

_H1 = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKUP = re.compile(r"[`*]|<[^>]+>|\{[^}]*\}")


def _plain(text: str) -> str:
    """Heading text without Markdown links, bold/italic stars, code marks, HTML or attr lists."""
    return " ".join(_MARKUP.sub("", _LINK.sub(r"\1", text)).split())


def _first_heading(markdown: str) -> str | None:
    match = _H1.search(_FENCE.sub("", markdown))
    return _plain(match.group(1)) if match else None


def on_page_markdown(markdown, page, config, files):
    meta = page.meta
    # page.is_homepage is False here: index.md sits under "Getting Started" in the nav.
    if page.file.src_uri == "index.md":
        base = str(meta.get("seo_title") or HOME_TITLE)
        full = base
    else:
        base = _plain(str(meta.get("seo_title") or meta.get("title") or _first_heading(markdown) or page.title or ""))
        full = next((base + s for s in SUFFIXES if len(base + s) <= MAX_TITLE), base) if base else config["site_name"]
    meta["seo_title"] = base
    meta["seo_title_full"] = full
    return markdown
