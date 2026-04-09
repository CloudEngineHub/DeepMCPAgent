# Prompt Engineering

Build reliable, testable, versioned system prompts in 9 incremental steps. Each step adds one concept -- from simple typed blocks to composable reasoning strategies, runtime guards, and prompt pipelines.

!!! tip "This is the recommended starting point for prompt engineering"
    This guide walks you through the complete prompt system step by step. For deep reference on individual features, see the [PromptBlocks](../prompting/blocks.md), [ConversationFlow](../prompting/flows.md), [Strategies](../prompting/strategies.md), [Guards](../prompting/guards.md), and [Context](../prompting/context.md) pages.

## What You'll Build

A prompt system for a data analyst agent with typed blocks, priority-based token budgeting, composable reasoning strategies, runtime guards, dynamic context injection, conversation flows that evolve across turns, version control, and automated testing. The same patterns used in production agent systems where prompt quality determines agent quality.

## Concepts

Most frameworks treat prompts as strings. Promptise treats them as **software components** -- composable, versioned, testable, debuggable. Prompts have types, priorities, lifecycle hooks, and debugging tools. They compose from independent parts. They adapt at runtime based on context. They drop gracefully when the context window gets tight.

The prompt system has three layers:

1. **PromptBlocks** -- typed building blocks (identity, rules, format, examples, context) with priorities that determine what survives when the context window is tight
2. **Strategies and perspectives** -- control how the agent reasons (chain-of-thought, self-critique, decompose) and from what angle (analyst, critic, advisor)
3. **Guards** -- enforce policy before and after generation (content filtering, length limits, schema validation with retry)

---

## Step 1: Typed Blocks

Start with the `@prompt` decorator and typed blocks instead of raw strings:

```python
from promptise.prompts import prompt
from promptise.prompts.blocks import blocks, Identity, Rules, OutputFormat

@prompt(model="openai:gpt-5-mini")
@blocks(
    Identity("Expert data analyst", traits=["statistical thinking", "clear communication"]),
    Rules(["Cite specific data points", "Include confidence intervals", "Never fabricate data"]),
    OutputFormat(format="markdown", instructions="Use ## headers for each section"),
)
async def analyze(dataset: str, question: str) -> str:
    """Given this dataset:
{dataset}

Answer this question: {question}"""

result = await analyze(
    dataset="Monthly users: Jan=10k, Feb=12k, Mar=15k",
    question="What is the growth trajectory?",
)
```

Eight block types, each with a specific role:

| Block | Priority | Purpose |
|-------|----------|---------|
| `Identity` | 10 (highest) | Who the agent is -- always included |
| `Rules` | 9 | Hard constraints the agent must follow |
| `OutputFormat` | 8 | How to structure the response |
| `ContextSlot` | Configurable | Dynamic runtime content (user info, memory, tool results) |
| `Section` | Configurable | Custom content blocks |
| `Examples` | 4 | Few-shot examples |
| `Conditional` | Varies | Blocks that appear/disappear based on predicates |
| `Composite` | Varies | Groups of blocks with shared config |

---

## Step 2: Priority-Based Token Budgeting

When the total prompt exceeds your token budget, the `PromptAssembler` drops blocks starting from the lowest priority. This is deterministic -- given the same blocks and budget, you get the same result every time.

```python
from promptise.prompts.blocks import blocks, Identity, Rules, Examples, Section, ContextSlot

@prompt(model="openai:gpt-5-mini", max_tokens=4000)
@blocks(
    Identity("Senior data analyst"),                          # Priority 10 -- last to drop
    Rules(["Cite sources", "Use provided data only"]),        # Priority 9
    ContextSlot("user_data", priority=7),                     # Priority 7
    Section("methodology", "Use regression analysis...", priority=5),  # Priority 5
    Examples([                                                 # Priority 4 -- first to drop
        {"input": "Revenue: $10M", "output": "Growth: stable"},
        {"input": "Revenue: $15M", "output": "Growth: 50% increase"},
    ]),
)
async def analyze(dataset: str) -> str:
    """Analyze: {dataset}"""
```

