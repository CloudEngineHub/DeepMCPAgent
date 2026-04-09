"""Live e2e test: real agent + 40-tool MCP server + semantic selection.

Runs every query TWICE — once with all 40 tools (no optimization) and
once with semantic selection (top-8). Compares real token counts from
the OpenAI API to prove semantic selection saves tokens while still
picking the correct tool.

Usage:
    .venv/bin/python tests/run_semantic_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

if not os.getenv("OPENAI_API_KEY"):
    print("ERROR: OPENAI_API_KEY not set. Export it or add to .env")
    sys.exit(1)


# ── Helpers ──────────────────────────────────────────────────────────


def extract_tool_calls(result: dict) -> list[dict]:
    """Extract tool name, args, and result from agent messages."""
    calls: list[dict] = []
    for msg in result.get("messages", []):
        msg_type = getattr(msg, "type", "")
        if msg_type == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                calls.append(
                    {
                        "name": tc.get("name", "?"),
                        "args": tc.get("args", {}),
                        "result": None,
                    }
                )
        if msg_type == "tool" and calls and calls[-1]["result"] is None:
            calls[-1]["result"] = getattr(msg, "content", "")[:300]
    return calls


def tool_names(calls: list[dict]) -> list[str]:
    return [c["name"] for c in calls]


def get_final_response(result: dict) -> str:
    """Get the agent's final text response."""
    for msg in reversed(result.get("messages", [])):
        if getattr(msg, "type", "") == "ai" and getattr(msg, "content", ""):
            return msg.content[:300]
    return "(no response)"


def get_token_counts(agent) -> dict:
    """Read prompt/completion/total tokens from the observability handler."""
    stats = agent.get_stats()
    return {
        "prompt": stats.get("total_prompt_tokens", 0),
        "completion": stats.get("total_completion_tokens", 0),
        "total": stats.get("total_tokens", 0),
    }


def reset_token_counts(agent) -> None:
    """Reset the callback handler counters between queries."""
    h = agent._handler
    if h is None:
        return
    h.total_prompt_tokens = 0
    h.total_completion_tokens = 0
    h.total_tokens = 0
    h.llm_call_count = 0
    h.tool_call_count = 0


# ── Main ─────────────────────────────────────────────────────────────


