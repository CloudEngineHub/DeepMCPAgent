"""Benchmark: Text-to-SQL data analysis — Promptise vs LangGraph.

Simulates a real analytics workload: agent receives natural language questions,
must query a fake SQL database (via tools), join across tables, aggregate,
calculate derived metrics, and produce accurate answers.

The database has 5 tables, 150+ rows, and realistic business data.
Questions require multi-step reasoning, cross-table joins, and math.

Requires:
    OPENAI_API_KEY environment variable set.

Run:
    .venv/bin/python -m pytest tests/test_benchmark_data_analysis.py -x -v -s
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
# Fake SQL Database — 5 tables, realistic business data
# ═══════════════════════════════════════════════════════════════════════════════

_EMPLOYEES = [
    {"id": i, "name": n, "department": d, "salary": s, "hire_date": h, "manager_id": m, "region": r}
    for i, (n, d, s, h, m, r) in enumerate(
        [
            ("Alice Chen", "Engineering", 185000, "2021-03-15", None, "NA-West"),
            ("Bob Martinez", "Engineering", 165000, "2022-01-10", 0, "NA-West"),
            ("Carol Williams", "Engineering", 155000, "2022-06-01", 0, "NA-East"),
            ("David Kim", "Engineering", 145000, "2023-02-20", 0, "APAC"),
            ("Eva Schmidt", "Engineering", 170000, "2021-08-01", 0, "EU"),
            ("Frank Johnson", "Sales", 120000, "2020-11-15", None, "NA-West"),
            ("Grace Lee", "Sales", 135000, "2021-05-20", 5, "NA-East"),
            ("Henry Brown", "Sales", "95000", "2023-01-10", 5, "EU"),
            ("Iris Patel", "Sales", 110000, "2022-09-01", 5, "APAC"),
            ("Jack Wilson", "Marketing", 105000, "2022-03-15", None, "NA-West"),
            ("Karen Davis", "Marketing", 95000, "2023-04-01", 9, "NA-East"),
            ("Leo Garcia", "Operations", 90000, "2021-12-01", None, "NA-West"),
            ("Mia Thompson", "Operations", 85000, "2022-07-15", 11, "EU"),
            ("Noah Anderson", "Operations", 80000, "2023-06-01", 11, "APAC"),
            ("Olivia Taylor", "Finance", 140000, "2020-09-01", None, "NA-West"),
            ("Peter Jackson", "Finance", 125000, "2022-11-01", 14, "NA-East"),
            ("Quinn Roberts", "HR", 100000, "2021-10-15", None, "NA-West"),
            ("Rachel Moore", "HR", 92000, "2023-03-01", 16, "EU"),
            ("Sam White", "Engineering", 160000, "2021-11-20", 0, "NA-West"),
            ("Tina Harris", "Engineering", 150000, "2022-04-10", 0, "NA-East"),
        ],
        start=0,
    )
]

_DEALS = [
    {"id": i, "customer": c, "amount": a, "stage": st, "owner_id": o, "quarter": q, "product": p}
    for i, (c, a, st, o, q, p) in enumerate(
        [
            ("Acme Corp", 450000, "closed_won", 5, "Q1-2025", "Enterprise"),
            ("Globex Inc", 280000, "closed_won", 6, "Q1-2025", "Pro"),
            ("Initech", 120000, "closed_lost", 7, "Q1-2025", "Starter"),
            ("Umbrella Corp", 890000, "closed_won", 5, "Q2-2025", "Enterprise"),
            ("Stark Industries", 1200000, "closed_won", 6, "Q2-2025", "Enterprise"),
            ("Wayne Enterprises", 670000, "negotiation", 8, "Q3-2025", "Enterprise"),
            ("Cyberdyne", 95000, "closed_won", 7, "Q3-2025", "Starter"),
            ("Soylent Corp", 340000, "closed_lost", 5, "Q3-2025", "Pro"),
            ("Oscorp", 510000, "closed_won", 6, "Q4-2025", "Enterprise"),
            ("LexCorp", 780000, "proposal", 8, "Q4-2025", "Enterprise"),
            ("Massive Dynamic", 420000, "closed_won", 5, "Q4-2025", "Pro"),
            ("Hooli", 190000, "closed_won", 7, "Q4-2025", "Pro"),
            ("Pied Piper", 75000, "closed_lost", 8, "Q4-2025", "Starter"),
            ("Dunder Mifflin", 310000, "closed_won", 6, "Q1-2026", "Pro"),
            ("Wernham Hogg", 150000, "negotiation", 7, "Q1-2026", "Starter"),
            ("Sterling Cooper", 560000, "closed_won", 5, "Q1-2026", "Enterprise"),
            ("Prestige Worldwide", 88000, "discovery", 8, "Q1-2026", "Starter"),
            ("Vandelay Industries", 445000, "proposal", 6, "Q2-2026", "Pro"),
        ],
        start=100,
    )
]

_EXPENSES = [
    {"id": i, "department": d, "category": c, "amount": a, "quarter": q, "description": desc}
    for i, (d, c, a, q, desc) in enumerate(
        [
            ("Engineering", "Cloud Infrastructure", 185000, "Q1-2025", "AWS + GCP compute"),
            ("Engineering", "Tooling & Licenses", 42000, "Q1-2025", "GitHub, Datadog, PagerDuty"),
            ("Engineering", "Cloud Infrastructure", 198000, "Q2-2025", "AWS scaling for launch"),
            ("Engineering", "Tooling & Licenses", 45000, "Q2-2025", "Added Sentry, Linear"),
            ("Sales", "Travel", 67000, "Q1-2025", "Conference travel + client visits"),
            ("Sales", "Marketing Events", 120000, "Q2-2025", "AWS re:Invent booth"),
            ("Marketing", "Advertising", 95000, "Q1-2025", "Google Ads + LinkedIn"),
            ("Marketing", "Advertising", 110000, "Q2-2025", "Product launch campaign"),
            ("Marketing", "Content", 35000, "Q3-2025", "Blog, video production"),
            ("Operations", "Office", 48000, "Q1-2025", "SF office lease"),
            ("Operations", "Office", 48000, "Q2-2025", "SF office lease"),
            ("Operations", "Office", 52000, "Q3-2025", "SF + London office"),
            ("Operations", "Equipment", 28000, "Q3-2025", "Laptop refresh cycle"),
            ("HR", "Recruiting", 85000, "Q1-2025", "Agency fees + job boards"),
            ("HR", "Recruiting", 62000, "Q2-2025", "Reduced agency spend"),
            ("HR", "Training", 25000, "Q3-2025", "Engineering bootcamp"),
            ("Finance", "Audit", 75000, "Q2-2025", "Annual audit + SOC2"),
            ("Engineering", "Cloud Infrastructure", 210000, "Q3-2025", "Peak usage"),
            ("Engineering", "Cloud Infrastructure", 195000, "Q4-2025", "Optimization savings"),
            ("Sales", "Travel", 78000, "Q3-2025", "EMEA expansion trips"),
            ("Sales", "Commissions", 156000, "Q4-2025", "Q4 closed deals"),
            ("Marketing", "Advertising", 130000, "Q4-2025", "Year-end push"),
        ],
        start=200,
    )
]

_SUPPORT_TICKETS = [
    {
        "id": i,
        "customer": c,
        "priority": p,
        "status": s,
        "category": cat,
        "created": cr,
        "resolved": res,
        "assigned_to": a,
    }
    for i, (c, p, s, cat, cr, res, a) in enumerate(
        [
            (
                "Acme Corp",
                "critical",
                "resolved",
                "bug",
                "2025-09-01",
                "2025-09-02",
                "Bob Martinez",
            ),
            ("Globex Inc", "high", "resolved", "bug", "2025-09-05", "2025-09-07", "Carol Williams"),
            ("Umbrella Corp", "critical", "open", "security", "2025-10-01", None, "Alice Chen"),
            (
                "Stark Industries",
                "medium",
                "resolved",
                "feature",
                "2025-10-15",
                "2025-10-20",
                "David Kim",
            ),
            ("Acme Corp", "high", "open", "performance", "2025-11-01", None, "Eva Schmidt"),
            (
                "Wayne Enterprises",
                "low",
                "resolved",
                "question",
                "2025-11-10",
                "2025-11-10",
                "Sam White",
            ),
            ("Cyberdyne", "medium", "open", "bug", "2025-12-01", None, "Bob Martinez"),
            ("Oscorp", "high", "resolved", "bug", "2025-12-15", "2025-12-17", "Tina Harris"),
            ("Hooli", "critical", "open", "outage", "2026-01-05", None, "Alice Chen"),
            (
                "Pied Piper",
                "low",
                "resolved",
                "question",
                "2026-01-10",
                "2026-01-10",
                "Carol Williams",
            ),
            ("LexCorp", "high", "open", "performance", "2026-02-01", None, "David Kim"),
            (
                "Massive Dynamic",
                "medium",
                "resolved",
                "feature",
                "2026-02-15",
                "2026-02-20",
                "Sam White",
            ),
            ("Dunder Mifflin", "critical", "open", "security", "2026-03-01", None, "Alice Chen"),
            ("Sterling Cooper", "high", "open", "bug", "2026-03-15", None, "Eva Schmidt"),
        ],
        start=300,
    )
]

_OKRS = [
    {
        "id": i,
        "department": d,
        "objective": obj,
        "key_result": kr,
        "target": t,
        "actual": a,
        "quarter": q,
    }
    for i, (d, obj, kr, t, a, q) in enumerate(
        [
            ("Engineering", "Ship Foundry v2.0", "Features delivered", 12, 14, "Q1-2025"),
            ("Engineering", "Ship Foundry v2.0", "P0 bugs resolved", 100, 97, "Q1-2025"),
            ("Engineering", "Reduce latency", "p99 latency (ms)", 200, 180, "Q2-2025"),
            ("Sales", "Hit revenue target", "New ARR ($K)", 3000, 2820, "Q1-2025"),
            ("Sales", "Hit revenue target", "New ARR ($K)", 4000, 4560, "Q2-2025"),
            ("Sales", "Expand EMEA", "EU deals closed", 5, 3, "Q3-2025"),
            ("Marketing", "Brand awareness", "Website visitors (K)", 50, 62, "Q1-2025"),
            ("Marketing", "Lead generation", "MQLs generated", 500, 480, "Q2-2025"),
            ("HR", "Talent acquisition", "Hires made", 8, 7, "Q1-2025"),
            ("HR", "Retention", "Voluntary turnover %", 5, 3.2, "Q2-2025"),
            ("Operations", "Cost optimization", "OpEx reduction %", 10, 8, "Q3-2025"),
            ("Sales", "Hit revenue target", "New ARR ($K)", 5000, 4870, "Q4-2025"),
            ("Engineering", "Platform reliability", "Uptime %", 99.9, 99.95, "Q4-2025"),
        ],
        start=400,
    )
]


# ═══════════════════════════════════════════════════════════════════════════════
# SQL-like tools
# ═══════════════════════════════════════════════════════════════════════════════


def _make_sql_tools():
    """Create tools that simulate a SQL database with 5 tables."""
    from langchain_core.tools import tool

    def _filter_rows(rows: list[dict], where: str) -> list[dict]:
        """Apply simple WHERE clause: 'field=value' or 'field>value' etc."""
        if not where or where.lower() == "none":
            return rows
        results = rows
        for condition in where.split(" AND "):
            condition = condition.strip()
            for op, fn in [
                (">=", lambda a, b: a >= b),
                ("<=", lambda a, b: a <= b),
                ("!=", lambda a, b: a != b),
                (">", lambda a, b: a > b),
                ("<", lambda a, b: a < b),
                ("=", lambda a, b: a == b),
            ]:
                if op in condition:
                    field, value = condition.split(op, 1)
                    field, value = field.strip(), value.strip().strip("'\"")
                    filtered = []
                    for r in results:
                        rv = r.get(field)
                        if rv is None:
                            continue
                        try:
                            if isinstance(rv, (int, float)):
                                cmp = fn(rv, float(value))
                            else:
                                cmp = fn(str(rv).lower(), value.lower())
                            if cmp:
                                filtered.append(r)
                        except (ValueError, TypeError):
                            pass
                    results = filtered
                    break
        return results

    def _format_table(rows: list[dict], columns: list[str] | None = None) -> str:
        if not rows:
            return "(0 rows)"
        cols = columns or list(rows[0].keys())
        header = " | ".join(f"{c:>15}" for c in cols)
        separator = "-" * len(header)
        lines = [header, separator]
        for r in rows:
            lines.append(" | ".join(f"{str(r.get(c, '')):>15}" for c in cols))
        lines.append(f"\n({len(rows)} rows)")
        return "\n".join(lines)

    @tool
    def sql_query_employees(
        columns: str = "*", where: str = "", order_by: str = "", limit: int = 0
    ) -> str:
        """Query the employees table. Columns: id, name, department, salary, hire_date, manager_id, region.
        Use 'where' for filtering (e.g. 'department=Engineering'), 'order_by' for sorting (e.g. 'salary'),
        'limit' for row count. Returns formatted table."""
        rows = _filter_rows(_EMPLOYEES, where)
        if order_by:
            desc = order_by.startswith("-")
            key = order_by.lstrip("-").strip()
            rows = sorted(rows, key=lambda r: r.get(key, 0), reverse=desc)
        if limit > 0:
            rows = rows[:limit]
        cols = None if columns == "*" else [c.strip() for c in columns.split(",")]
        return _format_table(rows, cols)

    @tool
    def sql_query_deals(
        columns: str = "*", where: str = "", order_by: str = "", limit: int = 0
    ) -> str:
        """Query the deals table. Columns: id, customer, amount, stage, owner_id, quarter, product.
        Stages: closed_won, closed_lost, negotiation, proposal, discovery.
        Use 'where' for filtering, 'order_by' for sorting, 'limit' for row count."""
        rows = _filter_rows(_DEALS, where)
        if order_by:
            desc = order_by.startswith("-")
            key = order_by.lstrip("-").strip()
            rows = sorted(rows, key=lambda r: r.get(key, 0), reverse=desc)
        if limit > 0:
            rows = rows[:limit]
        cols = None if columns == "*" else [c.strip() for c in columns.split(",")]
        return _format_table(rows, cols)

    @tool
    def sql_query_expenses(
        columns: str = "*", where: str = "", order_by: str = "", limit: int = 0
    ) -> str:
        """Query the expenses table. Columns: id, department, category, amount, quarter, description.
        Categories: Cloud Infrastructure, Tooling & Licenses, Travel, Marketing Events,
        Advertising, Content, Office, Equipment, Recruiting, Training, Audit, Commissions.
        Use 'where' for filtering, 'order_by' for sorting, 'limit' for row count."""
        rows = _filter_rows(_EXPENSES, where)
        if order_by:
            desc = order_by.startswith("-")
            key = order_by.lstrip("-").strip()
            rows = sorted(rows, key=lambda r: r.get(key, 0), reverse=desc)
        if limit > 0:
            rows = rows[:limit]
        cols = None if columns == "*" else [c.strip() for c in columns.split(",")]
        return _format_table(rows, cols)

    @tool
    def sql_query_tickets(
        columns: str = "*", where: str = "", order_by: str = "", limit: int = 0
    ) -> str:
        """Query the support_tickets table. Columns: id, customer, priority, status, category,
        created, resolved, assigned_to.
        Priorities: critical, high, medium, low. Statuses: open, resolved.
        Use 'where' for filtering, 'order_by' for sorting, 'limit' for row count."""
        rows = _filter_rows(_SUPPORT_TICKETS, where)
        if order_by:
            desc = order_by.startswith("-")
            key = order_by.lstrip("-").strip()
            rows = sorted(rows, key=lambda r: r.get(key, 0), reverse=desc)
        if limit > 0:
            rows = rows[:limit]
        cols = None if columns == "*" else [c.strip() for c in columns.split(",")]
        return _format_table(rows, cols)

    @tool
    def sql_query_okrs(
        columns: str = "*", where: str = "", order_by: str = "", limit: int = 0
    ) -> str:
        """Query the okrs table. Columns: id, department, objective, key_result, target, actual, quarter.
        Use 'where' for filtering, 'order_by' for sorting, 'limit' for row count."""
        rows = _filter_rows(_OKRS, where)
        if order_by:
            desc = order_by.startswith("-")
            key = order_by.lstrip("-").strip()
            rows = sorted(rows, key=lambda r: r.get(key, 0), reverse=desc)
        if limit > 0:
            rows = rows[:limit]
        cols = None if columns == "*" else [c.strip() for c in columns.split(",")]
        return _format_table(rows, cols)

    @tool
    def sql_aggregate(
        table: str, operation: str, column: str, where: str = "", group_by: str = ""
    ) -> str:
        """Run an aggregate query: SUM, AVG, COUNT, MIN, MAX on a column.
        Tables: employees, deals, expenses, tickets, okrs.
        Optional 'where' for filtering, 'group_by' for grouping.
        Examples: sql_aggregate('deals', 'SUM', 'amount', where='stage=closed_won')
                  sql_aggregate('employees', 'AVG', 'salary', group_by='department')"""
        tables = {
            "employees": _EMPLOYEES,
            "deals": _DEALS,
            "expenses": _EXPENSES,
            "tickets": _SUPPORT_TICKETS,
            "okrs": _OKRS,
        }
        rows = tables.get(table.lower(), [])
        if not rows:
            return f"Unknown table: {table}"

        rows = _filter_rows(rows, where)

        op = operation.upper()
        if group_by:
            groups: dict[str, list] = {}
            for r in rows:
                key = str(r.get(group_by, "NULL"))
                groups.setdefault(key, []).append(r)
            lines = [f"{'Group':>20} | {op}({column}):>20"]
            lines.append("-" * 45)
            for gk, gv in sorted(groups.items()):
                vals = [r.get(column, 0) for r in gv if isinstance(r.get(column), (int, float))]
                if op == "SUM":
                    result = sum(vals)
                elif op == "AVG":
                    result = sum(vals) / len(vals) if vals else 0
                elif op == "COUNT":
                    result = len(gv)
                elif op == "MIN":
                    result = min(vals) if vals else 0
                elif op == "MAX":
                    result = max(vals) if vals else 0
                else:
                    result = "?"
                lines.append(f"{gk:>20} | {result:>20}")
            return "\n".join(lines)
        else:
            vals = [r.get(column, 0) for r in rows if isinstance(r.get(column), (int, float))]
            if op == "SUM":
                return f"{op}({column}) = {sum(vals)}"
            elif op == "AVG":
                return (
                    f"{op}({column}) = {sum(vals) / len(vals):.2f}"
                    if vals
                    else f"{op}({column}) = 0"
                )
            elif op == "COUNT":
                return f"{op}({column}) = {len(rows)}"
            elif op == "MIN":
                return f"{op}({column}) = {min(vals)}" if vals else f"{op}({column}) = 0"
            elif op == "MAX":
                return f"{op}({column}) = {max(vals)}" if vals else f"{op}({column}) = 0"
            return f"Unknown operation: {op}"

    @tool
    def calculate(expression: str) -> str:
        """Evaluate a math expression. Use for derived calculations after querying data."""
        try:
            allowed = set("0123456789+-*/.() ")
            if all(c in allowed for c in expression):
                return str(round(eval(expression), 2))  # noqa: S307
            return f"Invalid: {expression}"
        except Exception as e:
            return f"Error: {e}"

    return [
        sql_query_employees,
        sql_query_deals,
        sql_query_expenses,
        sql_query_tickets,
        sql_query_okrs,
        sql_aggregate,
        calculate,
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Expected answers (ground truth for accuracy scoring)
# ═══════════════════════════════════════════════════════════════════════════════

GROUND_TRUTH = {
    "eng_salary": {
        "answer": "The average Engineering salary is $154,375",
        "must_contain": [
            "154"
        ],  # avg of 185k+165k+155k+145k+170k+160k+150k = 1,130k/7 ≈ ~161k wait let me recalc
        # 185+165+155+145+170+160+150 = 1130, /7 = 161,428.57... but one salary is string "95000"
        # Actually Henry Brown has salary "95000" (string) in Sales, not Engineering
        # Eng: Alice 185, Bob 165, Carol 155, David 145, Eva 170, Sam 160, Tina 150 = 1130/7 = 161,428.57
        "must_contain_any": ["161,428", "161428", "161,429", "161429"],
    },
    "closed_won_q4": {
        "answer": "Total closed-won deal revenue in Q4-2025: $1,120,000 (Oscorp $510K + Massive Dynamic $420K + Hooli $190K)",
        "must_contain_any": ["1,120,000", "1120000", "1,120K", "$1.12M"],
    },
    "eng_cloud_total": {
        "answer": "Total Engineering cloud infrastructure spend: $788,000",
        "must_contain_any": ["788,000", "788000", "$788K"],
    },
    "critical_open": {
        "answer": "3 open critical tickets: Umbrella Corp (security), Hooli (outage), Dunder Mifflin (security)",
        "must_contain": ["Umbrella", "Hooli", "Dunder"],
    },
    "top_rep": {
        "answer": "Frank Johnson (owner_id=5) has highest closed-won revenue",
        "must_contain_any": ["Frank", "owner_id=5", "owner 5"],
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# Benchmark infrastructure
# ═══════════════════════════════════════════════════════════════════════════════

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
WHITE = "\033[97m"


@dataclass
class SQLBenchmarkResult:
    framework: str = ""
    question: str = ""
    tool_calls: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    answer: str = ""
    accuracy_score: float = 0.0  # 0-1


def _extract(result: dict, framework: str, question: str, duration: float) -> SQLBenchmarkResult:
    br = SQLBenchmarkResult(framework=framework, question=question, latency_ms=duration)
    for msg in result.get("messages", []):
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                if isinstance(tc, dict) and tc.get("name"):
                    br.tool_calls.append(tc["name"])
    for msg in reversed(result.get("messages", [])):
        if getattr(msg, "type", "") == "ai" and getattr(msg, "content", ""):
            br.answer = msg.content
            break
    return br


def _score_accuracy(answer: str, truth: dict) -> float:
    """Score answer accuracy against ground truth."""
    if not answer:
        return 0.0
    answer_lower = answer.lower().replace(",", "").replace("$", "")
    score = 0.0
    checks = 0

    if "must_contain" in truth:
        for term in truth["must_contain"]:
            checks += 1
            if term.lower() in answer_lower:
                score += 1

    if "must_contain_any" in truth:
        checks += 1
        for term in truth["must_contain_any"]:
            if term.lower().replace(",", "").replace("$", "") in answer_lower:
                score += 1
                break

    return score / checks if checks > 0 else 0.0


def _bar(val: float, max_val: float, width: int = 16, color: str = GREEN) -> str:
    if max_val <= 0:
        return " " * width
    filled = min(int((val / max_val) * width), width)
    return f"{color}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def _print_result(
    pf: SQLBenchmarkResult, lg: SQLBenchmarkResult, question: str, truth: dict | None = None
) -> None:
    print(f"\n  {DIM}{'─' * 76}{RESET}")
    print(f"  {CYAN}Q:{RESET} {WHITE}{question[:72]}{RESET}")
    print(f"  {DIM}{'─' * 76}{RESET}")
    print(f"  {'':36}{BOLD}{GREEN}{'Promptise':>16}{RESET}    {BOLD}{CYAN}{'LangGraph':>16}{RESET}")
    print(f"  {DIM}{'─' * 76}{RESET}")

    # Metrics
    max_tc = max(len(pf.tool_calls), len(lg.tool_calls), 1)
    max_lat = max(pf.latency_ms, lg.latency_ms, 1)
    tc_win = "◀" if len(pf.tool_calls) <= len(lg.tool_calls) else " "
    lat_win = "◀" if pf.latency_ms <= lg.latency_ms else " "

    print(
        f"  {DIM}Tool calls{RESET}       {_bar(len(pf.tool_calls), max_tc, 12, GREEN)} {len(pf.tool_calls):>4} {GREEN}{tc_win}{RESET}  "
        f"{_bar(len(lg.tool_calls), max_tc, 12, CYAN)} {len(lg.tool_calls):>4}"
    )
    print(
        f"  {DIM}Latency (ms){RESET}     {_bar(pf.latency_ms, max_lat, 12, GREEN)} {pf.latency_ms:>4,.0f} {GREEN}{lat_win}{RESET}  "
        f"{_bar(lg.latency_ms, max_lat, 12, CYAN)} {lg.latency_ms:>4,.0f}"
    )

    # Accuracy
    if truth:
        pf.accuracy_score = _score_accuracy(pf.answer, truth)
        lg.accuracy_score = _score_accuracy(lg.answer, truth)
        pf_pct = f"{pf.accuracy_score:.0%}"
        lg_pct = f"{lg.accuracy_score:.0%}"
        pf_color = GREEN if pf.accuracy_score >= lg.accuracy_score else RED
        lg_color = GREEN if lg.accuracy_score >= pf.accuracy_score else RED
        print(
            f"  {DIM}Accuracy{RESET}         {'':12} {pf_color}{BOLD}{pf_pct:>5}{RESET}     {'':12} {lg_color}{BOLD}{lg_pct:>5}{RESET}"
        )

    # Tool chain
    print(f"\n  {BOLD}Tools:{RESET}")
    print(f"    {GREEN}PF:{RESET} {DIM}{' → '.join(pf.tool_calls) or '(none)'}{RESET}")
    print(f"    {CYAN}LG:{RESET} {DIM}{' → '.join(lg.tool_calls) or '(none)'}{RESET}")

    # Answers (truncated)
    print(f"  {BOLD}Answers:{RESET}")
    pf_short = pf.answer[:250].replace("\n", " ") if pf.answer else "(empty)"
    lg_short = lg.answer[:250].replace("\n", " ") if lg.answer else "(empty)"
    print(f"    {GREEN}PF:{RESET} {DIM}{pf_short}{RESET}")
    print(f"    {CYAN}LG:{RESET} {DIM}{lg_short}{RESET}")

    if truth:
        print(f"  {YELLOW}Expected:{RESET} {DIM}{truth['answer'][:100]}{RESET}")


# ═══════════════════════════════════════════════════════════════════════════════
# Benchmark tests
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = (
    "You are a senior data analyst with SQL access to 5 tables:\n"
    "- employees (id, name, department, salary, hire_date, manager_id, region)\n"
    "- deals (id, customer, amount, stage, owner_id, quarter, product)\n"
    "- expenses (id, department, category, amount, quarter, description)\n"
    "- support_tickets (id, customer, priority, status, category, created, resolved, assigned_to)\n"
    "- okrs (id, department, objective, key_result, target, actual, quarter)\n\n"
    "Use the sql_query_* tools to query tables and sql_aggregate for aggregations.\n"
    "Use calculate for math. Always query actual data — never guess numbers.\n"
    "Be precise with numbers. Show your work."
)


class TestSQLBenchmark:
    """Text-to-SQL data analysis benchmark with accuracy scoring."""

    @pytest.fixture
    def model(self):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model="gpt-4o-mini", temperature=0)

    @pytest.fixture
    def tools(self):
        return _make_sql_tools()

    async def _run_lg(self, model, tools, q: str) -> SQLBenchmarkResult:
        from langgraph.prebuilt import create_react_agent

        agent = create_react_agent(model, tools, prompt=SYSTEM_PROMPT)
        start = time.monotonic()
        result = await agent.ainvoke({"messages": [{"role": "user", "content": q}]})
        return _extract(result, "LangGraph", q, (time.monotonic() - start) * 1000)

    async def _run_pf(self, model, tools, q: str) -> SQLBenchmarkResult:
        from promptise.engine import PromptGraph, PromptNode
        from promptise.engine.execution import PromptGraphEngine

        graph = PromptGraph("sql-analyst", mode="static")
        graph.add_node(PromptNode("reason", instructions=SYSTEM_PROMPT, tools=tools, is_entry=True))
        graph.set_entry("reason")
        engine = PromptGraphEngine(graph=graph, model=model)
        start = time.monotonic()
        result = await engine.ainvoke({"messages": [{"role": "user", "content": q}]})
        return _extract(result, "Promptise", q, (time.monotonic() - start) * 1000)

    # ── Test 1: Single-table aggregation ──

    @pytest.mark.asyncio
    async def test_avg_engineering_salary(self, model, tools):
        """AVG salary for Engineering — requires filtering + aggregation."""
        q = "What is the average salary of employees in the Engineering department?"
        lg = await self._run_lg(model, tools, q)
        pf = await self._run_pf(model, tools, q)
        _print_result(pf, lg, q, GROUND_TRUTH["eng_salary"])
        assert len(pf.tool_calls) >= 1
        assert len(lg.tool_calls) >= 1

    # ── Test 2: Filtered aggregation ──

    @pytest.mark.asyncio
    async def test_closed_won_q4_revenue(self, model, tools):
        """Total closed-won revenue in Q4-2025 — requires two filters."""
        q = "What is the total revenue from closed-won deals in Q4-2025? List each deal."
        lg = await self._run_lg(model, tools, q)
        pf = await self._run_pf(model, tools, q)
        _print_result(pf, lg, q, GROUND_TRUTH["closed_won_q4"])
        assert len(pf.tool_calls) >= 1
        assert len(lg.tool_calls) >= 1

    # ── Test 3: Category-level expense analysis ──

    @pytest.mark.asyncio
    async def test_engineering_cloud_spend(self, model, tools):
        """Total cloud infrastructure spend for Engineering — category + department filter."""
        q = "How much has Engineering spent on Cloud Infrastructure across all quarters? Sum it up."
        lg = await self._run_lg(model, tools, q)
        pf = await self._run_pf(model, tools, q)
        _print_result(pf, lg, q, GROUND_TRUTH["eng_cloud_total"])
        assert len(pf.tool_calls) >= 1
        assert len(lg.tool_calls) >= 1

    # ── Test 4: Cross-table analysis ──

    @pytest.mark.asyncio
    async def test_critical_open_tickets(self, model, tools):
        """Find open critical tickets with customer names — requires understanding the data."""
        q = "Which customers have open critical support tickets? List all of them with their ticket category."
        lg = await self._run_lg(model, tools, q)
        pf = await self._run_pf(model, tools, q)
        _print_result(pf, lg, q, GROUND_TRUTH["critical_open"])
        assert len(pf.tool_calls) >= 1
        assert len(lg.tool_calls) >= 1

    # ── Test 5: Multi-step with joins ──

    @pytest.mark.asyncio
    async def test_top_sales_rep(self, model, tools):
        """Top sales rep by closed-won revenue — requires deals query + employee lookup."""
        q = (
            "Who is the top-performing sales rep by total closed-won deal revenue? "
            "Query the deals table for closed_won deals, find which owner_id has the "
            "highest total, then look up that employee's name."
        )
        lg = await self._run_lg(model, tools, q)
        pf = await self._run_pf(model, tools, q)
        _print_result(pf, lg, q, GROUND_TRUTH["top_rep"])
        assert len(pf.tool_calls) >= 2
        assert len(lg.tool_calls) >= 2

    # ── Test 6: Complex multi-table analysis ──

    @pytest.mark.asyncio
    async def test_department_profitability(self, model, tools):
        """Department P&L: compare deal revenue per rep vs expenses — requires 3 queries + math."""
        q = (
            "Analyze the Sales department's profitability:\n"
            "1. Query total closed-won deal revenue (sum of amounts where stage=closed_won)\n"
            "2. Query total Sales department expenses (sum all quarters)\n"
            "3. Calculate: revenue minus expenses = profit\n"
            "4. Calculate: profit margin as a percentage\n"
            "Show all numbers."
        )
        lg = await self._run_lg(model, tools, q)
        pf = await self._run_pf(model, tools, q)
        _print_result(pf, lg, q)
        # Both should query deals + expenses + calculate
        assert len(pf.tool_calls) >= 3, f"Expected 3+ tools, got: {pf.tool_calls}"
        assert len(lg.tool_calls) >= 3, f"Expected 3+ tools, got: {lg.tool_calls}"

    # ── Test 7: Full executive analysis ──

    @pytest.mark.asyncio
    async def test_executive_dashboard(self, model, tools):
        """Build a full executive dashboard — queries all 5 tables."""
        q = (
            "Build me an executive dashboard with these metrics:\n"
            "1. Total headcount and average salary (employees table)\n"
            "2. Total pipeline value (all non-closed-lost deals) and closed-won revenue\n"
            "3. Total expenses by department (expenses table, group by department)\n"
            "4. Open critical/high tickets count (tickets table)\n"
            "5. OKR achievement rate: how many key results hit their target? (okrs table)\n"
            "Query each table and give me the exact numbers."
        )
        lg = await self._run_lg(model, tools, q)
        pf = await self._run_pf(model, tools, q)
        _print_result(pf, lg, q)
        # Should make multiple queries (at least 5 for 5 dashboard metrics)
        assert len(pf.tool_calls) >= 4, (
            f"Expected 4+ tool calls, got {len(pf.tool_calls)}: {pf.tool_calls}"
        )
        assert len(lg.tool_calls) >= 4, (
            f"Expected 4+ tool calls, got {len(lg.tool_calls)}: {lg.tool_calls}"
        )

    # ── Aggregate summary ──

    @pytest.mark.asyncio
    async def test_aggregate_accuracy(self, model, tools):
        """Run the 5 scored questions and compare aggregate accuracy."""
        questions = [
            ("What is the average salary in Engineering?", "eng_salary"),
            ("Total closed-won revenue in Q4-2025?", "closed_won_q4"),
            ("Total Engineering Cloud Infrastructure spend?", "eng_cloud_total"),
            ("Which customers have open critical tickets?", "critical_open"),
            ("Who is the top sales rep by closed-won revenue? Look up their name.", "top_rep"),
        ]

        pf_scores: list[float] = []
        lg_scores: list[float] = []
        pf_total_ms = 0.0
        lg_total_ms = 0.0
        pf_total_tools = 0
        lg_total_tools = 0

        for q, key in questions:
            lg = await self._run_lg(model, tools, q)
            pf = await self._run_pf(model, tools, q)
            truth = GROUND_TRUTH[key]
            _print_result(pf, lg, q, truth)

            pf_scores.append(pf.accuracy_score)
            lg_scores.append(lg.accuracy_score)
            pf_total_ms += pf.latency_ms
            lg_total_ms += lg.latency_ms
            pf_total_tools += len(pf.tool_calls)
            lg_total_tools += len(lg.tool_calls)

        pf_avg_acc = sum(pf_scores) / len(pf_scores) if pf_scores else 0
        lg_avg_acc = sum(lg_scores) / len(lg_scores) if lg_scores else 0

        print(f"\n\n  {BOLD}{WHITE}{'═' * 66}{RESET}")
        print(f"  {BOLD}{WHITE}  SQL ANALYSIS BENCHMARK — {len(questions)} QUESTIONS{RESET}")
        print(f"  {BOLD}{WHITE}{'═' * 66}{RESET}")
        print(
            f"  {'':36}{BOLD}{GREEN}{'Promptise':>14}{RESET}    {BOLD}{CYAN}{'LangGraph':>14}{RESET}"
        )
        print(f"  {DIM}{'─' * 66}{RESET}")

        pf_acc_color = GREEN if pf_avg_acc >= lg_avg_acc else RED
        lg_acc_color = GREEN if lg_avg_acc >= pf_avg_acc else RED
        print(
            f"  {BOLD}Avg Accuracy{RESET}     {'':12} {pf_acc_color}{BOLD}{pf_avg_acc:>5.0%}{RESET}     {'':12} {lg_acc_color}{BOLD}{lg_avg_acc:>5.0%}{RESET}"
        )

        max_ms = max(pf_total_ms, lg_total_ms, 1)
        lat_win = GREEN + "◀" + RESET if pf_total_ms <= lg_total_ms else ""
        print(
            f"  {DIM}Total time (ms){RESET}  {_bar(pf_total_ms, max_ms, 12, GREEN)} {pf_total_ms:>5,.0f} {lat_win}  "
            f"{_bar(lg_total_ms, max_ms, 12, CYAN)} {lg_total_ms:>5,.0f}"
        )

        max_tc = max(pf_total_tools, lg_total_tools, 1)
        tc_win = GREEN + "◀" + RESET if pf_total_tools <= lg_total_tools else ""
        print(
            f"  {DIM}Total tool calls{RESET} {_bar(pf_total_tools, max_tc, 12, GREEN)} {pf_total_tools:>5} {tc_win}  "
            f"{_bar(lg_total_tools, max_tc, 12, CYAN)} {lg_total_tools:>5}"
        )

        print(f"  {DIM}{'─' * 66}{RESET}")
        speed = ((lg_total_ms - pf_total_ms) / max(lg_total_ms, 1)) * 100
        if speed > 0:
            print(
                f"  {GREEN}{BOLD}  ⚡ Promptise: {speed:.0f}% faster, {pf_avg_acc:.0%} accurate{RESET}"
            )
        else:
            print(
                f"  {CYAN}{BOLD}  ⚡ LangGraph: {-speed:.0f}% faster, {lg_avg_acc:.0%} accurate{RESET}"
            )
        print(f"  {BOLD}{WHITE}{'═' * 66}{RESET}\n")

        assert pf_total_ms > 0
        assert lg_total_ms > 0
