# Strategies & Perspectives

Strategies control **how** the agent reasons. Perspectives control **from where** the agent reasons. They are orthogonal and composable -- any strategy pairs with any perspective, and strategies compose via the `+` operator.

## Quick Example

```python
from promptise.prompts import prompt
from promptise.prompts.strategies import chain_of_thought, self_critique, analyst

@prompt(model="openai:gpt-5-mini")
async def analyze(text: str) -> str:
    """Analyze the following text: {text}"""

# Apply strategy + perspective
configured = (
    analyze
    .with_strategy(chain_of_thought + self_critique)
    .with_perspective(analyst)
)

result = await configured("Quarterly revenue grew 15% year-over-year...")
```

## Concepts

- **Strategy** = HOW to reason (step-by-step, critique, decompose). Strategies implement `wrap()` to inject reasoning instructions before the LLM call, and `parse()` to extract the final answer from the raw output.
- **Perspective** = FROM WHERE to reason (analyst, critic, advisor). Perspectives implement `apply()` to prepend or inject cognitive framing into the prompt text.

Both are composable and orthogonal -- any strategy can pair with any perspective.

## Built-In Strategies

### ChainOfThoughtStrategy

Step-by-step reasoning with an answer marker. Instructs the LLM to think step-by-step, then produce a final answer after `---ANSWER---`.

```python
from promptise.prompts.strategies import chain_of_thought

result = await analyze.with_strategy(chain_of_thought)("Complex problem...")
```

### StructuredReasoningStrategy

Multi-step structured reasoning with customizable phases. Default phases: Understand, Analyze, Evaluate, Conclude.

```python
from promptise.prompts.strategies import structured_reasoning, StructuredReasoningStrategy

# Use defaults
result = await analyze.with_strategy(structured_reasoning)("Evaluate this proposal")

# Custom phases
security_review = StructuredReasoningStrategy(
    steps=["Threat Model", "Attack Surface", "Mitigations", "Risk Rating"]
)
result = await analyze.with_strategy(security_review)("Review this API design")
```

### SelfCritiqueStrategy

Generate, critique, and improve. Supports multiple critique rounds.

```python
from promptise.prompts.strategies import self_critique, SelfCritiqueStrategy

# Single round (default)
result = await analyze.with_strategy(self_critique)("Write a recommendation")

# Multiple rounds
deep_critique = SelfCritiqueStrategy(rounds=3)
result = await analyze.with_strategy(deep_critique)("Design a system architecture")
```

### PlanAndExecuteStrategy

Plan first, then execute each step, then synthesize.

```python
from promptise.prompts.strategies import plan_and_execute

result = await analyze.with_strategy(plan_and_execute)("Create a marketing strategy")
```

### DecomposeStrategy

Break into subproblems, solve each independently, then combine.

```python
from promptise.prompts.strategies import decompose

result = await analyze.with_strategy(decompose)("Optimize this distributed system")
```

### Strategies Summary Table

| Strategy | Import | Behavior |
|----------|--------|----------|
| `chain_of_thought` | `strategies.chain_of_thought` | Step-by-step reasoning with `---ANSWER---` marker |
| `structured_reasoning` | `strategies.structured_reasoning` | Phases: Understand, Analyze, Evaluate, Conclude (customizable) |
| `self_critique` | `strategies.self_critique` | Generate, critique, improve (configurable rounds) |
| `plan_and_execute` | `strategies.plan_and_execute` | Plan steps, execute each, synthesize |
| `decompose` | `strategies.decompose` | Break into subproblems, solve each, combine |

## Composing Strategies

Strategies compose with the `+` operator. `wrap()` applies left-to-right, `parse()` applies right-to-left:

```python
from promptise.prompts.strategies import chain_of_thought, self_critique

combined = chain_of_thought + self_critique
result = await analyze.with_strategy(combined)("Complex optimization problem")
```

You can compose any number of strategies:

```python
triple = chain_of_thought + self_critique + decompose
```

The `CompositeStrategy` class handles the composition. It flattens nested composites automatically.

## Custom Strategies

Implement the `Strategy` protocol with `wrap()` and `parse()` methods:

```python
from promptise.prompts.strategies import Strategy
from promptise.prompts.context import PromptContext

class StepBackStrategy:
    """Take a step back before answering."""

    def wrap(self, prompt_text: str, ctx: PromptContext) -> str:
        return (
            f"{prompt_text}\n\n"
            "Before answering, take a step back and consider the broader context.\n"
            "What assumptions might be wrong? What is the bigger picture?\n"
            "After your reasoning, write '---ANSWER---' then your final answer."
        )

    def parse(self, raw_output: str, ctx: PromptContext) -> str:
        if "---ANSWER---" in raw_output:
            return raw_output.split("---ANSWER---", 1)[1].strip()
        return raw_output

    def __add__(self, other):
        from promptise.prompts.strategies import CompositeStrategy
        return CompositeStrategy([self, other])
```

Custom strategies compose with built-in strategies via `+`:

```python
step_back = StepBackStrategy()
combined = step_back + chain_of_thought
```

