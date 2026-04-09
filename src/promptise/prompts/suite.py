"""Prompt suites — groups of prompts sharing a world configuration.

A :class:`PromptSuite` bundles related prompts with shared defaults
for context providers, strategy, perspective, constraints, world
contexts, and guards.  Per-prompt decorators can override suite
defaults.

Example::

    from promptise.prompts import prompt
    from promptise.prompts.suite import PromptSuite
    from promptise.prompts.context import tool_context, memory_context
    from promptise.prompts.strategies import structured_reasoning, critic

    class SecurityAudit(PromptSuite):
        context_providers = [tool_context(), memory_context()]
        default_strategy = structured_reasoning
        default_perspective = critic
        default_constraints = ["Must reference OWASP categories"]

        @prompt(model="openai:gpt-4o")
        async def scan_code(self, code: str, language: str) -> list[str]:
            \"""Scan for security vulnerabilities in this {language} code:
            {code}\"""

        @prompt(model="openai:gpt-5-mini")
        async def suggest_fixes(self, vulnerabilities: list[str]) -> str:
            \"""Suggest fixes for: {vulnerabilities}\"""

    suite = SecurityAudit()
    results = await suite.scan_code(code="...", language="python")
"""

from __future__ import annotations

from typing import Any, ClassVar

from .context import BaseContext, ContextProvider, PromptContext
from .core import Prompt
from .guards import Guard
from .strategies import Perspective, Strategy

__all__ = ["PromptSuite"]


class PromptSuite:
    """Group of prompts sharing world configuration.

    Subclass and set class attributes to configure shared defaults.
    Decorate methods with ``@prompt(...)`` to define prompts that
    inherit suite-level configuration.

    Class Attributes:
        context_providers: Shared context providers.
        default_strategy: Default reasoning strategy.
        default_perspective: Default cognitive perspective.
        default_constraints: Default constraints.
        default_guards: Default guards.
        default_world: Default world contexts (dict of BaseContext).
    """

    context_providers: ClassVar[list[ContextProvider]] = []
    default_strategy: ClassVar[Strategy | None] = None
    default_perspective: ClassVar[Perspective | None] = None
    default_constraints: ClassVar[list[str]] = []
    default_guards: ClassVar[list[Guard]] = []
    default_world: ClassVar[dict[str, BaseContext]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Apply suite defaults to all @prompt-decorated methods."""
        super().__init_subclass__(**kwargs)
        for name in dir(cls):
            attr = getattr(cls, name, None)
            if isinstance(attr, Prompt):
                cls._apply_suite_defaults(attr)

    @classmethod
    def _apply_suite_defaults(cls, p: Prompt) -> None:
        """Merge suite defaults into a Prompt (prompt overrides suite).

        Idempotent — safe to call multiple times (e.g., in sub-subclasses).
        Uses a marker attribute ``_suite_defaults_applied`` to prevent
        duplicate merging.
        """
        if getattr(p, "_suite_defaults_applied", False):
            return
        p._suite_defaults_applied = True  # type: ignore[attr-defined]

        # Strip 'self' parameter — suite prompts are defined as methods but
        # accessed as class attributes (Prompt doesn't implement __get__).
        params = list(p._sig.parameters.values())
        if params and params[0].name == "self":
            p._sig = p._sig.replace(parameters=params[1:])

        # Context providers — suite providers come first (no duplicates)
        suite_providers = list(cls.context_providers)
        existing_names = {type(cp).__name__ for cp in p._context_providers}
        unique_suite = [cp for cp in suite_providers if type(cp).__name__ not in existing_names]
        p._context_providers = unique_suite + list(p._context_providers)

        # Strategy — prompt wins if set
        if p._strategy is None and cls.default_strategy is not None:
            p._strategy = cls.default_strategy

        # Perspective — prompt wins if set
        if p._perspective is None and cls.default_perspective is not None:
            p._perspective = cls.default_perspective

        # Constraints — suite constraints come first
        suite_constraints = list(cls.default_constraints)
        suite_constraints.extend(p._constraints)
        p._constraints = suite_constraints

        # Guards — suite guards come first
        suite_input_guards = list(cls.default_guards)
        suite_input_guards.extend(p._input_guards)
        p._input_guards = suite_input_guards

        suite_output_guards = list(cls.default_guards)
        suite_output_guards.extend(p._output_guards)
        p._output_guards = suite_output_guards

        # World — suite world is base, prompt world overrides
        merged_world = dict(cls.default_world)
        merged_world.update(p._world)
        p._world = merged_world

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @property
    def prompts(self) -> dict[str, Prompt]:
        """Discover all :class:`Prompt` instances on this suite.

        Returns:
            Dict mapping prompt name to Prompt instance.
        """
        result: dict[str, Prompt] = {}
        for name in dir(self.__class__):
            attr = getattr(self.__class__, name, None)
            if isinstance(attr, Prompt):
                result[attr.name] = attr
        return result

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def system_prompt(self) -> str:
        """Render a combined system prompt from all suite prompts.

        Returns a static text block suitable for ``build_agent()``
        when the suite is used as agent instructions.  For dynamic
        context, use individual prompt's ``render_async()``.
        """
        parts: list[str] = []
        for name, p in self.prompts.items():
            rendered = p.render()
            if rendered:
                parts.append(f"## {name}\n{rendered}")
        return "\n\n".join(parts) if parts else ""

    async def render_async(self, ctx: PromptContext | None = None) -> str:
        """Async render all prompts with context providers.

        Returns combined text from all prompts in the suite.
        """
        parts: list[str] = []
        for name, p in self.prompts.items():
            rendered = await p.render_async(ctx)
            if rendered:
                parts.append(f"## {name}\n{rendered}")
        return "\n\n".join(parts) if parts else ""

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        count = len(self.prompts)
        return f"<{self.__class__.__name__} prompts={count}>"
