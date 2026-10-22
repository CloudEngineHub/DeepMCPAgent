"""Prompt observation (observe=), inspector context/guard records, token
stats, and ConversationFlow transitions requested from a handler."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from promptise.observability import ObservabilityCollector, TimelineEventType
from promptise.prompts import ConversationFlow, PromptBuilder, PromptObserver, phase, prompt
from promptise.prompts.guards import GuardError, content_filter
from promptise.prompts.inspector import PromptInspector


class _Upper:
    async def provide(self, ctx: Any) -> str:
        return "CONTEXT"


def _llm(reply: str, *, usage: dict[str, int] | None = None) -> AsyncMock:
    message = AIMessage(content=reply)
    if usage is not None:
        message.usage_metadata = {**usage, "total_tokens": sum(usage.values())}  # type: ignore[assignment]
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=message)
    return model


def _types(collector: ObservabilityCollector) -> list[TimelineEventType]:
    return [e.event_type for e in collector.get_timeline()]


# ---------------------------------------------------------------------------
# observe=
# ---------------------------------------------------------------------------


async def test_observe_true_records_start_and_end() -> None:
    @prompt(model="openai:gpt-5-mini", observe=True)
    async def summarize(text: str) -> str:
        """Summarize: {text}"""

    with patch("promptise.prompts.core.init_chat_model", return_value=_llm("short")):
        await summarize("long text")

    assert isinstance(summarize.observer, PromptObserver)
    timeline = summarize.observer.collector.get_timeline()
    assert [e.event_type for e in timeline] == [
        TimelineEventType.PROMPT_START,
        TimelineEventType.PROMPT_END,
    ]
    assert timeline[0].agent_id == "summarize"
    assert timeline[1].metadata["output_length"] == len("short")


async def test_observe_with_a_shared_collector_records_context_and_guard_blocks() -> None:
    collector = ObservabilityCollector()

    @prompt(model="openai:gpt-5-mini", observe=collector)
    async def say(text: str) -> str:
        """Say: {text}"""

    guarded = say.with_context(_Upper()).with_guards(content_filter(blocked=["secret"]))
    with patch("promptise.prompts.core.init_chat_model", return_value=_llm("fine")):
        await guarded("hello")
        with pytest.raises(GuardError):
            await guarded("the secret")

    assert _types(collector) == [
        TimelineEventType.PROMPT_START,
        TimelineEventType.PROMPT_CONTEXT,
        TimelineEventType.PROMPT_END,
        TimelineEventType.PROMPT_START,
        TimelineEventType.PROMPT_CONTEXT,
        TimelineEventType.PROMPT_GUARD_BLOCK,
        TimelineEventType.PROMPT_ERROR,
    ]
    context = collector.get_timeline()[1].metadata
    assert context == {"prompt_name": "say", "provider": "_Upper", "chars_injected": 7}


async def test_unobserved_prompt_has_no_observer() -> None:
    @prompt(model="openai:gpt-5-mini")
    async def plain(text: str) -> str:
        """{text}"""

    assert plain.observer is None


async def test_with_observer_and_builder_and_yaml(tmp_path: Any) -> None:
    from promptise.prompts.loader import load_prompt

    collector = ObservabilityCollector()

    @prompt(model="openai:gpt-5-mini")
    async def plain(text: str) -> str:
        """{text}"""

    built = PromptBuilder("built").template("Hi {name}").observe().build()
    path = tmp_path / "greet.prompt"
    path.write_text("name: greet\ntemplate: 'Hello {name}'\nobserve: true\n")
    loaded = load_prompt(path)

    with patch("promptise.prompts.core.init_chat_model", return_value=_llm("ok")):
        await plain.with_observer(collector)("x")
        await built(name="a")
        await loaded(name="b")

    assert _types(collector) == [TimelineEventType.PROMPT_START, TimelineEventType.PROMPT_END]
    assert _types(built.observer.collector)[0] == TimelineEventType.PROMPT_START
    assert _types(loaded.observer.collector)[-1] == TimelineEventType.PROMPT_END


async def test_failing_observer_does_not_break_the_call() -> None:
    class Broken:
        def record_start(self, *args: Any) -> None:
            raise RuntimeError("telemetry down")

        record_end = record_error = record_context = record_guard_block = record_start

    @prompt(model="openai:gpt-5-mini", observe=Broken())
    async def say(text: str) -> str:
        """{text}"""

    with patch("promptise.prompts.core.init_chat_model", return_value=_llm("ok")):
        assert await say("x") == "ok"


def test_observe_rejects_other_values() -> None:
    with pytest.raises(TypeError, match="observe must be"):

        @prompt(model="openai:gpt-5-mini", observe="yes")
        async def say(text: str) -> str:
            """{text}"""


# ---------------------------------------------------------------------------
# Inspector and stats
# ---------------------------------------------------------------------------


async def test_inspector_records_context_providers_and_input_guards() -> None:
    inspector = PromptInspector()

    @prompt(model="openai:gpt-5-mini", inspect=inspector)
    async def say(text: str) -> str:
        """Say: {text}"""

    from promptise.prompts.guards import length

    configured = say.with_context(_Upper())
    configured._input_guards.append(length(max_length=10_000))  # an input-only guard
    with patch("promptise.prompts.core.init_chat_model", return_value=_llm("fine")):
        await configured("hello")
    trace = inspector.last()
    assert [(c.provider_name, c.chars_injected) for c in trace.context_providers] == [("_Upper", 7)]
    assert trace.guards_passed == ["LengthGuard"]

    await configured.render_async(text="hi")
    assert [c.provider_name for c in inspector.last().context_providers] == ["_Upper"]


async def test_guard_passing_input_but_failing_output_is_listed_as_failed() -> None:
    inspector = PromptInspector()

    @prompt(model="openai:gpt-5-mini", inspect=inspector)
    async def say(text: str) -> str:
        """Say: {text}"""

    guarded = say.with_guards(content_filter(blocked=["secret"]))
    with patch("promptise.prompts.core.init_chat_model", return_value=_llm("the secret")):
        with pytest.raises(GuardError):
            await guarded("hello")
    trace = inspector.last()
    assert trace.guards_passed == []
    assert trace.guards_failed == ["ContentFilterGuard"]


async def test_stats_have_token_counts_from_the_reply() -> None:
    @prompt(model="openai:gpt-5-mini")
    async def say(text: str) -> str:
        """{text}"""

    llm = _llm("ok", usage={"input_tokens": 12, "output_tokens": 3})
    with patch("promptise.prompts.core.init_chat_model", return_value=llm):
        await say("x")
    assert (say.last_stats.input_tokens, say.last_stats.output_tokens) == (12, 3)

    with patch("promptise.prompts.core.init_chat_model", return_value=_llm("ok")):
        await say("x")
    assert (say.last_stats.input_tokens, say.last_stats.output_tokens) == (0, 0)


# ---------------------------------------------------------------------------
# ConversationFlow transitions
# ---------------------------------------------------------------------------


class _Chain(ConversationFlow):
    def __init__(self) -> None:
        super().__init__()
        self.ran: list[str] = []

    @phase("a", initial=True)
    async def a(self, ctx: Any) -> None:
        self.ran.append("a")

    @phase("b")
    async def b(self, ctx: Any) -> None:
        self.ran.append("b")
        ctx.transition("c")

    @phase("c")
    async def c(self, ctx: Any) -> None:
        self.ran.append("c")


async def test_transition_runs_the_handler_of_a_phase_requested_by_a_handler() -> None:
    flow = _Chain()
    await flow.start()
    await flow.transition("b")
    assert flow.current_phase == "c"
    assert flow.ran == ["a", "b", "c"]


async def test_next_turn_follows_chained_transitions() -> None:
    class Flow(ConversationFlow):
        ran: list[str] = []

        @phase("a", initial=True)
        async def a(self, ctx: Any) -> None:
            self.ran.append("a")
            if ctx.turn:
                ctx.transition("b")

        @phase("b")
        async def b(self, ctx: Any) -> None:
            self.ran.append("b")
            ctx.transition("c")

        @phase("c")
        async def c(self, ctx: Any) -> None:
            self.ran.append("c")

    flow = Flow()
    await flow.start()
    await flow.next_turn("go")
    assert flow.current_phase == "c"
    assert flow.ran == ["a", "a", "b", "c"]


async def test_circular_transitions_hit_the_depth_limit() -> None:
    class Loop(ConversationFlow):
        @phase("ping", initial=True)
        async def ping(self, ctx: Any) -> None:
            ctx.transition("pong")

        @phase("pong")
        async def pong(self, ctx: Any) -> None:
            ctx.transition("ping")

    flow = Loop()
    with pytest.raises(RecursionError, match="depth limit"):
        await flow.start()
    # The counter is reset: a later call is not refused because of the first
    assert flow._transition_depth == 0
    with pytest.raises(RecursionError):
        await flow.transition("pong")