Drop order when budget is tight:

1. Examples go first (priority 4) -- the agent can often function without them
2. Custom sections go next (priority 5)
3. Context slots go based on their configured priority
4. Output format instructions survive longer (priority 8)
5. Rules survive almost everything (priority 9)
6. Identity is the last thing standing (priority 10)

---

## Step 3: Reasoning Strategies

Control how the agent thinks with composable strategies:

```python
from promptise.prompts.strategies import (
    chain_of_thought, self_critique, structured_reasoning,
    plan_and_execute, decompose,
)

# Chain of thought -- think step by step
@prompt(model="openai:gpt-5-mini")
async def analyze(text: str) -> str:
    """Analyze: {text}"""

result = await analyze.with_strategy(chain_of_thought)("Revenue data...")

# Self-critique -- generate, critique, improve
result = await analyze.with_strategy(self_critique)("Revenue data...")

# Compose strategies with +
result = await analyze.with_strategy(chain_of_thought + self_critique)("Revenue data...")
# Agent thinks step-by-step, then critiques its own reasoning

# Plan and execute -- create a plan, then follow it
result = await analyze.with_strategy(plan_and_execute)("Complex multi-step task...")

# Decompose -- break into sub-questions, answer each, synthesize
result = await analyze.with_strategy(decompose)("Multi-domain question...")
```

Five strategies compose freely:

| Strategy | How it reasons |
|----------|---------------|
| `chain_of_thought` | Step-by-step reasoning, then final answer |
| `structured_reasoning` | Formal premises, analysis, and conclusions |
| `self_critique` | Generate, critique, improve |
| `plan_and_execute` | Create plan, execute steps in order |
| `decompose` | Break into sub-questions, synthesize answers |

---

## Step 4: Perspectives

Perspectives frame *how* the agent thinks, orthogonal to what reasoning process it uses:

```python
from promptise.prompts.strategies import analyst, critic, advisor, creative, CustomPerspective

# Built-in perspectives
result = await analyze.with_perspective(analyst)("Revenue data...")
result = await analyze.with_perspective(critic)("Proposed strategy...")
result = await analyze.with_perspective(advisor)("Business decision...")
result = await analyze.with_perspective(creative)("Marketing campaign...")

# Custom perspective
security_reviewer = CustomPerspective(
    "You are a senior security engineer reviewing this system for vulnerabilities."
)
result = await analyze.with_perspective(security_reviewer)("Architecture doc...")

# Combine strategy + perspective
configured = (
    analyze
    .with_strategy(chain_of_thought + self_critique)
    .with_perspective(analyst)
)
result = await configured("Complex financial data...")
```

An analyst perspective with chain-of-thought produces different output than a creative perspective with the same strategy.

---

## Step 5: Guards

Guards enforce policy before the LLM call (input guards) and after the response (output guards):

```python
from promptise.prompts import prompt
from promptise.prompts.guards import guard, content_filter, length, schema_strict

# Content filtering -- block/require specific words
@prompt(model="openai:gpt-5-mini")
@guard(content_filter(blocked=["password", "secret", "ssn"]))
async def process(data: str) -> str:
    """Process: {data}"""

# Length enforcement -- min and max
@prompt(model="openai:gpt-5-mini")
@guard(length(min_length=100, max_length=2000))
async def summarize(text: str) -> str:
    """Summarize: {text}"""

# JSON schema validation with automatic retry
expected_schema = {
    "type": "object",
    "required": ["findings", "confidence"],
    "properties": {
        "findings": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

@prompt(model="openai:gpt-5-mini")
@guard(schema_strict(expected_schema, max_retries=3))
async def analyze_structured(data: str) -> str:
    """Analyze and return JSON: {data}"""

# If the output doesn't match the schema, the guard rejects it and retries
# with the validation error as feedback to the LLM
```

