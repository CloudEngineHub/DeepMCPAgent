"""Generate llms.txt and llms-full.txt from the docs nav.

llms.txt lists every page in the nav (title, URL and description from the
page's front matter). llms-full.txt holds the text of the reference pages, in
nav order, for tools that read a whole documentation set at once.

Run from the repository root after changing the docs:

    python scripts/build_llms_txt.py
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SITE = "https://docs.promptise.com/"

SUMMARY = """\
# Promptise Foundry

> Promptise Foundry is a Python framework for agentic AI systems: agents
> (`build_agent()`), the tools they call (`@tool` functions and MCP servers built
> with the MCP server SDK or generated from an OpenAPI spec with MCPcast), a
> reasoning graph engine, prompt engineering, agent identity and an agent runtime
> for long-running agents with triggers, journals and governance.
> Install with `pip install promptise` (Python 3.10+).

Key facts:

- `await build_agent(model="openai:gpt-5-mini", extra_tools=[...], servers={...})`
  returns a `PromptiseAgent`; run it with `await agent.ainvoke({"messages": [...]})`
  or `await agent.chat(message, session_id=...)`, and `await agent.shutdown()`.
- Python functions become tools with `from promptise import tool`. MCP servers are
  connected with `HTTPServerSpec` / `StdioServerSpec` from `promptise.config`.
- Models are a `"provider:model"` string or `Model(...)`; keys come from `.env`,
  the environment or code. `promptise models check <model>` explains what is missing.
- Every feature is a keyword argument of `build_agent()` and off by default:
  memory, conversation_store, guardrails, cache, approval, observe, sandbox,
  cross_agents, agent_pattern, optimize_tools, events, adaptive, context_engine.
"""

# Sections whose pages go into llms-full.txt.
FULL_SECTIONS = {
    "Getting Started",
    "Agent",
    "Reasoning Graph",
    "MCP",
    "MCPcast",
    "Agent Runtime",
    "Agent Identity",
    "Prompting & Context",
}
SKIP_IN_FULL = {"resources/changelog.md"}


class _Loader(yaml.SafeLoader):
    pass


def _ignore_python_tags(loader, tag_suffix, node):  # mkdocs.yml has !!python/name tags
    return None


_Loader.add_multi_constructor("tag:yaml.org,2002:python/", _ignore_python_tags)


def _url(path: str) -> str:
    if path == "index.md":
        return SITE
    path = path[:-3]
    if path.endswith("/index"):
        path = path[: -len("index")]
    else:
        path += "/"
    return SITE + path


def _split_front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            meta = yaml.safe_load(text[4:end]) or {}
            return meta, text[end + 5 :]
    return {}, text


def _resolve_snippets(body: str) -> str:
    def repl(match: re.Match[str]) -> str:
        target = ROOT / match.group(2)
        return match.group(1) + target.read_text() if target.exists() else match.group(0)

    return re.sub(r'^([ \t]*)--8<--\s*"([^"]+)"\s*$', repl, body, flags=re.M)


def _title(meta: dict, body: str, fallback: str) -> str:
    if meta.get("seo_title"):
        return str(meta["seo_title"])
    match = re.search(r"^# (.+)$", body, re.M)
    return match.group(1).strip() if match else fallback


def _walk(items, section=None):
    for item in items:
        if isinstance(item, str):
            yield section, None, item
            continue
        for label, value in item.items():
            if isinstance(value, str):
                yield section, label, value
            else:
                yield from _walk(value, section or label)


def main() -> None:
    config = yaml.load((ROOT / "mkdocs.yml").read_text(), Loader=_Loader)
    index_lines = [SUMMARY]
    full_lines = [SUMMARY, ""]
    current = None
    seen: set[str] = set()
    for section, label, path in _walk(config["nav"]):
        if not path.endswith(".md") or path in seen or path.startswith("blog/"):
            continue
        seen.add(path)
        meta, body = _split_front_matter((DOCS / path).read_text())
        title = _title(meta, body, label or path)
        if section != current:
            index_lines.append(f"\n## {section}\n")
            current = section
        description = str(meta.get("description", "")).strip()
        index_lines.append(f"- [{title}]({_url(path)})" + (f": {description}" if description else ""))
        if section in FULL_SECTIONS and path not in SKIP_IN_FULL and not path.startswith("api/"):
            full_lines += ["", "=" * 80, f"Page: {title}", f"URL: {_url(path)}", "=" * 80, ""]
            full_lines.append(_resolve_snippets(body).strip())
    (ROOT / "llms.txt").write_text("\n".join(index_lines) + "\n")
    (ROOT / "llms-full.txt").write_text("\n".join(full_lines) + "\n")
    print(f"llms.txt: {len(seen)} pages; llms-full.txt: {(ROOT / 'llms-full.txt').stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
