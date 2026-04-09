# Prompts API Reference

Prompt engineering framework — decorators, blocks, flows, strategies, guards, context providers, chaining, loading, versioning, and testing.

## Core

### prompt decorator

::: promptise.prompts.core.prompt
    options:
      show_source: false
      heading_level: 4

### Prompt

::: promptise.prompts.core.Prompt
    options:
      show_source: false
      heading_level: 4

### PromptStats

::: promptise.prompts.core.PromptStats
    options:
      show_source: false
      heading_level: 4

### constraint

::: promptise.prompts.core.constraint
    options:
      show_source: false
      heading_level: 4

---

## Blocks (Layer 1)

Composable prompt blocks with priority-based token budgeting.

### Block

::: promptise.prompts.blocks.Block
    options:
      show_source: false
      heading_level: 4

### BlockContext

::: promptise.prompts.blocks.BlockContext
    options:
      show_source: false
      heading_level: 4

### Identity

::: promptise.prompts.blocks.Identity
    options:
      show_source: false
      heading_level: 4

### Rules

::: promptise.prompts.blocks.Rules
    options:
      show_source: false
      heading_level: 4

### OutputFormat

::: promptise.prompts.blocks.OutputFormat
    options:
      show_source: false
      heading_level: 4

### ContextSlot

::: promptise.prompts.blocks.ContextSlot
    options:
      show_source: false
      heading_level: 4

### Section

::: promptise.prompts.blocks.Section
    options:
      show_source: false
      heading_level: 4

### Examples

::: promptise.prompts.blocks.Examples
    options:
      show_source: false
      heading_level: 4

### Conditional

::: promptise.prompts.blocks.Conditional
    options:
      show_source: false
      heading_level: 4

### Composite

::: promptise.prompts.blocks.Composite
    options:
      show_source: false
      heading_level: 4

### SimpleBlock

::: promptise.prompts.blocks.SimpleBlock
    options:
      show_source: false
      heading_level: 4

### ToolsBlock

::: promptise.prompts.blocks.ToolsBlock
    options:
      show_source: false
      heading_level: 4

### PhaseBlock

::: promptise.prompts.blocks.PhaseBlock
    options:
      show_source: false
      heading_level: 4

### PlanBlock

::: promptise.prompts.blocks.PlanBlock
    options:
      show_source: false
      heading_level: 4

### ObservationBlock

::: promptise.prompts.blocks.ObservationBlock
    options:
      show_source: false
      heading_level: 4

### ReflectionBlock

::: promptise.prompts.blocks.ReflectionBlock
    options:
      show_source: false
      heading_level: 4

### PromptAssembler

::: promptise.prompts.blocks.PromptAssembler
    options:
      show_source: false
      heading_level: 4

### AssembledPrompt

::: promptise.prompts.blocks.AssembledPrompt
    options:
      show_source: false
      heading_level: 4

### block (decorator)

::: promptise.prompts.blocks.block
    options:
      show_source: false
      heading_level: 4

### blocks (utility)

::: promptise.prompts.blocks.blocks
    options:
      show_source: false
      heading_level: 4

---

## PromptBuilder

Fluent runtime prompt construction.

### PromptBuilder

::: promptise.prompts.builder.PromptBuilder
    options:
      show_source: false
      heading_level: 4

---

## Conversation Flows

### ConversationFlow

::: promptise.prompts.flows.ConversationFlow
    options:
      show_source: false
      heading_level: 4

### Phase

::: promptise.prompts.flows.Phase
    options:
      show_source: false
      heading_level: 4

### TurnContext

::: promptise.prompts.flows.TurnContext
    options:
      show_source: false
      heading_level: 4

### phase (decorator)

::: promptise.prompts.flows.phase
    options:
      show_source: false
      heading_level: 4

---

## Strategies and Perspectives

Reasoning strategies and cognitive perspectives. Both are composable with `+`.

### Strategy

::: promptise.prompts.strategies.Strategy
    options:
      show_source: false
      heading_level: 4

### Perspective

::: promptise.prompts.strategies.Perspective
    options:
      show_source: false
      heading_level: 4

### chain_of_thought

::: promptise.prompts.strategies.chain_of_thought
    options:
      show_source: false
      heading_level: 4

### self_critique

::: promptise.prompts.strategies.self_critique
    options:
      show_source: false
      heading_level: 4

### structured_reasoning

::: promptise.prompts.strategies.structured_reasoning
    options:
      show_source: false
      heading_level: 4

### plan_and_execute

::: promptise.prompts.strategies.plan_and_execute
    options:
      show_source: false
      heading_level: 4

### decompose

::: promptise.prompts.strategies.decompose
    options:
      show_source: false
      heading_level: 4

### analyst

::: promptise.prompts.strategies.analyst
    options:
      show_source: false
      heading_level: 4

### critic