**Custom guards** -- any callable:

```python
from promptise.prompts.guards import input_validator, output_validator

def check_pii(text: str) -> bool:
    """Return False to reject."""
    return "SSN" not in text.upper()

@prompt(model="openai:gpt-5-mini")
@guard(
    input_validator(check_pii, error_message="Input contains PII"),
    output_validator(check_pii, error_message="Output contains PII"),
)
async def process(data: str) -> str:
    """Process: {data}"""
```

---

## Step 6: Context Providers

11 built-in context providers inject information into the prompt automatically before every LLM call:

```python
from promptise.prompts.context import (
    ToolContextProvider,
    MemoryContextProvider,
    UserContextProvider,
    EnvironmentContextProvider,
    ConversationContextProvider,
    ErrorContextProvider,
    StaticContextProvider,
    CallableContextProvider,
    ConditionalContextProvider,
)

# Static context -- always included
static = StaticContextProvider(
    "Company policy: all data analysis must include confidence intervals."
)

# Dynamic context from a function
async def get_user_prefs(ctx):
    return f"User timezone: {ctx.user.timezone}, language: {ctx.user.language}"

dynamic = CallableContextProvider(get_user_prefs, priority=6)

# Conditional context -- only when predicate is true
premium_ctx = ConditionalContextProvider(
    content="You have access to premium data sources.",
    predicate=lambda ctx: ctx.user.plan == "premium",
    priority=5,
)
```

| Provider | What it injects |
|----------|----------------|
| `ToolContextProvider` | Descriptions of available tools |
| `MemoryContextProvider` | Relevant memories from vector search |
| `TaskContextProvider` | Current task description and status |
| `UserContextProvider` | User identity, preferences, permissions |
| `EnvironmentContextProvider` | Environment variables and system info |
| `ConversationContextProvider` | Recent conversation history |
| `TeamContextProvider` | Peer agents and their capabilities |
| `ErrorContextProvider` | Recent errors for self-correction |
| `OutputContextProvider` | Expected output format and constraints |
| `StaticContextProvider` | Fixed content, always included |
| `CallableContextProvider` | Custom async function returning context |
| `ConditionalContextProvider` | Content included only when a predicate is true |

---

## Step 7: ConversationFlow

Static prompts work for simple agents. Complex conversational agents need prompts that change as the conversation progresses:

```python
from promptise.prompts import ConversationFlow, Phase

flow = ConversationFlow(
    phases={
        "greeting": Phase(
            blocks=["identity", "greeting_instructions", "capability_summary"],
            transitions={
                "investigation": lambda ctx: ctx.turn > 1,
            },
            on_enter=lambda ctx: print("Entering greeting phase"),
        ),
        "investigation": Phase(
            blocks=["identity", "investigation_rules", "tool_context", "memory_context"],
            transitions={
                "resolution": lambda ctx: ctx.state.get("has_enough_info"),
            },
        ),
        "resolution": Phase(
            blocks=["identity", "resolution_rules", "output_format", "examples"],
        ),
        "handoff": Phase(
            blocks=["identity", "handoff_instructions", "summary_format"],
        ),
    },
    initial_phase="greeting",
)
```

The system prompt on turn 1 uses greeting blocks. After the user responds, it transitions to investigation with different active blocks. Once the agent has enough information, the resolution phase activates with output format and examples. Each phase has `on_enter` and `on_exit` hooks for custom logic.

---

## Step 8: PromptBuilder and Registry

**Fluent runtime construction** when decorators aren't convenient:

```python
from promptise.prompts import PromptBuilder
from promptise.prompts.strategies import chain_of_thought, self_critique, analyst
from promptise.prompts.guards import schema_strict

prompt = (
    PromptBuilder("Analyze the following data: {data}")
    .identity("senior data analyst")
    .rules(["cite all sources", "use only provided data"])
    .strategy(chain_of_thought + self_critique)
    .perspective(analyst)
    .output_format("JSON with 'findings' and 'confidence' keys")
    .guard(schema_strict(schema))
    .build()
)

result = await prompt(data="Revenue: $10M, $12M, $15M")
```

