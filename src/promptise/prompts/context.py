"""Extensible context system — the agent's world.

Defines :class:`BaseContext` (the extensible foundation), predefined
context classes, :class:`PromptContext` (the complete world), and
built-in :class:`ContextProvider` implementations.

Example::

    from promptise.prompts import prompt, context
    from promptise.prompts.context import (
        UserContext, tool_context, memory_context, user_context,
    )

    @prompt(model="openai:gpt-5-mini")
    @context(tool_context(), memory_context(), user_context())
    async def analyze(text: str) -> str:
        \"\"\"Based on available context, analyze: {text}\"\"\"
"""

from __future__ import annotations

import time
from collections.abc import Callable, KeysView
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    # Base + world classes
    "BaseContext",
    "UserContext",
    "EnvironmentContext",
    "ConversationContext",
    "TeamContext",
    "ErrorContext",
    "OutputContext",
    "PromptContext",
    # Provider protocol
    "ContextProvider",
    # Built-in providers
    "ToolContextProvider",
    "MemoryContextProvider",
    "TaskContextProvider",
    "BlackboardContextProvider",
    "UserContextProvider",
    "EnvironmentContextProvider",
    "ConversationContextProvider",
    "TeamContextProvider",
    "ErrorContextProvider",
    "OutputContextProvider",
    "StaticContextProvider",
    "CallableContextProvider",
    "ConditionalContextProvider",
    "WorldContextProvider",
    # Convenience constructors
    "tool_context",
    "memory_context",
    "task_context",
    "blackboard_context",
    "user_context",
    "env_context",
    "conversation_context",
    "team_context",
    "error_context",
    "output_context",
    "static_context",
    "callable_context",
    "conditional_context",
    "world_context",
    # Decorator
    "context",
]


# ===================================================================
# BaseContext — extensible foundation
# ===================================================================


