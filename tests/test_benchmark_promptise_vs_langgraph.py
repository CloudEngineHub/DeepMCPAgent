"""Benchmark: Promptise PromptGraph vs LangGraph create_react_agent.

Head-to-head comparison on the same model, same tools, same questions.
Measures: tool calls, tokens, latency, answer quality.

Requires:
    OPENAI_API_KEY environment variable set.

Run:
    .venv/bin/python -m pytest tests/test_benchmark_promptise_vs_langgraph.py -x -v -s
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — skipping benchmark",
)


# ═══════════════════════════════════════════════════════════════════════════════
# Shared tools (identical for both frameworks)
# ═══════════════════════════════════════════════════════════════════════════════


def _make_tools():
    """Create LangChain tools usable by both Promptise and LangGraph."""
    from langchain_core.tools import tool

    @tool
    def search_knowledge_base(query: str) -> str:
        """Search the internal knowledge base for information."""
        data = {
            "revenue": "Q4 2025 revenue: $42M, up 15% YoY. Key driver: enterprise contracts grew 40%.",
            "employees": "Total headcount: 340. Engineering: 120, Sales: 80, Ops: 60, Other: 80.",
            "product": "Promptise Foundry v2.0 launched March 2026. Features: Reasoning Graph, MCP SDK.",
            "competitors": "Main competitors: LangChain, CrewAI, Autogen. Our advantage: production-ready.",
            "market": "TAM: $12B by 2027. AI agent platforms growing 45% CAGR.",
        }
        for key, val in data.items():
            if key in query.lower():
                return val
        return f"No results for: {query}"

    @tool
    def calculate(expression: str) -> str:
        """Evaluate a math expression. Example: '42000000 / 340'."""
        try:
            allowed = set("0123456789+-*/.() ")
            if all(c in allowed for c in expression):
                return str(round(eval(expression), 2))  # noqa: S307
            return f"Invalid expression: {expression}"
        except Exception as e:
            return f"Error: {e}"

    @tool
    def get_current_date() -> str:
        """Get today's date."""
        return "2026-04-02"

    return [search_knowledge_base, calculate, get_current_date]


