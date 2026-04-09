# Skills Library

Pre-configured node factories for common agent tasks. Each function returns a fully configured node with domain-specific instructions, default flags, and sensible settings. All parameters can be overridden.

## Standard Skills

### web_researcher

Search the web with multi-source citation and fact verification.

```python
web_researcher(
    name="web_research",
    tools=None,              # Search tools (BaseTool instances)
    max_iterations=5,        # Max tool-calling loops
    **kwargs,                # All PromptNode parameters
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
    guards=None,             # Guard instances (defaults to content filters)
    on_pass="__end__",       # Node on pass
    on_fail=None,            # Node on fail
    **kwargs,                # All GuardNode parameters
)
# Returns: GuardNode
```

### summarizer

Compresses information into structured response: key finding, supporting details, sources.

```python
summarizer(
    name="summarize",
    max_length=2000,         # Target summary length (chars)
    **kwargs,                # All PromptNode parameters
)
# Returns: PromptNode
```

### planner

Creates step-by-step plans with self-evaluation (rates quality 1-5).

```python
planner(
    name="plan",
    max_subgoals=4,          # Maximum number of subgoals
    **kwargs,                # All PromptNode parameters
)
# Returns: PromptNode
```

### decision_router

Routes to next step based on progress. Lightweight LLM call to choose between named routes.

```python
decision_router(
    name="decide",
    routes=None,             # Dict mapping route labels to node names
    **kwargs,                # All RouterNode parameters
)
# Returns: RouterNode
```

### formatter

Pure data transformation (no LLM call) for formatting final output.

```python
formatter(
    name="format",
    output_key="formatted_result",  # Where to store in state.context
    transform=None,                  # Custom transform function
    **kwargs,                        # All TransformNode parameters
)
# Returns: TransformNode
```

## Reasoning Skills

Pre-built reasoning node factories. Each returns a fully configured reasoning node with built-in instructions and default [Node Flags](engine-flags.md).

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

Skills are functions that return nodes. Override any parameter:

```python
# Custom max iterations
graph.add_node(web_researcher("search", tools=my_tools, max_iterations=10))

# Custom instructions override the built-in ones
graph.add_node(summarizer("conclude", instructions="Summarize in bullet points only."))

# Per-node model override
graph.add_node(thinker("think", model_override="openai:gpt-4o-mini"))

# Additional flags
from promptise.engine import NodeFlag
graph.add_node(web_researcher("search",
    tools=my_tools,
    flags={NodeFlag.CRITICAL, NodeFlag.RETRYABLE},
))
```