::: promptise.prompts.strategies.critic
    options:
      show_source: false
      heading_level: 4

### advisor

::: promptise.prompts.strategies.advisor
    options:
      show_source: false
      heading_level: 4

### creative

::: promptise.prompts.strategies.creative
    options:
      show_source: false
      heading_level: 4

### perspective

::: promptise.prompts.strategies.perspective
    options:
      show_source: false
      heading_level: 4

---

## Context Providers

Pluggable async context that gets injected into the prompt at runtime.

### context (decorator)

::: promptise.prompts.context.context
    options:
      show_source: false
      heading_level: 4

### BaseContext

::: promptise.prompts.context.BaseContext
    options:
      show_source: false
      heading_level: 4

### ContextProvider

::: promptise.prompts.context.ContextProvider
    options:
      show_source: false
      heading_level: 4

### PromptContext

::: promptise.prompts.context.PromptContext
    options:
      show_source: false
      heading_level: 4

### UserContext

::: promptise.prompts.context.UserContext
    options:
      show_source: false
      heading_level: 4

### ConversationContext

::: promptise.prompts.context.ConversationContext
    options:
      show_source: false
      heading_level: 4

### EnvironmentContext

::: promptise.prompts.context.EnvironmentContext
    options:
      show_source: false
      heading_level: 4

### ErrorContext

::: promptise.prompts.context.ErrorContext
    options:
      show_source: false
      heading_level: 4

### OutputContext

::: promptise.prompts.context.OutputContext
    options:
      show_source: false
      heading_level: 4

### TeamContext

::: promptise.prompts.context.TeamContext
    options:
      show_source: false
      heading_level: 4

---

## Guards

### Guard

::: promptise.prompts.guards.Guard
    options:
      show_source: false
      heading_level: 4

### GuardError

::: promptise.prompts.guards.GuardError
    options:
      show_source: false
      heading_level: 4

### guard (decorator)

::: promptise.prompts.guards.guard
    options:
      show_source: false
      heading_level: 4

---

## Chaining

Composable execution primitives: sequential, parallel, conditional branching, retry, and fallback.

### chain

::: promptise.prompts.chain.chain
    options:
      show_source: false
      heading_level: 4

### parallel

::: promptise.prompts.chain.parallel
    options:
      show_source: false
      heading_level: 4

### branch

::: promptise.prompts.chain.branch
    options:
      show_source: false
      heading_level: 4

### retry

::: promptise.prompts.chain.retry
    options:
      show_source: false
      heading_level: 4

### fallback

::: promptise.prompts.chain.fallback
    options:
      show_source: false
      heading_level: 4

---

## Inspector

Introspect prompt assembly step-by-step: which blocks were included, token counts, context providers, guard results.

### PromptInspector

::: promptise.prompts.inspector.PromptInspector
    options:
      show_source: false
      heading_level: 4

### PromptTrace

::: promptise.prompts.inspector.PromptTrace
    options:
      show_source: false
      heading_level: 4

---

## Registry and Versioning

### PromptRegistry

::: promptise.prompts.registry.PromptRegistry
    options:
      show_source: false
      heading_level: 4

### registry (singleton)

::: promptise.prompts.registry.registry
    options:
      show_source: false
      heading_level: 4

### version (decorator)

::: promptise.prompts.registry.version
    options:
      show_source: false
      heading_level: 4

---

## Loader and Templates

Load `.prompt` YAML files and render templates.

### load_prompt

::: promptise.prompts.loader.load_prompt
    options:
      show_source: false
      heading_level: 4

### load_directory

::: promptise.prompts.loader.load_directory
    options:
      show_source: false
      heading_level: 4

### load_url

::: promptise.prompts.loader.load_url
    options:
      show_source: false
      heading_level: 4

### save_prompt

::: promptise.prompts.loader.save_prompt
    options:
      show_source: false
      heading_level: 4

### PromptFileError

::: promptise.prompts.loader.PromptFileError
    options:
      show_source: false
      heading_level: 4

### PromptValidationError

::: promptise.prompts.loader.PromptValidationError
    options:
      show_source: false
      heading_level: 4

### TemplateEngine

::: promptise.prompts.template.TemplateEngine
    options:
      show_source: false
      heading_level: 4

### render_template

::: promptise.prompts.template.render_template
    options:
      show_source: false
      heading_level: 4

---

## Suite

Group related prompts with shared defaults (strategy, perspective, constraints, guards, context).

### PromptSuite

::: promptise.prompts.suite.PromptSuite
    options:
      show_source: false
      heading_level: 4

---

## Testing

### PromptTestCase

::: promptise.prompts.testing.PromptTestCase
    options:
      show_source: false
      heading_level: 4

---

## Observability

### PromptObserver

::: promptise.prompts.observe.PromptObserver
    options:
      show_source: false
      heading_level: 4