async def main():
    from promptise import build_agent
    from promptise.config import StdioServerSpec

    server_script = os.path.join(os.path.dirname(__file__), "_e2e_40tool_server.py")

    print("=" * 78)
    print("  SEMANTIC TOOL SELECTION — TOKEN COMPARISON TEST")
    print("  40-tool MCP server | OpenAI gpt-4o-mini | observe=True")
    print("=" * 78)
    print()

    # ── Build TWO agents: one with semantic, one without ─────────────
    print("[1/4] Building BASELINE agent (40 tools, no optimization)...")
    agent_full = await build_agent(
        servers={
            "biz": StdioServerSpec(command=sys.executable, args=[server_script]),
        },
        model="openai:gpt-4o-mini",
        instructions=(
            "You are a business operations assistant. "
            "Always call the most appropriate tool. Do not answer from memory."
        ),
        observe=True,
    )

    print("[2/4] Building SEMANTIC agent (top-8 from 40, optimize_tools='semantic')...")
    agent_semantic = await build_agent(
        servers={
            "biz": StdioServerSpec(command=sys.executable, args=[server_script]),
        },
        model="openai:gpt-4o-mini",
        instructions=(
            "You are a business operations assistant. "
            "Always call the most appropriate tool. Do not answer from memory."
        ),
        observe=True,
        optimize_tools="semantic",
    )

    indexed = len(agent_semantic._tool_index.all_tool_names) if agent_semantic._tool_index else 0
    print(f"[3/4] Baseline: 40 tools bound | Semantic: {indexed} indexed, top-8 per query")
    print()

    # ── List all 40 tools by domain ──────────────────────────────────
    all_names = (
        sorted(agent_semantic._tool_index.all_tool_names) if agent_semantic._tool_index else []
    )
    domains_map: dict[str, list[str]] = {}
    for name in all_names:
        d = name.split("_")[0]
        domains_map.setdefault(d, []).append(name)

    print(f"  ALL {len(all_names)} TOOLS DISCOVERED FROM MCP SERVER:")
    print(f"  {'─' * 60}")
    for d in sorted(domains_map):
        tools_in_d = domains_map[d]
        print(f"  {d.upper():12s} ({len(tools_in_d)} tools): {', '.join(tools_in_d)}")
    print()
    print("[4/4] Running queries side-by-side...\n")

    queries = [
        ("HR", "List all employees in the engineering department"),
        ("FINANCE", "Create an invoice for Acme Corp for $15,000 for Q1 consulting"),
        ("IT", "Restart the production API server right now"),
        ("INVENTORY", "Check the current stock level for product SKU-7890"),
        ("CALENDAR", "Find free time slots on 2026-04-01 for a 45-minute meeting"),
        ("SUPPORT", "Escalate ticket TK-3344 because the customer has been waiting 5 days"),
    ]

    total_full_prompt = 0
    total_sem_prompt = 0
    total_full_all = 0
    total_sem_all = 0
    results_table: list[dict] = []

    for domain, query in queries:
        prefix = domain.lower() + "_"

        # ── Run BASELINE (all 40 tools) ──────────────────────────────
        reset_token_counts(agent_full)
        result_full = await agent_full.ainvoke({"messages": [{"role": "user", "content": query}]})
        tokens_full = get_token_counts(agent_full)
        calls_full = extract_tool_calls(result_full)
        response_full = get_final_response(result_full)
        ok_full = any(t.startswith(prefix) for t in tool_names(calls_full))

        # ── Run SEMANTIC (top-8 tools) ───────────────────────────────
        reset_token_counts(agent_semantic)
        result_sem = await agent_semantic.ainvoke(
            {"messages": [{"role": "user", "content": query}]}
        )
        tokens_sem = get_token_counts(agent_semantic)
        calls_sem = extract_tool_calls(result_sem)
        response_sem = get_final_response(result_sem)
        ok_sem = any(t.startswith(prefix) for t in tool_names(calls_sem))

        # ── Compute savings ──────────────────────────────────────────
        prompt_saved = tokens_full["prompt"] - tokens_sem["prompt"]
        pct = (prompt_saved / tokens_full["prompt"] * 100) if tokens_full["prompt"] else 0

        total_full_prompt += tokens_full["prompt"]
        total_sem_prompt += tokens_sem["prompt"]
        total_full_all += tokens_full["total"]
        total_sem_all += tokens_sem["total"]

        results_table.append(
            {
                "correct_full": ok_full,
                "correct_sem": ok_sem,
            }
        )

        # ── Print full details ───────────────────────────────────────
        status_f = "OK" if ok_full else "WRONG"
        status_s = "OK" if ok_sem else "WRONG"

        print(f"{'━' * 78}")
        print(f"  QUERY [{domain}]: {query}")
        print(f'  EXPECTED: tool name starting with "{prefix}*"')
        print(f"{'━' * 78}")
        print()

        # Baseline
        print(
            f"  ┌─ BASELINE (40 tools) ── {tokens_full['prompt']} prompt / {tokens_full['total']} total tokens"
        )
        for tc in calls_full:
            match = "MATCH" if tc["name"].startswith(prefix) else "MISS"
            print(f"  │  Tool call: {tc['name']}({tc['args']})  [{match}]")
            if tc["result"]:
                print(f"  │  Tool returned: {tc['result']}")
        if not calls_full:
            print("  │  (no tool called)  [MISS]")
        print(f"  │  Agent response: {response_full}")
        print(f"  │  Result: [{status_f}]")
        print(f"  └{'─' * 77}")
        print()

        # Semantic
        print(
            f"  ┌─ SEMANTIC (top-8 tools) ── {tokens_sem['prompt']} prompt / {tokens_sem['total']} total tokens"
        )
        for tc in calls_sem:
            match = "MATCH" if tc["name"].startswith(prefix) else "MISS"
            print(f"  │  Tool call: {tc['name']}({tc['args']})  [{match}]")
            if tc["result"]:
                print(f"  │  Tool returned: {tc['result']}")
        if not calls_sem:
            print("  │  (no tool called)  [MISS]")
        print(f"  │  Agent response: {response_sem}")
        print(f"  │  Result: [{status_s}]")
        print(f"  └{'─' * 77}")
        print()
        print(f"  TOKEN SAVINGS: {prompt_saved:,} prompt tokens saved ({pct:.0f}% reduction)")
        print()

    # ── Shutdown ─────────────────────────────────────────────────────
    await agent_full.shutdown()
    await agent_semantic.shutdown()

    # ── Summary table ────────────────────────────────────────────────
    overall_pct = (
        (total_full_prompt - total_sem_prompt) / total_full_prompt * 100 if total_full_prompt else 0
    )

    correct_full = sum(1 for r in results_table if r["correct_full"])
    correct_sem = sum(1 for r in results_table if r["correct_sem"])
    n = len(queries)

    print("=" * 78)
    print("  SUMMARY")
    print("=" * 78)
    print()
    print(f"  {'':30s} {'BASELINE':>12s}  {'SEMANTIC':>12s}  {'SAVED':>12s}")
    print(f"  {'─' * 68}")
    print(
        f"  {'Total prompt tokens':30s} {total_full_prompt:>12,}  {total_sem_prompt:>12,}  {total_full_prompt - total_sem_prompt:>12,}"
    )
    print(
        f"  {'Total tokens (prompt+compl.)':30s} {total_full_all:>12,}  {total_sem_all:>12,}  {total_full_all - total_sem_all:>12,}"
    )
    print(
        f"  {'Avg prompt tokens / query':30s} {total_full_prompt // n:>12,}  {total_sem_prompt // n:>12,}  {(total_full_prompt - total_sem_prompt) // n:>12,}"
    )
    print(f"  {'Prompt token reduction':30s} {'':>12s}  {'':>12s}  {overall_pct:>11.1f}%")
    print(f"  {'Correct tool selected':30s} {correct_full:>10}/{n}    {correct_sem:>10}/{n}")
    print()

    if correct_sem == n:
        print(
            "  RESULT: Semantic selection saved tokens while selecting the correct tool every time."
        )
    elif correct_sem >= correct_full:
        print(
            f"  RESULT: Semantic selection matched or beat baseline accuracy ({correct_sem}/{n} vs {correct_full}/{n})."
        )
    else:
        print(
            f"  WARNING: Semantic selection missed some tools ({correct_sem}/{n} vs {correct_full}/{n})."
        )

    print()
    print("=" * 78)

    if correct_sem < n:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