class BaseContext:
    """Extensible context container.

    Accepts arbitrary keyword arguments.  Predefined subclasses add
    typed convenience fields but NEVER restrict what developers can
    store.

    Example::

        # Predefined fields
        user = UserContext(user_id="123", name="Alice")

        # Custom fields — no subclassing needed
        user = UserContext(user_id="123", department="eng", clearance="high")

        # Entirely custom context
        project = BaseContext(sprint="2026-Q1", budget=50000)

        # Access
        project.sprint            # "2026-Q1"
        project["budget"]         # 50000
        project.get("missing")    # None

        # Extend after creation
        project.deadline = "March 2026"

        # Merge
        combined = project.merge(BaseContext(team_size=5))
    """

    def __init__(self, **kwargs: Any) -> None:
        object.__setattr__(self, "_data", kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        data: dict[str, Any] = object.__getattribute__(self, "_data")
        return data.get(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            data: dict[str, Any] = object.__getattribute__(self, "_data")
            data[name] = value

    def __getitem__(self, key: str) -> Any:
        return object.__getattribute__(self, "_data")[key]

    def __setitem__(self, key: str, value: Any) -> None:
        object.__getattribute__(self, "_data")[key] = value

    def __contains__(self, key: str) -> bool:
        return key in object.__getattribute__(self, "_data")

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value by key with optional default."""
        return object.__getattribute__(self, "_data").get(key, default)

    def to_dict(self) -> dict[str, Any]:
        """Return a shallow copy of the stored data."""
        return dict(object.__getattribute__(self, "_data"))

    def merge(self, other: BaseContext) -> BaseContext:
        """Return new context with merged data (*other* overrides *self*)."""
        merged_data = dict(object.__getattribute__(self, "_data"))
        merged_data.update(object.__getattribute__(other, "_data"))
        return self.__class__(**merged_data)

    def keys(self) -> KeysView[str]:
        """Return the context keys."""
        return object.__getattribute__(self, "_data").keys()

    def __bool__(self) -> bool:
        return bool(object.__getattribute__(self, "_data"))

    def __repr__(self) -> str:
        data = object.__getattribute__(self, "_data")
        fields = ", ".join(f"{k}={v!r}" for k, v in data.items())
        return f"{self.__class__.__name__}({fields})"


# ===================================================================
# Predefined context classes (all accept **kwargs)
# ===================================================================


class UserContext(BaseContext):
    """Who the agent is serving.  Extend with any user-specific fields.

    Args:
        user_id: Unique user identifier.
        name: Display name.
        preferences: User preference dict.
        expertise_level: ``"beginner"``, ``"intermediate"``, or ``"expert"``.
        language: Preferred language.
        **kwargs: Any additional fields.
    """

    def __init__(
        self,
        user_id: str = "",
        name: str = "",
        preferences: dict[str, Any] | None = None,
        expertise_level: str = "intermediate",
        language: str = "english",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            user_id=user_id,
            name=name,
            preferences=preferences or {},
            expertise_level=expertise_level,
            language=language,
            **kwargs,
        )


class EnvironmentContext(BaseContext):
    """Runtime environment.  Extend with deployment-specific fields.

    Args:
        timestamp: Epoch timestamp (defaults to now).
        timezone: IANA timezone string.
        platform: OS platform (darwin, linux, windows).
        available_apis: List of available API identifiers.
        **kwargs: Any additional fields.
    """

    def __init__(
        self,
        timestamp: float | None = None,
        timezone: str = "",
        platform: str = "",
        available_apis: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            timestamp=timestamp if timestamp is not None else time.time(),
            timezone=timezone,
            platform=platform,
            available_apis=available_apis or [],
            **kwargs,
        )


class ConversationContext(BaseContext):
    """Conversation history and state.

    Args:
        messages: List of ``{"role": ..., "content": ...}`` dicts.
        turn_count: Number of conversation turns.
        summary: Compressed summary of older turns.
        **kwargs: Any additional fields.
    """

    def __init__(
        self,
        messages: list[dict[str, str]] | None = None,
        turn_count: int = 0,
        summary: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            messages=messages or [],
            turn_count=turn_count,
            summary=summary,
            **kwargs,
        )


class TeamContext(BaseContext):
    """Other agents in the team.

    Args:
        agents: List of ``{"name": ..., "role": ..., "capabilities": ...}`` dicts.
        completed_tasks: List of ``{"agent": ..., "task": ..., "result_preview": ...}`` dicts.
        **kwargs: Any additional fields.
    """

    def __init__(
        self,
        agents: list[dict[str, Any]] | None = None,
        completed_tasks: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            agents=agents or [],
            completed_tasks=completed_tasks or [],
            **kwargs,
        )


class ErrorContext(BaseContext):
    """Previous errors for retry and recovery.

    Args:
        errors: List of ``{"type": ..., "message": ..., "timestamp": ...}`` dicts.
        retry_count: How many retries have been attempted.
        last_error: The most recent error message.
        **kwargs: Any additional fields.
    """

    def __init__(
        self,
        errors: list[dict[str, Any]] | None = None,
        retry_count: int = 0,
        last_error: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            errors=errors or [],
            retry_count=retry_count,
            last_error=last_error,
            **kwargs,
        )


class OutputContext(BaseContext):
    """Expected output characteristics.

    Args:
        format: Output format (``"json"``, ``"markdown"``, ``"plain"``).
        schema_description: Human-readable schema description.
        examples: List of example outputs.
        constraints: List of output constraint strings.
        **kwargs: Any additional fields.
    """

    def __init__(
        self,
        format: str = "",
        schema_description: str = "",
        examples: list[dict[str, Any]] | None = None,
        constraints: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            format=format,
            schema_description=schema_description,
            examples=examples or [],
            constraints=constraints or [],
            **kwargs,
        )


# ===================================================================
# PromptContext — the complete world
# ===================================================================


@dataclass
class PromptContext:
    """The agent's complete world during prompt execution.

    Every context provider, strategy, perspective, guard, and hook
    receives this object.  It carries everything the agent knows.

    The :attr:`world` dict holds :class:`BaseContext` instances keyed
    by name.  Predefined keys (``"user"``, ``"environment"``, etc.)
    have convenience properties.  Developers add custom contexts via
    ``world["project"] = BaseContext(...)``.
    """

    # Prompt identity
    prompt_name: str = ""
    prompt_version: str = "0.0.0"
    model: str = ""

    # Input
    input_args: dict[str, Any] = field(default_factory=dict)
    rendered_text: str = ""

    # Agent integration (set when used within Agent/Team)
    agent: Any | None = None
    task: Any | None = None
    blackboard: Any | None = None

    # World contexts — dict of BaseContext instances
    world: dict[str, BaseContext] = field(default_factory=dict)

    # Extensible state — carries data between chain steps, hooks, etc.
    state: dict[str, Any] = field(default_factory=dict)

    # Convenience properties
    @property
    def user(self) -> UserContext | None:
        """User context if set."""
        v = self.world.get("user")
        return v if isinstance(v, UserContext) else None

    @property
    def environment(self) -> EnvironmentContext | None:
        """Environment context if set."""
        v = self.world.get("environment")
        return v if isinstance(v, EnvironmentContext) else None

    @property
    def conversation(self) -> ConversationContext | None:
        """Conversation context if set."""
        v = self.world.get("conversation")
        return v if isinstance(v, ConversationContext) else None

    @property
    def team(self) -> TeamContext | None:
        """Team context if set."""
        v = self.world.get("team")
        return v if isinstance(v, TeamContext) else None

    @property
    def errors(self) -> ErrorContext | None:
        """Error context if set."""
        v = self.world.get("errors")
        return v if isinstance(v, ErrorContext) else None

    @property
    def output(self) -> OutputContext | None:
        """Output context if set."""
        v = self.world.get("output")
        return v if isinstance(v, OutputContext) else None


# ===================================================================
# ContextProvider protocol
# ===================================================================


@runtime_checkable
class ContextProvider(Protocol):
    """Pluggable source of dynamic context for prompts.

    Implement this protocol to create custom context providers.
    Return empty string to skip injection when data isn't available.
    """

    async def provide(self, ctx: PromptContext) -> str:
        """Generate context text at runtime."""
        ...


# ===================================================================
# Built-in context providers
# ===================================================================


class ToolContextProvider:
    """Placeholder for tool context injection.

    This provider intentionally returns empty string.  Tools are wired
    directly into the LangGraph graph at the agent level, not injected
    via prompt context text.  The provider exists so that ``tool_context()``
    can be used in provider lists without error; it produces no output.

    Args:
        include_schemas: Reserved for future use; currently ignored.
    """

    def __init__(self, include_schemas: bool = False) -> None:
        self._include_schemas = include_schemas

    async def provide(self, ctx: PromptContext) -> str:
        # Intentional no-op: tools are wired directly into the
        # LangGraph graph, not injected via prompt context text.
        return ""


class MemoryContextProvider:
    """Retrieve relevant memories from the agent's memory provider.

    Args:
        limit: Maximum number of memories to retrieve.
        min_score: Minimum relevance score (0.0–1.0).
    """

    def __init__(self, limit: int = 5, min_score: float = 0.3) -> None:
        self._limit = limit
        self._min_score = min_score

    async def provide(self, ctx: PromptContext) -> str:
        if ctx.agent is None:
            return ""
        provider = getattr(ctx.agent, "provider", None)
        if provider is None:
            return ""
        query = " ".join(str(v) for v in ctx.input_args.values())[:500]
        if not query.strip():
            query = ctx.rendered_text[:500]
        if not query.strip():
            return ""
        try:
            results = await provider.search(query, limit=self._limit)
        except Exception:
            return ""
        results = [r for r in results if r.score >= self._min_score]
        if not results:
            return ""
        from promptise.memory import sanitize_memory_content

        lines = [f"- {sanitize_memory_content(r.content)}" for r in results]
        return "## Relevant Context\n" + "\n".join(lines)


class TaskContextProvider:
    """Inject current task metadata (goal, description, expected output)."""

    async def provide(self, ctx: PromptContext) -> str:
        if ctx.task is None:
            return ""
        sections: list[str] = []
        goal = getattr(ctx.task, "goal", None) or getattr(ctx.task, "name", None)
        description = getattr(ctx.task, "description", None)
        expected = getattr(ctx.task, "expected_output", None)
        priority = getattr(ctx.task, "priority", None)
        if goal:
            sections.append(f"**Goal:** {goal}")
        if description:
            sections.append(f"**Task:** {description}")
        if expected:
            sections.append(f"**Expected Output:** {expected}")
        if priority:
            sections.append(f"**Priority:** {priority}")
        if not sections:
            return ""
        return "## Task Context\n" + "\n".join(sections)


class BlackboardContextProvider:
    """Inject shared state from the blackboard.

    Args:
        prefix: Key prefix to filter (e.g. ``"result."``).
    """

    def __init__(self, prefix: str = "result.") -> None:
        self._prefix = prefix

    async def provide(self, ctx: PromptContext) -> str:
        if ctx.blackboard is None:
            return ""
        try:
            data = ctx.blackboard.get_section(self._prefix)
        except Exception:
            return ""
        if not data:
            return ""
        lines = [f"**{k.removeprefix(self._prefix)}:** {v}" for k, v in data.items()]
        return "## Prior Results\n" + "\n".join(lines)


class UserContextProvider:
    """Inject user identity, preferences, and expertise level."""

    async def provide(self, ctx: PromptContext) -> str:
        user = ctx.user
        if user is None:
            return ""
        data = user.to_dict()
        if not data:
            return ""
        lines: list[str] = []
        if data.get("name"):
            lines.append(f"**User:** {data['name']}")
        if data.get("expertise_level"):
            lines.append(f"**Expertise:** {data['expertise_level']}")
        if data.get("language") and data["language"] != "english":
            lines.append(f"**Language:** {data['language']}")
        if data.get("preferences"):
            lines.append(f"**Preferences:** {data['preferences']}")
        # Include any custom fields
        skip = {"user_id", "name", "expertise_level", "language", "preferences"}
        for k, v in data.items():
            if k not in skip and v:
                lines.append(f"**{k.replace('_', ' ').title()}:** {v}")
        if not lines:
            return ""
        return "## User Context\n" + "\n".join(lines)


class EnvironmentContextProvider:
    """Inject runtime environment information."""

    async def provide(self, ctx: PromptContext) -> str:
        env = ctx.environment
        if env is None:
            return ""
        data = env.to_dict()
        if not data:
            return ""
        lines: list[str] = []
        if data.get("platform"):
            lines.append(f"**Platform:** {data['platform']}")
        if data.get("timezone"):
            lines.append(f"**Timezone:** {data['timezone']}")
        if data.get("available_apis"):
            lines.append(f"**Available APIs:** {', '.join(data['available_apis'])}")
        # Custom fields
        skip = {"timestamp", "platform", "timezone", "available_apis"}
        for k, v in data.items():
            if k not in skip and v:
                lines.append(f"**{k.replace('_', ' ').title()}:** {v}")
        if not lines:
            return ""
        return "## Environment\n" + "\n".join(lines)


class ConversationContextProvider:
    """Inject recent conversation messages or summary.

    Args:
        last_n: Number of recent messages to include.
    """

    def __init__(self, last_n: int = 10) -> None:
        self._last_n = last_n

    async def provide(self, ctx: PromptContext) -> str:
        conv = ctx.conversation
        if conv is None:
            return ""
        data = conv.to_dict()
        parts: list[str] = []
        summary = data.get("summary", "")
        if summary:
            parts.append(f"**Summary:** {summary}")
        messages = data.get("messages", [])
        if messages:
            recent = messages[-self._last_n :]
            for msg in recent:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                parts.append(f"**{role}:** {content}")
        if not parts:
            return ""
        return "## Conversation History\n" + "\n".join(parts)


class TeamContextProvider:
    """Inject team composition and completed task summaries."""

    async def provide(self, ctx: PromptContext) -> str:
        team = ctx.team
        if team is None:
            return ""
        data = team.to_dict()
        parts: list[str] = []
        agents = data.get("agents", [])
        if agents:
            lines = []
            for a in agents:
                name = a.get("name", "?")
                role = a.get("role", "")
                caps = a.get("capabilities", [])
                desc = f"- **{name}**"
                if role:
                    desc += f" ({role})"
                if caps:
                    desc += f": {', '.join(caps)}"
                lines.append(desc)
            parts.append("**Team Members:**\n" + "\n".join(lines))
        completed = data.get("completed_tasks", [])
        if completed:
            lines = []
            for t in completed[-5:]:
                agent = t.get("agent", "?")
                task = t.get("task", "")
                preview = t.get("result_preview", "")[:200]
                lines.append(f"- **{agent}** completed: {task}")
                if preview:
                    lines.append(f"  Result: {preview}")
            parts.append("**Completed Tasks:**\n" + "\n".join(lines))
        if not parts:
            return ""
        return "## Team Context\n" + "\n\n".join(parts)


class ErrorContextProvider:
    """Inject previous errors for retry awareness."""

    async def provide(self, ctx: PromptContext) -> str:
        err = ctx.errors
        if err is None:
            return ""
        data = err.to_dict()
        errors = data.get("errors", [])
        last_error = data.get("last_error", "")
        retry_count = data.get("retry_count", 0)
        if not errors and not last_error:
            return ""
        parts: list[str] = []
        if retry_count > 0:
            parts.append(f"**Retry attempt:** {retry_count}")
        if last_error:
            parts.append(f"**Last error:** {last_error}")
        if errors:
            for e in errors[-3:]:
                etype = e.get("type", "Error")
                msg = e.get("message", "")
                parts.append(f"- {etype}: {msg}")
        header = "## Previous Errors\nPrevious attempts encountered these issues. Avoid repeating them:\n"
        return header + "\n".join(parts)


class OutputContextProvider:
    """Inject expected output format, schema, and examples."""

    async def provide(self, ctx: PromptContext) -> str:
        out = ctx.output
        if out is None:
            return ""
        data = out.to_dict()
        parts: list[str] = []
        fmt = data.get("format", "")
        if fmt:
            parts.append(f"**Format:** {fmt}")
        schema_desc = data.get("schema_description", "")
        if schema_desc:
            parts.append(f"**Schema:** {schema_desc}")
        examples = data.get("examples", [])
        if examples:
            parts.append("**Examples:**")
            for ex in examples[:3]:
                parts.append(f"  - {ex}")
        constraints = data.get("constraints", [])
        if constraints:
            parts.append("**Constraints:**")
            for c in constraints:
                parts.append(f"  - {c}")
        if not parts:
            return ""
        return "## Expected Output\n" + "\n".join(parts)


class StaticContextProvider:
    """Inject a fixed text block.

    Args:
        text: The static text to inject.
        header: Optional markdown header (e.g. ``"## System"``).
    """

    def __init__(self, text: str, header: str | None = None) -> None:
        self._text = text
        self._header = header

    async def provide(self, ctx: PromptContext) -> str:
        if self._header:
            return f"{self._header}\n{self._text}"
        return self._text


class CallableContextProvider:
    """Wrap any async callable as a context provider.

    Args:
        fn: An ``async def(ctx: PromptContext) -> str`` callable.
        header: Optional markdown header.
    """

    def __init__(
        self,
        fn: Callable[..., Any],
        header: str | None = None,
    ) -> None:
        self._fn = fn
        self._header = header
        import inspect as _inspect

        self._is_async = _inspect.iscoroutinefunction(fn)

    async def provide(self, ctx: PromptContext) -> str:
        if self._is_async:
            result = await self._fn(ctx)
        else:
            result = self._fn(ctx)
        if not result:
            return ""
        if self._header:
            return f"{self._header}\n{result}"
        return result


class ConditionalContextProvider:
    """Only run the inner provider if *condition* returns True.

    Args:
        condition: A ``(ctx: PromptContext) -> bool`` callable.
        provider: The inner provider to conditionally run.
    """

    def __init__(
        self,
        condition: Callable[..., Any],
        provider: ContextProvider,
    ) -> None:
        self._condition = condition
        self._provider = provider

    async def provide(self, ctx: PromptContext) -> str:
        if not self._condition(ctx):
            return ""
        return await self._provider.provide(ctx)


class WorldContextProvider:
    """Read any custom :class:`BaseContext` from the world dict.

    Formats all fields of the context into a markdown section.

    Args:
        key: The key in ``ctx.world`` to read.
        header: Optional markdown header (defaults to the key, title-cased).
    """

    def __init__(self, key: str, header: str | None = None) -> None:
        self._key = key
        self._header = header

    async def provide(self, ctx: PromptContext) -> str:
        bc = ctx.world.get(self._key)
        if bc is None or not bc:
            return ""
        data = bc.to_dict()
        header = self._header or f"## {self._key.replace('_', ' ').title()}"
        lines = [
            f"**{k.replace('_', ' ').title()}:** {v}"
            for k, v in data.items()
            if v is not None and v != "" and v != []
        ]
        if not lines:
            return ""
        return header + "\n" + "\n".join(lines)


# ===================================================================
# Convenience constructors
# ===================================================================


def tool_context(include_schemas: bool = False) -> ToolContextProvider:
    """Create a :class:`ToolContextProvider`."""
    return ToolContextProvider(include_schemas=include_schemas)


def memory_context(limit: int = 5, min_score: float = 0.3) -> MemoryContextProvider:
    """Create a :class:`MemoryContextProvider`."""
    return MemoryContextProvider(limit=limit, min_score=min_score)


def task_context() -> TaskContextProvider:
    """Create a :class:`TaskContextProvider`."""
    return TaskContextProvider()


def blackboard_context(prefix: str = "result.") -> BlackboardContextProvider:
    """Create a :class:`BlackboardContextProvider`."""
    return BlackboardContextProvider(prefix=prefix)


def user_context() -> UserContextProvider:
    """Create a :class:`UserContextProvider`."""
    return UserContextProvider()


def env_context() -> EnvironmentContextProvider:
    """Create a :class:`EnvironmentContextProvider`."""
    return EnvironmentContextProvider()


def conversation_context(last_n: int = 10) -> ConversationContextProvider:
    """Create a :class:`ConversationContextProvider`."""
    return ConversationContextProvider(last_n=last_n)


def team_context() -> TeamContextProvider:
    """Create a :class:`TeamContextProvider`."""
    return TeamContextProvider()


def error_context() -> ErrorContextProvider:
    """Create a :class:`ErrorContextProvider`."""
    return ErrorContextProvider()


def output_context() -> OutputContextProvider:
    """Create a :class:`OutputContextProvider`."""
    return OutputContextProvider()


def static_context(text: str, header: str | None = None) -> StaticContextProvider:
    """Create a :class:`StaticContextProvider`."""
    return StaticContextProvider(text=text, header=header)


def callable_context(
    fn: Callable[..., Any],
    header: str | None = None,
) -> CallableContextProvider:
    """Create a :class:`CallableContextProvider`."""
    return CallableContextProvider(fn=fn, header=header)


def conditional_context(
    condition: Callable[..., Any],
    provider: ContextProvider,
) -> ConditionalContextProvider:
    """Create a :class:`ConditionalContextProvider`."""
    return ConditionalContextProvider(condition=condition, provider=provider)


def world_context(key: str, header: str | None = None) -> WorldContextProvider:
    """Create a :class:`WorldContextProvider`."""
    return WorldContextProvider(key=key, header=header)


# ===================================================================
# @context decorator
# ===================================================================


def context(*providers: ContextProvider) -> Callable[..., Any]:
    """Decorator that attaches context providers to a :class:`Prompt`.

    Usage::

        @prompt(model="openai:gpt-5-mini")
        @context(tool_context(), memory_context())
        async def analyze(text: str) -> str:
            \"\"\"Analyze: {text}\"\"\"
    """

    def decorator(prompt_or_func: Any) -> Any:
        if hasattr(prompt_or_func, "_context_providers"):
            prompt_or_func._context_providers.extend(providers)
            return prompt_or_func
        if not hasattr(prompt_or_func, "_pending_context"):
            prompt_or_func._pending_context = []
        prompt_or_func._pending_context.extend(providers)
        return prompt_or_func

    return decorator
