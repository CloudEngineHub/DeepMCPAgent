"""Test agents without calling a model provider.

:class:`ScriptedModel` is a chat model that answers with replies you write in
advance, one per model call, and records every request it gets.  Give it to
``build_agent(model=...)`` and the agent runs as it would with a real model:
tools are bound and called, results go back to the model, streaming works.
Nothing leaves the process and no API key is needed.

A reply is one of:

- a string: the model's text with no tool calls (a final answer);
- a :class:`ToolCall`, or a list of them: a turn that calls tools (a string
  in the list is the turn's text);
- a :class:`Malformed` reply: tool calls in the broken shapes small models
  send (tool-call JSON as text, arguments that are not valid JSON);
- a LangChain ``AIMessage``, used as it is.

A tool name the agent doesn't have and arguments that don't match the tool's
schema are plain :class:`ToolCall` replies.

Example::

    from promptise import build_agent, tool
    from promptise.testing import ScriptedModel, ToolCall

    @tool
    def get_order(order_id: str) -> dict:
        \"\"\"Look up an order.\"\"\"
        return {"order_id": order_id, "status": "shipped"}

    model = ScriptedModel([
        ToolCall("get_order", {"order_id": "A-1001"}),
        "Order A-1001 has shipped.",
    ])
    agent = await build_agent(model=model, extra_tools=[get_order])
    result = await agent.ainvoke({"messages": [{"role": "user", "content": "Where is A-1001?"}]})
    assert model.calls[1].messages[-1].content == '{"order_id": "A-1001", "status": "shipped"}'
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Union

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, PrivateAttr, field_validator

__all__ = [
    "Malformed",
    "ModelCall",
    "Reply",
    "ScriptExhaustedError",
    "ScriptedModel",
    "ToolCall",
]


@dataclass(frozen=True)
class ToolCall:
    """A tool call in a scripted reply.

    Args:
        name: The tool to call.  A name the agent has no tool for tests how
            the agent answers an invented tool.
        args: The call's arguments.  Arguments that don't match the tool's
            schema test how the agent answers invalid ones.
        id: The tool call's id.  Left out, the model numbers its calls
            ``call_1``, ``call_2`` ... in the order it sends them.
    """

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    id: str | None = None


_TEXT_STYLES = ("json", "tagged", "fenced")


@dataclass(frozen=True)
class Malformed:
    """A reply in a broken shape that small models send.

    Build one with :meth:`tool_call_as_text` or
    :meth:`unparseable_arguments`.
    """

    text: str = ""
    invalid_calls: tuple[tuple[str, str, str | None], ...] = ()

    @classmethod
    def tool_call_as_text(
        cls,
        name: str,
        args: Mapping[str, Any] | None = None,
        *,
        style: Literal["json", "tagged", "fenced"] = "json",
    ) -> Malformed:
        """A tool call written into the reply's text instead of sent as one.

        The reply carries no tool call, so the agent takes the text as the
        model's answer.

        Args:
            name: The tool name in the text.
            args: The arguments in the text.
            style: ``"json"`` for the bare object
                (``{"name": ..., "arguments": {...}}``), ``"tagged"`` for the
                object inside ``<tool_call>`` tags, ``"fenced"`` for the
                object in a Markdown code block.
        """
        if style not in _TEXT_STYLES:
            raise ValueError(f"style must be one of {_TEXT_STYLES}, not {style!r}")
        body = json.dumps({"name": name, "arguments": dict(args or {})})
        if style == "tagged":
            body = f"<tool_call>\n{body}\n</tool_call>"
        elif style == "fenced":
            body = f"```json\n{body}\n```"
        return cls(text=body)

    @classmethod
    def unparseable_arguments(
        cls,
        name: str,
        raw_args: str = "{'arg': 'value'}",
        *,
        id: str | None = None,
    ) -> Malformed:
        """A tool call whose arguments are not valid JSON.

        LangChain keeps such a call in the message's ``invalid_tool_calls``
        rather than its ``tool_calls``, as it does for a provider's reply.
        When the reply is streamed, LangChain reads the arguments as a
        partial JSON object, as it does for a provider's stream: a cut-off
        object such as ``'{"order_id"'`` becomes a tool call with the
        arguments read so far, and text that can't start a JSON object stays
        an invalid call.

        Args:
            name: The tool the call names.
            raw_args: The argument text, as the model sent it.  The default
                uses Python-style quotes, which JSON does not allow.
            id: The tool call's id (numbered like :class:`ToolCall` ids when
                left out).
        """
        return cls(invalid_calls=((name, raw_args, id),))


Reply = Union[str, ToolCall, Malformed, AIMessage, Sequence[Union[str, ToolCall]]]
"""What a :class:`ScriptedModel` reply can be."""


@dataclass(frozen=True)
class ModelCall:
    """One request a :class:`ScriptedModel` received.

    Attributes:
        messages: The messages the model was sent, in order.
        tools: The names of the tools bound to the model for this call.
        tool_schemas: Those tools as the model was offered them
            (OpenAI-style function definitions).
        reply: The message the model answered with, ``None`` when it had no
            reply left.
    """

    messages: list[BaseMessage]
    tools: list[str]
    tool_schemas: list[dict[str, Any]]
    reply: AIMessage | None


class ScriptExhaustedError(RuntimeError):
    """A :class:`ScriptedModel` was called after its last reply."""


def _check_reply(reply: Any, index: int) -> None:
    if isinstance(reply, (str, ToolCall, Malformed, AIMessage)):
        return
    if isinstance(reply, Sequence) and not isinstance(reply, (bytes, bytearray)):
        for item in reply:
            if not isinstance(item, (str, ToolCall)):
                raise TypeError(
                    f"reply {index}: a list reply holds ToolCall objects (and text), "
                    f"not {type(item).__name__}"
                )
        return
    raise TypeError(
        f"reply {index}: expected a string, a ToolCall, a list of ToolCall, a Malformed "
        f"reply or an AIMessage, not {type(reply).__name__}"
    )


def _tool_names(schemas: list[dict[str, Any]]) -> list[str]:
    names = []
    for schema in schemas:
        function = schema.get("function") if isinstance(schema, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        names.append(str(name) if name is not None else "")
    return names


class ScriptedModel(BaseChatModel):
    """A chat model that answers each call with the next scripted reply.

    Use it wherever a model is accepted (``build_agent(model=...)``, a
    graph node's model) to run an agent without a provider.

    Args:
        replies: The replies, in the order the model sends them (see the
            module docs for the forms a reply takes).
        on_exhausted: What a call after the last reply does: ``"raise"``
            (the default) raises :class:`ScriptExhaustedError`;
            ``"repeat_last"`` answers with the last reply again.

    Attributes:
        calls: A :class:`ModelCall` for every request, in order.
    """

    replies: list[Any] = Field(default_factory=list)
    on_exhausted: Literal["raise", "repeat_last"] = "raise"
    model_name: str = "scripted"
    calls: list[ModelCall] = Field(default_factory=list, repr=False)

    _position: int = PrivateAttr(default=0)
    _call_ids: int = PrivateAttr(default=0)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def __init__(
        self,
        replies: Sequence[Reply] | None = None,
        /,
        *,
        on_exhausted: Literal["raise", "repeat_last"] = "raise",
        **kwargs: Any,
    ) -> None:
        if replies is not None:
            if "replies" in kwargs:
                raise TypeError("give the replies once, positionally or as replies=")
            kwargs["replies"] = replies
        kwargs["on_exhausted"] = on_exhausted
        # A scripted reply must never come from LangChain's response cache.
        kwargs.setdefault("cache", False)
        super().__init__(**kwargs)

    @field_validator("replies", mode="before")
    @classmethod
    def _validate_replies(cls, value: Any) -> list[Any]:
        if isinstance(value, (str, ToolCall, Malformed, AIMessage)):
            raise TypeError("replies is a list of replies; wrap a single reply in a list")
        replies = list(value)
        for index, reply in enumerate(replies):
            _check_reply(reply, index)
        return replies

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    @property
    def remaining(self) -> int:
        """How many scripted replies have not been sent yet."""
        return max(len(self.replies) - self._position, 0)

    def bind_tools(  # type: ignore[override]
        self,
        tools: Sequence[Any],
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Bind *tools* the way provider models do; the scripted replies
        don't depend on them, :attr:`calls` records them."""
        schemas = [convert_to_openai_tool(t) for t in tools]
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return self.bind(tools=schemas, **kwargs)

    # ── replies ──

    def _next_id(self) -> str:
        self._call_ids += 1
        return f"call_{self._call_ids}"

    def _message(self, reply: Any) -> AIMessage:
        if isinstance(reply, AIMessage):
            return reply.model_copy(deep=True)
        if isinstance(reply, str):
            return AIMessage(content=reply)
        if isinstance(reply, Malformed):
            return AIMessage(
                content=reply.text,
                invalid_tool_calls=[
                    {
                        "type": "invalid_tool_call",
                        "name": name,
                        "args": raw,
                        "id": call_id or self._next_id(),
                        "error": "the arguments are not valid JSON",
                    }
                    for name, raw, call_id in reply.invalid_calls
                ],
            )
        items = [reply] if isinstance(reply, ToolCall) else list(reply)
        text = "".join(item for item in items if isinstance(item, str))
        return AIMessage(
            content=text,
            tool_calls=[
                {
                    "type": "tool_call",
                    "name": item.name,
                    "args": dict(item.args),
                    "id": item.id or self._next_id(),
                }
                for item in items
                if isinstance(item, ToolCall)
            ],
        )

    def _respond(self, messages: list[BaseMessage], kwargs: dict[str, Any]) -> AIMessage:
        """Record the call and return its reply (or raise when there is none)."""
        schemas = list(kwargs.get("tools") or [])
        with self._lock:
            number = len(self.calls) + 1
            if self._position < len(self.replies):
                reply: Any = self.replies[self._position]
                self._position += 1
            elif self.on_exhausted == "repeat_last" and self.replies:
                reply = self.replies[-1]
            else:
                reply = None
            message = None if reply is None else self._message(reply)
            self.calls.append(
                ModelCall(
                    messages=list(messages),
                    tools=_tool_names(schemas),
                    tool_schemas=schemas,
                    reply=message,
                )
            )
        if message is None:
            raise ScriptExhaustedError(
                f"ScriptedModel has no reply for model call {number}: the script has "
                f"{len(self.replies)} {'reply' if len(self.replies) == 1 else 'replies'}. "
                "Add a reply, or pass on_exhausted='repeat_last' to repeat the last one."
            )
        return message

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._respond(messages, kwargs))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, None, **kwargs)

    def _chunks(self, message: AIMessage) -> Iterator[AIMessageChunk]:
        """*message* as stream chunks: its text word by word, then each tool
        call (valid or not) as a tool-call chunk."""
        text = message.content if isinstance(message.content, str) else ""
        for word in re.findall(r"\s*\S+\s*", text) or ([text] if text else []):
            yield AIMessageChunk(content=word)
        calls: list[tuple[str, str, str | None]] = [
            (tc["name"], json.dumps(tc["args"]), tc.get("id")) for tc in message.tool_calls
        ]
        calls += [
            (itc.get("name") or "", itc.get("args") or "", itc.get("id"))
            for itc in message.invalid_tool_calls
        ]
        for index, (name, args, call_id) in enumerate(calls):
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "type": "tool_call_chunk",
                        "name": name,
                        "args": args,
                        "id": call_id,
                        "index": index,
                    }
                ],
            )
        if not text and not calls:
            yield AIMessageChunk(content="")

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        # BaseChatModel reports each chunk to the callbacks itself.
        for chunk in self._chunks(self._respond(messages, kwargs)):
            yield ChatGenerationChunk(message=chunk)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._chunks(self._respond(messages, kwargs)):
            yield ChatGenerationChunk(message=chunk)