**Version control** for prompts:

```python
from promptise.prompts import prompt, version

@prompt(model="openai:gpt-5-mini")
@version("1.0.0")
async def analyze(data: str) -> str:
    """Analyze the provided data and return findings."""
    ...

@prompt(model="openai:gpt-5-mini")
@version("2.0.0")
async def analyze(data: str) -> str:
    """Analyze the provided data with improved methodology."""
    ...

# Both versions coexist -- route to latest by default, or pin to specific version
# A/B test: deploy v2 to 10% of traffic, compare results, promote or roll back
```

**Shared defaults** with `PromptSuite`:

```python
from promptise.prompts import PromptSuite
from promptise.prompts.strategies import chain_of_thought, analyst
from promptise.prompts.guards import length

suite = PromptSuite(
    strategy=chain_of_thought,
    perspective=analyst,
    guards=[length(min_length=100)],
    constraints=["cite sources"],
)

@suite.prompt
async def analyze(data: str) -> str:
    """Analyze: {data}"""

@suite.prompt
async def summarize(data: str) -> str:
    """Summarize: {data}"""

# Both prompts inherit strategy, perspective, guards, and constraints
```

---

## Step 9: Testing and Debugging

**Unit test** your prompts with pytest:

```python
from promptise.prompts.testing import mock_llm, mock_context, assert_schema

async def test_analysis_prompt():
    with mock_llm(response='{"findings": ["growth stable"], "confidence": 0.85}'):
        result = await analyze(data="Revenue: $10M, $12M, $15M")

    assert_schema(result, expected_schema)
    assert "findings" in result

async def test_guard_blocks_pii():
    with pytest.raises(GuardError):
        await process(data="SSN: 123-45-6789")

async def test_context_injection():
    with mock_context(user={"plan": "premium", "timezone": "UTC"}):
        # Verify premium context is included
        result = await analyze(data="test")
        assert "premium data sources" in result.prompt_trace
```

**Debug** with `PromptInspector`:

```python
from promptise.prompts import PromptInspector

inspector = PromptInspector(analyze)
trace = await inspector.trace(data="Revenue: $10M, $12M, $15M")

print(f"Blocks included: {trace.included_blocks}")
print(f"Blocks dropped: {trace.dropped_blocks}")
print(f"Total tokens: {trace.total_tokens}")
print(f"Guard results: {trace.guard_results}")
print(f"Render time: {trace.render_time_ms}ms")
```

When your agent produces unexpected output, the inspector shows exactly what prompt it received, what context was injected, what blocks were dropped, and what guards ran.

---

## Prompt Chaining

Compose prompts into multi-step pipelines:

```python
from promptise.prompts.chaining import chain, parallel, branch, retry, fallback

# Sequential -- output of A feeds into B
pipeline = chain(research_prompt, analysis_prompt, formatting_prompt)
result = await pipeline("Raw data...")

# Parallel -- run simultaneously, collect all results
results = await parallel(analyst_prompt, critic_prompt, advisor_prompt)("Business plan")

# Conditional routing
router = branch(
    (lambda x: "financial" in x, financial_prompt),
    (lambda x: "technical" in x, technical_prompt),
    default=general_prompt,
)
result = await router("Financial report on Q3...")

# Retry with backoff on guard rejection or LLM error
resilient = retry(strict_prompt, max_retries=3, backoff=2.0)
result = await resilient("Input data...")

# Fallback -- try alternatives on failure
safe = fallback(detailed_prompt, simple_prompt, minimal_prompt)
result = await safe("Input data...")
```

---

## YAML Templates

Define prompts as portable files:

```yaml
# customer_analysis.prompt
name: customer_analysis
version: "1.0.0"
template: "Analyze this customer's behavior: {customer_data}"

blocks:
  - type: identity
    content: "You are a senior customer analytics specialist."
  - type: rules
    content:
      - "Base all conclusions on provided data only"
      - "Flag any data quality issues"
  - type: examples
    content: |
      Input: { "purchases": 12, "returns": 3 }
      Output: { "retention_risk": "medium", "reason": "25% return rate" }

strategy: chain_of_thought + self_critique
perspective: analyst

guards:
  - type: schema
    schema:
      type: object
      required: ["retention_risk", "reason"]
```

Load from a file, URL, or directory:

```python
from promptise.prompts.loader import load_prompt, load_directory

prompt = load_prompt("customer_analysis.prompt")
result = await prompt(customer_data='{"purchases": 5, "returns": 0}')

# Load all .prompt files from a directory
prompts = load_directory("prompts/")
```

---

## Complete Example

```python
import asyncio
from promptise.prompts import prompt, PromptSuite, ConversationFlow, Phase
from promptise.prompts.blocks import blocks, Identity, Rules, OutputFormat, Examples
from promptise.prompts.strategies import chain_of_thought, self_critique, analyst
from promptise.prompts.guards import guard, content_filter, schema_strict

# Schema for structured output
analysis_schema = {
    "type": "object",
    "required": ["findings", "confidence", "recommendations"],
    "properties": {
        "findings": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
}

# Create a suite with shared defaults
suite = PromptSuite(
    strategy=chain_of_thought + self_critique,
    perspective=analyst,
    constraints=["cite data sources", "include confidence intervals"],
)

@suite.prompt
@prompt(model="openai:gpt-5-mini")
@blocks(
    Identity("Senior data analyst", traits=["statistical rigor", "clear communication"]),
    Rules(["Use only provided data", "Never fabricate numbers", "Flag data quality issues"]),
    OutputFormat(format="json", instructions="Return valid JSON matching the schema"),
    Examples([
        {"input": "Revenue: $10M, $12M, $15M", "output": '{"findings": ["50% growth over 3 months"], "confidence": 0.9, "recommendations": ["Continue current strategy"]}'},
    ]),
)
@guard(
    content_filter(blocked=["password", "ssn"]),
    schema_strict(analysis_schema, max_retries=2),
)
async def analyze_data(dataset: str, question: str) -> str:
    """Dataset:
{dataset}

Question: {question}

Respond with valid JSON."""

async def main():
    result = await analyze_data(
        dataset="Q1: $2.1M, Q2: $2.8M, Q3: $3.2M, Q4: $3.9M",
        question="What is the annual growth trend and what should we expect for next year?",
    )
    print(result)

asyncio.run(main())
```

---

## What's Next

**Go deeper on each feature:**

| Feature used in this guide | Deep reference |
|---|---|
| Block types and priorities | [PromptBlocks](../prompting/blocks.md) |
| Conversation phases | [ConversationFlow](../prompting/flows.md) |
| Fluent construction | [Prompt Builder](../prompting/builder.md) |
| Context injection | [Context & Variables](../prompting/context.md) |
| Reasoning strategies | [Strategies](../prompting/strategies.md) |
| Input/output guards | [Guards & Validation](../prompting/guards.md) |
| Versioning and suites | [Suite & Registry](../prompting/suite-registry.md) |
| YAML templates | [Loader & Templates](../prompting/loader-templates.md) |
| Debugging and tracing | [Inspector & Observability](../prompting/inspector.md) |
| Testing utilities | [Testing Utilities](../prompting/testing.md) |
| Multi-step pipelines | [Prompt Chaining](../prompting/chaining.md) |

**Other guides:**

- [Building AI Agents](building-agents.md) -- The agents that use your prompts
- [Building Production MCP Servers](production-mcp-servers.md) -- The tools your agents call
- [Building Agentic Runtime Systems](agentic-runtime.md) -- Autonomous agents with governance