## Built-In Perspectives

### AnalystPerspective

Evidence-based analysis. Focuses on data, patterns, measurable outcomes, and evidence-based conclusions.

```python
from promptise.prompts.strategies import analyst

result = await analyze.with_perspective(analyst)("Evaluate this investment proposal")
```

### CriticPerspective

Critical evaluation. Challenges assumptions, identifies weaknesses, stress-tests ideas.

```python
from promptise.prompts.strategies import critic

result = await analyze.with_perspective(critic)("Review this architecture design")
```

### AdvisorPerspective

Balanced advisory. Provides balanced recommendations with trade-off analysis and actionable next steps.

```python
from promptise.prompts.strategies import advisor

result = await analyze.with_perspective(advisor)("Should we migrate to microservices?")
```

### CreativePerspective

Creative exploration. Explores unconventional solutions, novel combinations, and challenges conventional thinking.

```python
from promptise.prompts.strategies import creative

result = await analyze.with_perspective(creative)("How can we improve user engagement?")
```

### Perspectives Summary Table

| Perspective | Framing |
|-------------|---------|
| `analyst` | Evidence-based, data patterns, quantifiable metrics |
| `critic` | Challenge assumptions, identify weaknesses, stress-test |
| `advisor` | Balanced recommendations, trade-off analysis, actionable steps |
| `creative` | Unconventional solutions, novel combinations, innovation |

## Custom Perspectives

Use the `perspective()` factory function for quick custom perspectives:

```python
from promptise.prompts.strategies import perspective

security_auditor = perspective(
    role="security auditor",
    instructions="Focus on OWASP Top 10 vulnerabilities and compliance gaps.",
)

result = await analyze.with_perspective(security_auditor)("Review this API code")
```

Or implement the `Perspective` protocol directly:

```python
class DomainExpertPerspective:
    def __init__(self, domain: str):
        self.domain = domain

    def apply(self, prompt_text: str, ctx: PromptContext) -> str:
        return (
            f"You are a domain expert in {self.domain}. Apply deep domain "
            f"knowledge and industry-specific best practices.\n\n{prompt_text}"
        )

fintech_expert = DomainExpertPerspective("financial technology")
```

## Combining Strategy + Perspective

Strategies and perspectives are orthogonal. Use both on the same prompt:

```python
from promptise.prompts.strategies import chain_of_thought, analyst

configured = (
    analyze
    .with_strategy(chain_of_thought)
    .with_perspective(analyst)
)

result = await configured("Evaluate quarterly performance data")
```

## API Summary

### Strategy Protocol

| Method | Description |
|--------|-------------|
| `wrap(prompt_text, ctx) -> str` | Inject reasoning instructions into the prompt |
| `parse(raw_output, ctx) -> str` | Extract the final answer from the raw LLM output |

### Perspective Protocol

| Method | Description |
|--------|-------------|
| `apply(prompt_text, ctx) -> str` | Prepend cognitive framing into the prompt |

### Built-In Singletons

| Name | Type | Description |
|------|------|-------------|
| `chain_of_thought` | `ChainOfThoughtStrategy` | Step-by-step reasoning |
| `structured_reasoning` | `StructuredReasoningStrategy` | Multi-phase reasoning |
| `self_critique` | `SelfCritiqueStrategy` | Generate-critique-improve |
| `plan_and_execute` | `PlanAndExecuteStrategy` | Plan then execute |
| `decompose` | `DecomposeStrategy` | Subproblem decomposition |
| `analyst` | `AnalystPerspective` | Evidence-based analysis |
| `critic` | `CriticPerspective` | Critical evaluation |
| `advisor` | `AdvisorPerspective` | Balanced advisory |
| `creative` | `CreativePerspective` | Creative exploration |

### Factories

| Function | Returns | Description |
|----------|---------|-------------|
| `perspective(role, instructions="")` | `CustomPerspective` | Create a custom perspective |
| `StructuredReasoningStrategy(steps=[...])` | `StructuredReasoningStrategy` | Custom reasoning phases |
| `SelfCritiqueStrategy(rounds=1)` | `SelfCritiqueStrategy` | Multi-round critique |

!!! tip "Use the ---ANSWER--- marker"
    All built-in strategies use the `---ANSWER---` marker to separate reasoning from the final answer. The `parse()` method strips the reasoning and returns only the answer. Custom strategies should follow this convention for consistency.

!!! tip "Strategy + Perspective = powerful combination"
    Pair `chain_of_thought` with `analyst` for data-driven step-by-step reasoning. Pair `self_critique` with `critic` for rigorous self-evaluation. The combinations multiply your prompt's effectiveness.

!!! warning "Composite strategy ordering"
    In a composite `A + B`, `wrap()` applies A first then B (outer wrapping), while `parse()` applies B first then A (inner unwrapping). This means B's reasoning instructions appear closest to the prompt text.

## What's Next

- [Guards](guards.md) -- Input/output validation
- [Context System](context.md) -- Dynamic context injection
- [PromptBuilder](builder.md) -- Fluent API for runtime prompt construction
