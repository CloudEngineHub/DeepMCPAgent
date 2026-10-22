---
seo_title: "Reasoning Skills Library: Node Factories"
description: "Pre-configured node factories for agent tasks such as web research, code review, data analysis, fact checking, planning and summarizing, all overridable."
---

# Skills Library

Pre-configured node factories for common agent tasks, in `promptise.engine.skills`. Each function returns a node with task-specific instructions. Import them from `promptise.engine.skills` by name: `critic` in particular is also the name of a perspective in `promptise.prompts`.

```python
from promptise.engine.skills import critic, planner, web_researcher

nodes = [planner("plan", is_entry=True), web_researcher("search", tools=[]), critic("review")]
print([type(n).__name__ for n in nodes])   # ['PromptNode', 'PromptNode', 'CritiqueNode']
```

Every setting a skill picks (`instructions`, `tool_choice`, `tools`,
`default_next`, ...) is a default: pass the same keyword to override it, as
for any parameter of the node class. Before 1.7.0 the standard skills fixed
some of them, and passing one again raised `TypeError: got multiple values
for keyword argument`.

## Standard Skills

### web_researcher

Search the web with multi-source citation and fact verification.

```python
web_researcher(
    name="web_research",
    tools=None,              # Search tools (BaseTool instances)
    max_iterations=5,        # Model calls per run (each tool-loop round counts)
    **kwargs,                # Other PromptNode parameters
)
# Returns: PromptNode
```

### code_reviewer

Analyzes code for security vulnerabilities, performance issues, best practices, and logic errors.

```python
code_reviewer(
    name="code_review",
    tools=None,              # Code analysis tools
    **kwargs,                # All PromptNode parameters
)
# Returns: PromptNode
```

### data_analyst

Evidence-based analysis with data-backed claims and confidence levels.

```python
data_analyst(
    name="analyze",
    tools=None,              # Data query tools
    **kwargs,                # All PromptNode parameters
)
# Returns: PromptNode
```

### fact_checker

Validates findings for accuracy. Returns a GuardNode with pass/fail routing.

```python
fact_checker(
    name="verify",
    guards=None,             # Guard instances; None means no guards, so every check passes
    on_pass="__end__",       # Node on pass
    on_fail=None,            # Node on fail
    **kwargs,                # Other GuardNode parameters
)
# Returns: GuardNode (no model call; pass the guards that do the checking)
```

### summarizer

Compresses information into structured response: key finding, supporting details, sources.

```python
summarizer(
    name="summarize",
    max_length=2000,         # Length asked for in the instructions (chars); not enforced
    **kwargs,                # Other PromptNode parameters
)
# Returns: PromptNode with no tools and default_next="__end__"
```

### planner

Creates step-by-step plans with self-evaluation (rates quality 1-5).

```python
planner(
    name="plan",
    max_subgoals=4,          # Number of subgoals asked for in the instructions
    **kwargs,                # Other PromptNode parameters
)
# Returns: PromptNode (not a PlanNode: it does not set state.plan; use PlanNode for that)
```

### decision_router

Routes to next step based on progress. Lightweight LLM call to choose between named routes.

```python
decision_router(
    name="decide",
    routes=None,             # Dict mapping route labels to node names
    **kwargs,                # Other RouterNode parameters (default_next, model_override, ...)
)
# Returns: RouterNode
```

### formatter

Pure data transformation (no LLM call) for formatting final output.

```python
formatter(
    name="format",
    output_key="formatted_result",  # Where to store in state.context
    transform=None,                  # fn(state) -> Any; without one the node fails
    **kwargs,                        # Other TransformNode parameters
)
# Returns: TransformNode with default_next="__end__"
```

## Reasoning Skills

Pre-built reasoning node factories. Each returns the [reasoning node](engine-nodes.md#reasoning-nodes-pre-built-building-bricks) of the same kind, with its built-in instructions and default [Node Flags](engine-flags.md). Like the nodes themselves, they read routing and state fields only from a structured output, so pass an `output_schema` when the node should route or update state.

### thinker

Gap analysis and next-step reasoning. Default flags: `READONLY`, `LIGHTWEIGHT`.

```python
thinker(
    name="think",
    **kwargs,                # All ThinkNode parameters (focus_areas, etc.)
)
# Returns: ThinkNode
```

### reflector

Self-evaluation and mistake correction. Default flags: `STATEFUL`, `OBSERVABLE`.

```python
reflector(
    name="reflect",
    **kwargs,                # All ReflectNode parameters (review_depth, etc.)
)
# Returns: ReflectNode
```

### critic

Adversarial review with severity-based routing. Default flags: `READONLY`, `OBSERVABLE`.

```python
from promptise.engine.skills import critic

critic(
    name="critique",
    **kwargs,                # All CritiqueNode parameters (severity_threshold, etc.)
)
# Returns: CritiqueNode
```

### justifier

Chain-of-thought justification for audit trail. Default flags: `READONLY`, `OBSERVABLE`, `VERBOSE`.

```python
justifier(
    name="justify",
    **kwargs,                # All JustifyNode parameters
)
# Returns: JustifyNode
```

### synthesizer

Combines all gathered data into a final answer. Default flags: `OBSERVABLE`.

```python
synthesizer(
    name="synthesize",
    **kwargs,                # All SynthesizeNode parameters
)
# Returns: SynthesizeNode
```

### validator_node

LLM-powered quality gate with pass/fail routing. Default flags: `READONLY`, `VALIDATE_OUTPUT`.

```python
validator_node(
    name="validate",
    criteria=None,           # List of validation criteria strings
    **kwargs,                # All ValidateNode parameters (on_pass, on_fail, etc.)
)
# Returns: ValidateNode
```

### observer_node

Tool result interpretation and data extraction. Default flags: `STATEFUL`.

```python
observer_node(
    name="observe",
    **kwargs,                # All ObserveNode parameters
)
# Returns: ObserveNode
```

## Usage

```python
from promptise.engine import PromptGraph
from promptise.engine.skills import (
    planner, web_researcher, reflector, synthesizer,
)

graph = PromptGraph("research-team")
graph.add_node(planner("plan", is_entry=True))
graph.add_node(web_researcher("search", tools=search_tools))
graph.add_node(reflector("reflect"))
graph.add_node(synthesizer("conclude", is_terminal=True))
```

## Customization

Skills are functions that return nodes. Pass any parameter of the node class, including the ones the skill sets:

```python
from promptise.engine import NodeFlag, PromptGraph
from promptise.engine.skills import summarizer, thinker, web_researcher

graph = PromptGraph("custom")

# Custom max iterations
graph.add_node(web_researcher("search", tools=my_tools, max_iterations=10))

# Custom instructions
graph.add_node(summarizer("conclude", instructions="Summarize in bullet points only."))

# Reasoning skills take any parameter, including their instructions
graph.add_node(thinker("think", instructions="List what is still unknown."))

# Per-node model override
graph.add_node(thinker("think_fast", model_override="openai:gpt-5-mini"))

# Additional flags
graph.add_node(web_researcher("search",
    tools=my_tools,
    flags={NodeFlag.CRITICAL, NodeFlag.RETRYABLE},
))
```
