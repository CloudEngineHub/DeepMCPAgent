"""Compaction must not make a model re-read the same results without end.

Reproduces the agent-harness-python report (gpt-4.1-mini on a 30-SKU audit)
with a scripted model: per-SKU records longer than ``keep_result_chars`` are
cut to an excerpt once the loop compacts, and the excerpt tells the model to
call the tool again for the full text.  A model that does so one record at a
time used to see each re-read excerpted again as soon as the next exchange
started, so it could never have all records in full and looped until its
iteration limit.  A result the model fetched again is now kept in full.

Scripted models only: no network.
"""

from __future__ import annotations

import itertools
import json
from collections import Counter
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from promptise import build_agent
from promptise.engine import ContextCompaction
from promptise.engine.compaction import (
    LEDGER_HEADER,
    build_compacted_view,
    split_input,
)

SKUS = [f"SKU-{i:02d}" for i in range(1, 7)]
LOG = "2026-09-01 cycle count in aisle 7: 21 units scanned, label check ok. " * 36
_ids = itertools.count()


def _record(sku: str) -> str:
    return json.dumps({"sku": sku, "reorder_point": 20, "on_hand": 19, "warehouse_log": LOG})


def _full_records(messages: list[BaseMessage]) -> set[str]:
    """SKUs whose stock record this request shows in full."""
    full: set[str] = set()
    for m in messages:
        if isinstance(m, ToolMessage) and '"warehouse_log"' in str(m.content):
            full.add(json.loads(str(m.content))["sku"])
        if isinstance(m, SystemMessage) and LEDGER_HEADER in str(m.content):
            for line in str(m.content).splitlines():
                if line.startswith("- [") and "get_stock" in line:
                    if "[shortened" not in line and "[result not shown" not in line:
                        full.add(json.loads(line.split(") = ", 1)[1])["sku"])
    return full


class Auditor(BaseChatModel):
    """Fetches one stock record per turn, then needs every record in full at
    once; follows the excerpt's advice and re-reads one cut record per turn."""

    requests: list[int] = []

    @property
    def _llm_type(self) -> str:
        return "auditor"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Auditor:  # type: ignore[override]
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, **kw: Any) -> ChatResult:
        raise NotImplementedError("async only")

    async def _agenerate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        self.requests.append(1)
        n = len(self.requests)
        if n <= len(SKUS):
            sku = SKUS[n - 1]
        else:
            missing = [s for s in SKUS if s not in _full_records(messages)]
            if not missing:
                msg = AIMessage(content="REORDER: " + ", ".join(SKUS))
                return ChatResult(generations=[ChatGeneration(message=msg)])
            sku = missing[0]
        call = {"name": "get_stock", "args": {"sku": sku}, "id": f"s{next(_ids)}"}
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))]
        )


@pytest.mark.asyncio
async def test_default_compaction_does_not_loop_on_rereads() -> None:
    fetched: list[str] = []

    @tool
    def get_stock(sku: str) -> str:
        """Stock record for one SKU."""
        fetched.append(sku)
        return _record(sku)

    model = Auditor(requests=[])
    agent = await build_agent(servers={}, model=model, extra_tools=[get_stock])
    try:
        result = await agent.ainvoke({"messages": [HumanMessage(content="Audit all SKUs.")]})
    finally:
        await agent.shutdown()

    assert result["messages"][-1].content.startswith("REORDER:")
    # Each record is fetched once, and re-read at most once for its full text.
    assert max(Counter(fetched).values()) <= 2
    assert len(fetched) <= 2 * len(SKUS)


def _exchange(sku: str, n: int) -> list[BaseMessage]:
    call = {"name": "get_stock", "args": {"sku": sku}, "id": f"x{n}"}
    return [
        AIMessage(content="", tool_calls=[call]),
        ToolMessage(content=_record(sku), tool_call_id=f"x{n}"),
    ]


def test_reread_result_stays_in_full_and_excerpt_says_what_was_cut() -> None:
    question = HumanMessage(content="Audit.")
    messages: list[BaseMessage] = [question]
    for n, sku in enumerate(["SKU-01", "SKU-02", "SKU-01", "SKU-03"]):
        messages += _exchange(sku, n)
    q, head = split_input(messages)
    view = build_compacted_view(
        messages, question=q, head=head, settings=ContextCompaction(keep_result_chars=200)
    )
    ledger = next(str(m.content) for m in view if LEDGER_HEADER in str(m.content))
    lines = {line.split(") = ")[0]: line for line in ledger.splitlines() if line.startswith("- [")}
    sku1 = next(v for k, v in lines.items() if "SKU-01" in k)
    sku2 = next(v for k, v in lines.items() if "SKU-02" in k)
    # SKU-01 was fetched again for its full text: shown in full.
    assert _record("SKU-01") in sku1
    assert "[shortened" not in sku1
    # SKU-02 is cut; the excerpt says how much and that a re-read is kept.
    assert "[shortened" in sku2
    assert "kept in full" in sku2
    # The header no longer invites re-reading any value that "changes over time".
    assert "changes over time" not in ledger


def test_token_budget_shrinks_reread_results_last() -> None:
    question = HumanMessage(content="Audit.")
    messages: list[BaseMessage] = [question]
    for n, sku in enumerate(["SKU-01", "SKU-02", "SKU-01", "SKU-03", "SKU-04"]):
        messages += _exchange(sku, n)
    q, head = split_input(messages)
    settings = ContextCompaction(keep_result_chars=200, max_tokens=1550)
    view = build_compacted_view(messages, question=q, head=head, settings=settings)
    ledger = next(str(m.content) for m in view if LEDGER_HEADER in str(m.content))
    sku1 = next(line for line in ledger.splitlines() if "SKU-01" in line)
    sku2 = next(line for line in ledger.splitlines() if "SKU-02" in line)
    assert "[result not shown" in sku2
    assert _record("SKU-01") in sku1
