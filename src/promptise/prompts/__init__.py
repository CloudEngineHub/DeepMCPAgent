"""Prompt & Context Engineering Framework for Promptise.

This package provides composable, extensible primitives for building
dynamic prompts, injecting runtime context, applying reasoning
strategies, and enforcing guardrails.

Quick start::

    from promptise.prompts import prompt, guard, context
    from promptise.prompts.guards import content_filter, length
    from promptise.prompts.context import user_context, tool_context
    from promptise.prompts.strategies import chain_of_thought, analyst

    @prompt(model="openai:gpt-5-mini")
    @context(user_context(), tool_context())
    @guard(content_filter(blocked=["secret"]), length(max_length=2000))
    async def analyze(text: str) -> str:
        \"""Analyze the following: {text}\"""
"""

from .blocks import (
    AssembledPrompt,
    Block,
    BlockContext,
    Composite,
    Conditional,
    ContextSlot,
    Examples,
    Identity,
    ObservationBlock,
    OutputFormat,
    PhaseBlock,
    PlanBlock,
    PromptAssembler,
    ReflectionBlock,
    Rules,
    Section,
    SimpleBlock,
    ToolsBlock,
    block,
    blocks,
)
from .builder import PromptBuilder
from .chain import branch, chain, fallback, parallel, retry
from .context import (
    BaseContext,
    ContextProvider,
    ConversationContext,
    EnvironmentContext,
    ErrorContext,
    OutputContext,
    PromptContext,
    TeamContext,
    UserContext,
    context,
)
from .core import Prompt, PromptStats, constraint, prompt
from .flows import ConversationFlow, Phase, TurnContext, phase
from .guards import Guard, GuardError, guard
from .inspector import PromptInspector, PromptTrace
from .loader import (
    PromptFileError,
    PromptValidationError,
    load_directory,
    load_prompt,
    load_url,
    save_prompt,
)
from .observe import PromptObserver
from .registry import PromptRegistry, registry, version
from .strategies import (
    Perspective,
    Strategy,
    advisor,
    analyst,
    chain_of_thought,
    creative,
    critic,
    decompose,
    perspective,
    plan_and_execute,
    self_critique,
    structured_reasoning,
)
from .suite import PromptSuite
from .template import (
    ShellExecutionError,
    ShellExecutor,
    SubprocessShellExecutor,
    TemplateEngine,
    render_template,
)
from .testing import PromptTestCase

__all__ = [
    # Core
    "Prompt",
    "PromptStats",
    "prompt",
    "constraint",
    # Blocks (Layer 1)
    "Block",
    "BlockContext",
    "Identity",
    "Rules",
    "OutputFormat",
    "ContextSlot",
    "Section",
    "Examples",
    "Conditional",
    "Composite",
    "PromptAssembler",
    "AssembledPrompt",
    "blocks",
    # Flows (Layer 2)
    "ConversationFlow",
    "Phase",
    "TurnContext",
    "phase",
    # Inspector
    "PromptInspector",
    "PromptTrace",
    # Context
    "BaseContext",
    "ContextProvider",
    "PromptContext",
    "UserContext",
    "EnvironmentContext",
    "ConversationContext",
    "TeamContext",
    "ErrorContext",
    "OutputContext",
    "context",
    # Guards
    "Guard",
    "GuardError",
    "guard",
    # Strategies & Perspectives
    "Strategy",
    "Perspective",
    "chain_of_thought",
    "structured_reasoning",
    "self_critique",
    "plan_and_execute",
    "decompose",
    "analyst",
    "critic",
    "advisor",
    "creative",
    "perspective",
    # Builder
    "PromptBuilder",
    # Suite
    "PromptSuite",
    # Chain operators
    "chain",
    "parallel",
    "branch",
    "retry",
    "fallback",
    # Registry
    "PromptRegistry",
    "registry",
    "version",
    # Template
    "TemplateEngine",
    "render_template",
    "ShellExecutor",
    "ShellExecutionError",
    "SubprocessShellExecutor",
    # Observability
    "PromptObserver",
    # Testing
    "PromptTestCase",
    # Loader
    "PromptFileError",
    "PromptValidationError",
    "load_prompt",
    "load_url",
    "save_prompt",
    "load_directory",
    # Custom block helpers
    "SimpleBlock",
    "block",
    # Agentic blocks
    "ToolsBlock",
    "ObservationBlock",
    "PlanBlock",
    "ReflectionBlock",
    "PhaseBlock",
]