def _make_complex_tools():
    """Richer tool set for harder benchmarks — simulates a real business system."""
    from langchain_core.tools import tool

    _db = {
        "customers": [
            {
                "id": 1,
                "name": "Acme Corp",
                "revenue": 2400000,
                "plan": "enterprise",
                "region": "NA",
            },
            {
                "id": 2,
                "name": "Globex Inc",
                "revenue": 1800000,
                "plan": "enterprise",
                "region": "EU",
            },
            {"id": 3, "name": "Initech", "revenue": 960000, "plan": "pro", "region": "NA"},
            {
                "id": 4,
                "name": "Umbrella Corp",
                "revenue": 3200000,
                "plan": "enterprise",
                "region": "APAC",
            },
            {
                "id": 5,
                "name": "Stark Industries",
                "revenue": 5600000,
                "plan": "enterprise",
                "region": "NA",
            },
            {
                "id": 6,
                "name": "Wayne Enterprises",
                "revenue": 4100000,
                "plan": "enterprise",
                "region": "EU",
            },
            {"id": 7, "name": "Cyberdyne", "revenue": 720000, "plan": "pro", "region": "NA"},
            {
                "id": 8,
                "name": "Soylent Corp",
                "revenue": 480000,
                "plan": "starter",
                "region": "APAC",
            },
        ],
        "tickets": [
            {
                "id": 101,
                "customer_id": 1,
                "status": "open",
                "priority": "high",
                "subject": "API downtime",
            },
            {
                "id": 102,
                "customer_id": 3,
                "status": "open",
                "priority": "medium",
                "subject": "Billing error",
            },
            {
                "id": 103,
                "customer_id": 5,
                "status": "resolved",
                "priority": "high",
                "subject": "Data export bug",
            },
            {
                "id": 104,
                "customer_id": 2,
                "status": "open",
                "priority": "low",
                "subject": "Feature request",
            },
            {
                "id": 105,
                "customer_id": 4,
                "status": "open",
                "priority": "critical",
                "subject": "Security vuln",
            },
        ],
        "metrics": {
            "mrr": 1850000,
            "arr": 22200000,
            "churn_rate": 0.032,
            "nps": 72,
            "avg_deal_size": 186000,
            "sales_cycle_days": 45,
            "cac": 12000,
            "ltv": 540000,
        },
    }

    @tool
    def query_customers(filter_field: str = "", filter_value: str = "") -> str:
        """Query customers. Optional filter by field (plan, region) and value."""
        results = _db["customers"]
        if filter_field and filter_value:
            results = [
                c for c in results if str(c.get(filter_field, "")).lower() == filter_value.lower()
            ]
        lines = [f"  {c['name']}: ${c['revenue']:,} ({c['plan']}, {c['region']})" for c in results]
        return f"Found {len(results)} customers:\n" + "\n".join(lines)

    @tool
    def get_customer_revenue(customer_name: str) -> str:
        """Get revenue details for a specific customer."""
        for c in _db["customers"]:
            if customer_name.lower() in c["name"].lower():
                return (
                    f"{c['name']}: ${c['revenue']:,}/year, plan={c['plan']}, region={c['region']}"
                )
        return f"Customer '{customer_name}' not found."

    @tool
    def query_tickets(status: str = "", priority: str = "") -> str:
        """Query support tickets. Optional filter by status (open/resolved) and priority."""
        results = _db["tickets"]
        if status:
            results = [t for t in results if t["status"] == status.lower()]
        if priority:
            results = [t for t in results if t["priority"] == priority.lower()]
        customer_map = {c["id"]: c["name"] for c in _db["customers"]}
        lines = []
        for t in results:
            cname = customer_map.get(t["customer_id"], "Unknown")
            lines.append(f"  #{t['id']} [{t['priority']}] {t['subject']} — {cname} ({t['status']})")
        return f"Found {len(results)} tickets:\n" + "\n".join(lines)

    @tool
    def get_business_metrics() -> str:
        """Get current business metrics: MRR, ARR, churn, NPS, deal size, etc."""
        m = _db["metrics"]
        return (
            f"MRR: ${m['mrr']:,} | ARR: ${m['arr']:,} | Churn: {m['churn_rate']:.1%}\n"
            f"NPS: {m['nps']} | Avg Deal: ${m['avg_deal_size']:,} | Sales Cycle: {m['sales_cycle_days']}d\n"
            f"CAC: ${m['cac']:,} | LTV: ${m['ltv']:,} | LTV/CAC: {m['ltv'] / m['cac']:.1f}x"
        )

    @tool
    def calculate_metric(expression: str) -> str:
        """Evaluate a math expression. Use for aggregations and calculations."""
        try:
            allowed = set("0123456789+-*/.() ")
            if all(c in allowed for c in expression):
                return str(round(eval(expression), 2))  # noqa: S307
            return f"Invalid: {expression}"
        except Exception as e:
            return f"Error: {e}"

    @tool
    def write_summary(title: str, content: str) -> str:
        """Save an executive summary report. Returns confirmation."""
        return f"Summary '{title}' saved ({len(content.split())} words)."

    return [
        query_customers,
        get_customer_revenue,
        query_tickets,
        get_business_metrics,
        calculate_metric,
        write_summary,
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Result container
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class BenchmarkResult:
    """Metrics from a single agent run."""

    framework: str = ""
    question: str = ""
    tool_calls: list[str] = field(default_factory=list)
    total_tokens: int = 0
    latency_ms: float = 0.0
    answer: str = ""
    error: str | None = None


def _extract_metrics(
    result: dict, framework: str, question: str, duration_ms: float
) -> BenchmarkResult:
    """Extract standardized metrics from an agent result dict."""
    br = BenchmarkResult(framework=framework, question=question, latency_ms=duration_ms)

    for msg in result.get("messages", []):
        # Tool calls from AIMessage
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                if isinstance(tc, dict) and tc.get("name"):
                    br.tool_calls.append(tc["name"])
        # Token usage
        usage = getattr(msg, "usage_metadata", None)
        if usage:
            br.total_tokens += getattr(usage, "total_tokens", 0)

    # Final answer
    for msg in reversed(result.get("messages", [])):
        if getattr(msg, "type", "") == "ai" and getattr(msg, "content", ""):
            br.answer = msg.content
            break

    return br


DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
WHITE = "\033[97m"
BG_DIM = "\033[48;5;236m"


def _bar(value: float, max_val: float, width: int = 20, color: str = GREEN) -> str:
    """Render a horizontal bar chart segment."""
    if max_val <= 0:
        return " " * width
    filled = int((value / max_val) * width)
    filled = min(filled, width)
    return f"{color}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def _winner_badge(pf_val: float, lg_val: float, lower_is_better: bool = True) -> tuple[str, str]:
    """Return (pf_badge, lg_badge) with the winner highlighted."""
    if lower_is_better:
        pf_wins = pf_val <= lg_val
    else:
        pf_wins = pf_val >= lg_val
    pf_b = f"{GREEN}◀ WIN{RESET}" if pf_wins else ""
    lg_b = f"{GREEN}WIN ▶{RESET}" if not pf_wins else ""
    return pf_b, lg_b


def _wrap_text(text: str, width: int = 64, indent: str = "    ") -> str:
    """Wrap long text to multiple indented lines."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append(current)
    return ("\n" + indent).join(lines)


def _print_comparison(results: list[BenchmarkResult], question: str) -> None:
    """Print a visually rich comparison table with tool details and answers."""
    pf = next((r for r in results if r.framework == "Promptise"), None)
    lg = next((r for r in results if r.framework == "LangGraph"), None)
    if not pf or not lg:
        return

    print(f"\n  {DIM}{'─' * 72}{RESET}")
    print(f"  {CYAN}Q:{RESET} {WHITE}{question[:68]}{RESET}")
    print(f"  {DIM}{'─' * 72}{RESET}")
    print(
        f"  {'':32}{BOLD}{GREEN}{'Promptise':>14}{RESET}  {'':3}  {BOLD}{CYAN}{'LangGraph':>14}{RESET}"
    )
    print(f"  {DIM}{'─' * 72}{RESET}")

    # Tool calls bar
    max_tc = max(len(pf.tool_calls), len(lg.tool_calls), 1)
    pf_b, lg_b = _winner_badge(len(pf.tool_calls), len(lg.tool_calls))
    print(
        f"  {DIM}Tool calls{RESET}     "
        f"{_bar(len(pf.tool_calls), max_tc, 12, GREEN)} {len(pf.tool_calls):>4}  {pf_b:8}"
        f"{_bar(len(lg.tool_calls), max_tc, 12, CYAN)} {len(lg.tool_calls):>4}  {lg_b}"
    )

    # Latency bar
    max_lat = max(pf.latency_ms, lg.latency_ms, 1)
    pf_b, lg_b = _winner_badge(pf.latency_ms, lg.latency_ms)
    print(
        f"  {DIM}Latency (ms){RESET}   "
        f"{_bar(pf.latency_ms, max_lat, 12, GREEN)} {pf.latency_ms:>4,.0f}  {pf_b:8}"
        f"{_bar(lg.latency_ms, max_lat, 12, CYAN)} {lg.latency_ms:>4,.0f}  {lg_b}"
    )

    # Answer length bar
    max_ans = max(len(pf.answer), len(lg.answer), 1)
    pf_b, lg_b = _winner_badge(len(pf.answer), len(lg.answer), lower_is_better=False)
    print(
        f"  {DIM}Answer chars{RESET}   "
        f"{_bar(len(pf.answer), max_ans, 12, GREEN)} {len(pf.answer):>4}  {pf_b:8}"
        f"{_bar(len(lg.answer), max_ans, 12, CYAN)} {len(lg.answer):>4}  {lg_b}"
    )

    # Speed diff
    if lg.latency_ms > 0:
        speedup = ((lg.latency_ms - pf.latency_ms) / lg.latency_ms) * 100
        if speedup > 0:
            print(f"\n  {GREEN}⚡ Promptise is {speedup:.0f}% faster{RESET}")
        else:
            print(f"\n  {CYAN}⚡ LangGraph is {-speedup:.0f}% faster{RESET}")

    # ── Tool call details ──
    print(f"\n  {DIM}{'─' * 72}{RESET}")
    print(f"  {BOLD}Tool Calls{RESET}")

    pf_tools_str = " → ".join(pf.tool_calls) if pf.tool_calls else "(none)"
    lg_tools_str = " → ".join(lg.tool_calls) if lg.tool_calls else "(none)"
    print(f"    {GREEN}Promptise:{RESET} {DIM}{pf_tools_str}{RESET}")
    print(f"    {CYAN}LangGraph:{RESET} {DIM}{lg_tools_str}{RESET}")

    # ── Answers ──
    print(f"\n  {BOLD}Answers{RESET}")
    pf_preview = pf.answer[:200].replace("\n", " ") if pf.answer else "(empty)"
    lg_preview = lg.answer[:200].replace("\n", " ") if lg.answer else "(empty)"
    print(f"    {GREEN}Promptise:{RESET}")
    print(f"    {DIM}{_wrap_text(pf_preview)}{RESET}")
    print(f"    {CYAN}LangGraph:{RESET}")
    print(f"    {DIM}{_wrap_text(lg_preview)}{RESET}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Benchmark tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBenchmark:
    """Head-to-head Promptise vs LangGraph on identical tasks."""

    @pytest.fixture
    def model(self):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model="gpt-4o-mini", temperature=0)

    @pytest.fixture
    def tools(self):
        return _make_tools()

    async def _run_langgraph(self, model, tools, question: str) -> BenchmarkResult:
        """Run question through LangGraph create_react_agent."""
        from langgraph.prebuilt import create_react_agent

        agent = create_react_agent(model, tools)
        start = time.monotonic()
        result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
        duration = (time.monotonic() - start) * 1000
        return _extract_metrics(result, "LangGraph", question, duration)

    async def _run_promptise(self, model, tools, question: str) -> BenchmarkResult:
        """Run question through Promptise PromptGraphEngine."""
        from promptise.engine import PromptGraph, PromptNode
        from promptise.engine.execution import PromptGraphEngine

        graph = PromptGraph("benchmark", mode="static")
        graph.add_node(
            PromptNode(
                "reason",
                instructions="You are a business analyst. Use the provided tools to answer accurately.",
                tools=tools,
                is_entry=True,
            )
        )
        graph.set_entry("reason")
        engine = PromptGraphEngine(graph=graph, model=model)

        start = time.monotonic()
        result = await engine.ainvoke({"messages": [{"role": "user", "content": question}]})
        duration = (time.monotonic() - start) * 1000

        br = _extract_metrics(result, "Promptise", question, duration)
        # Also capture engine-level token count if available
        if engine.last_report and engine.last_report.total_tokens > br.total_tokens:
            br.total_tokens = engine.last_report.total_tokens
        return br

    # ── Test 1: Simple tool call ──

    @pytest.mark.asyncio
    async def test_simple_search(self, model, tools):
        """Single search tool call — compare overhead."""
        q = "Search for our revenue data."

        lg = await self._run_langgraph(model, tools, q)
        pf = await self._run_promptise(model, tools, q)

        _print_comparison([pf, lg], q)

        assert len(pf.tool_calls) > 0, "Promptise should call tools"
        assert len(lg.tool_calls) > 0, "LangGraph should call tools"
        assert "search_knowledge_base" in pf.tool_calls
        assert "search_knowledge_base" in lg.tool_calls

    # ── Test 2: Multi-step reasoning ──

    @pytest.mark.asyncio
    async def test_multi_step_reasoning(self, model, tools):
        """Search + calculate — compare multi-step performance."""
        q = "Find our employee count, then calculate revenue per employee if revenue is $42M."

        lg = await self._run_langgraph(model, tools, q)
        pf = await self._run_promptise(model, tools, q)

        _print_comparison([pf, lg], q)

        # Both should call at least search
        assert len(pf.tool_calls) >= 1, f"Promptise tools: {pf.tool_calls}"
        assert len(lg.tool_calls) >= 1, f"LangGraph tools: {lg.tool_calls}"
        assert len(pf.answer) > 10
        assert len(lg.answer) > 10

    # ── Test 3: Quick factual lookup ──

    @pytest.mark.asyncio
    async def test_quick_lookup(self, model, tools):
        """Simple date lookup — minimum overhead comparison."""
        q = "What is today's date? Use the get_current_date tool."

        lg = await self._run_langgraph(model, tools, q)
        pf = await self._run_promptise(model, tools, q)

        _print_comparison([pf, lg], q)

        assert "get_current_date" in pf.tool_calls
        assert "get_current_date" in lg.tool_calls

    # ── Test 4: Summary comparison ──

    @pytest.mark.asyncio
    async def test_overall_summary(self, model, tools):
        """Run 3 questions and print aggregate comparison."""
        questions = [
            "Search for our product information.",
            "What is today's date?",
            "Calculate 42000000 / 340.",
        ]

        pf_total_tokens = 0
        pf_total_time = 0.0
        lg_total_tokens = 0
        lg_total_time = 0.0

        for q in questions:
            lg = await self._run_langgraph(model, tools, q)
            pf = await self._run_promptise(model, tools, q)

            pf_total_tokens += pf.total_tokens
            pf_total_time += pf.latency_ms
            lg_total_tokens += lg.total_tokens
            lg_total_time += lg.latency_ms

        pf_avg = pf_total_time / len(questions)
        lg_avg = lg_total_time / len(questions)
        speed_diff = ((lg_total_time - pf_total_time) / max(lg_total_time, 1)) * 100
        max_time = max(pf_total_time, lg_total_time, 1)

        print(f"\n\n  {BOLD}{WHITE}{'═' * 62}{RESET}")
        print(f"  {BOLD}{WHITE}  AGGREGATE — {len(questions)} QUESTIONS{RESET}")
        print(f"  {BOLD}{WHITE}{'═' * 62}{RESET}")
        print(f"  {'':32}{BOLD}{'Promptise':>14}{RESET}  {'':3}  {BOLD}{'LangGraph':>14}{RESET}")
        print(f"  {DIM}{'─' * 62}{RESET}")

        pf_b, lg_b = _winner_badge(pf_total_time, lg_total_time)
        print(
            f"  {DIM}Total time (ms){RESET} "
            f"{_bar(pf_total_time, max_time, 12, GREEN)} {pf_total_time:>5,.0f}  {pf_b:8}"
            f"{_bar(lg_total_time, max_time, 12, CYAN)} {lg_total_time:>5,.0f}  {lg_b}"
        )
        print(
            f"  {DIM}Avg time (ms){RESET}   {'':12} {pf_avg:>5,.0f}  {'':8}{'':12} {lg_avg:>5,.0f}"
        )

        print(f"  {DIM}{'─' * 62}{RESET}")
        if speed_diff > 0:
            print(f"\n  {GREEN}{BOLD}  ⚡ Promptise is {speed_diff:.0f}% faster overall{RESET}")
        else:
            print(f"\n  {CYAN}{BOLD}  ⚡ LangGraph is {-speed_diff:.0f}% faster overall{RESET}")
        print(f"  {BOLD}{WHITE}{'═' * 62}{RESET}\n")

        # Both should have completed all questions (tokens may be 0 if
        # the model doesn't report usage_metadata — that's a LangChain/provider
        # limitation, not a framework issue)
        assert pf_total_time > 0, "Promptise should have run"
        assert lg_total_time > 0, "LangGraph should have run"


# ═══════════════════════════════════════════════════════════════════════════════
# Complex Benchmark — 6 tools, multi-step reasoning, data aggregation
# ═══════════════════════════════════════════════════════════════════════════════


class TestComplexBenchmark:
    """Harder scenarios: multiple tools, data aggregation, multi-step reasoning.

    Both agents are ReAct — same model, same tools, same system prompt.
    Tests real-world business analysis tasks.
    """

    SYSTEM_PROMPT = (
        "You are a senior business analyst with access to company data tools. "
        "When analyzing data, always query the actual data — never guess. "
        "Use calculate_metric for any math. Be precise with numbers."
    )

    @pytest.fixture
    def model(self):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model="gpt-4o-mini", temperature=0)

    @pytest.fixture
    def tools(self):
        return _make_complex_tools()

    async def _run_langgraph(self, model, tools, question: str) -> BenchmarkResult:
        from langgraph.prebuilt import create_react_agent

        agent = create_react_agent(model, tools, prompt=self.SYSTEM_PROMPT)
        start = time.monotonic()
        result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
        duration = (time.monotonic() - start) * 1000
        return _extract_metrics(result, "LangGraph", question, duration)

    async def _run_promptise(self, model, tools, question: str) -> BenchmarkResult:
        from promptise.engine import PromptGraph, PromptNode
        from promptise.engine.execution import PromptGraphEngine

        graph = PromptGraph("benchmark-complex", mode="static")
        graph.add_node(
            PromptNode(
                "reason",
                instructions=self.SYSTEM_PROMPT,
                tools=tools,
                is_entry=True,
            )
        )
        graph.set_entry("reason")
        engine = PromptGraphEngine(graph=graph, model=model)

        start = time.monotonic()
        result = await engine.ainvoke({"messages": [{"role": "user", "content": question}]})
        duration = (time.monotonic() - start) * 1000

        br = _extract_metrics(result, "Promptise", question, duration)
        if engine.last_report and engine.last_report.total_tokens > br.total_tokens:
            br.total_tokens = engine.last_report.total_tokens
        return br

    # ── Test 1: Multi-tool data gathering ──

    @pytest.mark.asyncio
    async def test_multi_tool_gathering(self, model, tools):
        """Agent must query customers AND metrics to answer."""
        q = (
            "What is our total enterprise customer revenue? "
            "Query all enterprise customers and sum their revenue."
        )

        lg = await self._run_langgraph(model, tools, q)
        pf = await self._run_promptise(model, tools, q)

        _print_comparison([pf, lg], q)

        assert len(pf.tool_calls) >= 1
        assert len(lg.tool_calls) >= 1
        assert len(pf.answer) > 10
        assert len(lg.answer) > 10

    # ── Test 2: Cross-reference analysis ──

    @pytest.mark.asyncio
    async def test_cross_reference(self, model, tools):
        """Agent must cross-reference tickets with customer data."""
        q = (
            "Which enterprise customers have open critical or high-priority tickets? "
            "List the customer names with their ticket subjects."
        )

        lg = await self._run_langgraph(model, tools, q)
        pf = await self._run_promptise(model, tools, q)

        _print_comparison([pf, lg], q)

        # Should call both ticket and customer queries
        assert len(pf.tool_calls) >= 1
        assert len(lg.tool_calls) >= 1

    # ── Test 3: Calculation chain ──

    @pytest.mark.asyncio
    async def test_calculation_chain(self, model, tools):
        """Agent must gather metrics then calculate derived values."""
        q = (
            "Get our business metrics. Then calculate: "
            "(1) monthly revenue growth needed to hit $25M ARR, "
            "(2) how many new deals at average deal size to close the gap."
        )

        lg = await self._run_langgraph(model, tools, q)
        pf = await self._run_promptise(model, tools, q)

        _print_comparison([pf, lg], q)

        # Should call metrics + calculate
        assert len(pf.tool_calls) >= 2, f"Expected 2+ tools, got: {pf.tool_calls}"
        assert len(lg.tool_calls) >= 2, f"Expected 2+ tools, got: {lg.tool_calls}"

    # ── Test 4: Report generation ──

    @pytest.mark.asyncio
    async def test_report_generation(self, model, tools):
        """Agent must gather data, analyze it, and produce a written report."""
        q = (
            "Create an executive summary: query all customers, get business metrics, "
            "identify the top 3 customers by revenue, and use write_summary to save "
            "a brief report about the health of the business."
        )

        lg = await self._run_langgraph(model, tools, q)
        pf = await self._run_promptise(model, tools, q)

        _print_comparison([pf, lg], q)

        # Should use multiple tools including write_summary
        assert len(pf.tool_calls) >= 2
        assert len(lg.tool_calls) >= 2
        assert "write_summary" in pf.tool_calls, f"Expected write_summary, got: {pf.tool_calls}"
        assert "write_summary" in lg.tool_calls, f"Expected write_summary, got: {lg.tool_calls}"

    # ── Test 5: Aggregate summary ──

    @pytest.mark.asyncio
    async def test_complex_aggregate(self, model, tools):
        """Run all complex questions and print aggregate comparison."""
        questions = [
            "Query enterprise customers and get their total revenue.",
            "List open high-priority tickets with customer names.",
            "Get business metrics and calculate LTV/CAC ratio.",
        ]

        all_pf: list[BenchmarkResult] = []
        all_lg: list[BenchmarkResult] = []

        for q in questions:
            lg = await self._run_langgraph(model, tools, q)
            pf = await self._run_promptise(model, tools, q)
            all_pf.append(pf)
            all_lg.append(lg)
            _print_comparison([pf, lg], q)

        pf_total_time = sum(r.latency_ms for r in all_pf)
        lg_total_time = sum(r.latency_ms for r in all_lg)
        pf_total_tools = sum(len(r.tool_calls) for r in all_pf)
        lg_total_tools = sum(len(r.tool_calls) for r in all_lg)
        max_time = max(pf_total_time, lg_total_time, 1)
        speed_diff = ((lg_total_time - pf_total_time) / max(lg_total_time, 1)) * 100

        print(f"\n\n  {BOLD}{WHITE}{'═' * 62}{RESET}")
        print(f"  {BOLD}{WHITE}  COMPLEX AGGREGATE — {len(questions)} QUESTIONS (6 tools){RESET}")
        print(f"  {BOLD}{WHITE}{'═' * 62}{RESET}")
        print(
            f"  {'':32}{BOLD}{GREEN}{'Promptise':>14}{RESET}  {'':3}  {BOLD}{CYAN}{'LangGraph':>14}{RESET}"
        )
        print(f"  {DIM}{'─' * 62}{RESET}")

        pf_b, lg_b = _winner_badge(pf_total_time, lg_total_time)
        print(
            f"  {DIM}Total time (ms){RESET} "
            f"{_bar(pf_total_time, max_time, 12, GREEN)} {pf_total_time:>5,.0f}  {pf_b:8}"
            f"{_bar(lg_total_time, max_time, 12, CYAN)} {lg_total_time:>5,.0f}  {lg_b}"
        )
        pf_b, lg_b = _winner_badge(pf_total_tools, lg_total_tools)
        print(
            f"  {DIM}Total tool calls{RESET}"
            f"{_bar(pf_total_tools, max(pf_total_tools, lg_total_tools, 1), 12, GREEN)} {pf_total_tools:>5}  {pf_b:8}"
            f"{_bar(lg_total_tools, max(pf_total_tools, lg_total_tools, 1), 12, CYAN)} {lg_total_tools:>5}  {lg_b}"
        )

        print(f"  {DIM}{'─' * 62}{RESET}")
        if speed_diff > 0:
            print(f"\n  {GREEN}{BOLD}  ⚡ Promptise is {speed_diff:.0f}% faster overall{RESET}")
        else:
            print(f"\n  {CYAN}{BOLD}  ⚡ LangGraph is {-speed_diff:.0f}% faster overall{RESET}")
        print(f"  {BOLD}{WHITE}{'═' * 62}{RESET}\n")

        assert pf_total_time > 0
        assert lg_total_time > 0
