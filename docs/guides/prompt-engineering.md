---
seo_title: "Prompt Engineering Tutorial: System Prompts"
description: "Build testable, versioned system prompts in 9 steps: typed blocks, token budgets, strategies, perspectives, guards, context providers, flows and tests."
---

# Prompt Engineering

Build testable, versioned system prompts in 9 incremental steps. Each step adds one concept, from typed blocks to reasoning strategies, runtime guards, conversation flows and prompt pipelines.

!!! tip "This is the recommended starting point for prompt engineering"
    This guide walks you through the prompt system step by step. For the full reference on each feature, see the [PromptBlocks](../prompting/blocks.md), [ConversationFlow](../prompting/flows.md), [Strategies](../prompting/strategies.md), [Guards](../prompting/guards.md) and [Context](../prompting/context.md) pages.

## What You'll Build

A prompt system for a data analyst agent: typed blocks, a priority-based token budget, composable reasoning strategies, runtime guards, context injection, a conversation flow that changes the system prompt across turns, versioning and tests that run without an API key.

## Concepts

Promptise treats a prompt as a component you compose, version, test and inspect, not as a single string. A prompt is an async function whose docstring is the template; decorators and `with_*()` methods attach the rest:

1. **PromptBlocks**: typed parts of the system prompt (identity, rules, output format, examples, context slots), each with a priority that decides what is dropped first under a token budget.
2. **Strategies and perspectives**: how the model reasons (chain of thought, self-critique, decompose) and from which angle (analyst, critic, advisor).
3. **Guards**: checks before the LLM call (input guards) and after it (output guards): blocked words, length bounds, JSON validity, or your own function.
4. **Context providers**: functions that add text to the prompt on every call (user profile, environment, errors, your own data).

!!! info "You need an API key for the LLM calls"
    The examples that `await` a prompt call the model. Put `OPENAI_API_KEY` in a `.env` file (Promptise loads it automatically). Steps 2 and 9 run without a key.

---

## Step 1: Typed Blocks

Start with the `@prompt` decorator and typed blocks instead of one long string:

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
print(result)
```

The function parameters become the template variables (`{dataset}`, `{question}`). The blocks are rendered in the order you list them and placed before the template text.

| Block | Priority | Purpose |
|-------|----------|---------|
| `Identity(description, *, name="identity", traits=None)` | 10 | Who the agent is ("You are ...") |
| `Rules(rules, *, name="rules")` | 9 | Numbered rules the agent must follow |
| `OutputFormat(*, format="text", schema=None, instructions="", name="output_format")` | 8 | How to structure the response |
| `ContextSlot(name, *, priority=6, default="")` | configurable | A slot you fill at runtime |
| `Section(name, content, *, priority=5)` | configurable | Any custom text, static or a callable |
| `Examples(examples, *, name="examples", max_count=None)` | 4 | Few-shot input/output pairs |
| `Conditional(name, block, *, condition)` | inner block's | Renders only when `condition(ctx)` is true |
| `Composite(name, blocks, *, separator="\n\n")` | highest inner | Groups blocks as one unit |

For one-off custom text you can also use `SimpleBlock(name, content, priority=5)` or the `@block(name, priority=5)` decorator. See [PromptBlocks](../prompting/blocks.md).

---

## Step 2: Priority-Based Token Budgeting

Priority never reorders the prompt. It only matters when you set a token budget: the `PromptAssembler` then drops blocks starting from the lowest priority (among equal priorities, the one listed last) until the rest fit. The result is deterministic.

```python
from promptise.prompts.blocks import (
    PromptAssembler, Identity, Rules, ContextSlot, Section, Examples,
)

assembler = PromptAssembler(
    Identity("Senior data analyst"),                                    # priority 10
    Rules(["Cite sources", "Use provided data only"]),                  # priority 9
    ContextSlot("user_data", priority=7),                               # priority 7
    Section("methodology", "Use regression analysis. " * 20, priority=5),
    Examples([                                                          # priority 4
        {"input": "Revenue: $10M", "output": "Growth: stable"},
        {"input": "Revenue: $15M", "output": "Growth: 50% increase"},
    ]),
)
assembler.fill_slot("user_data", "Region: EMEA, plan: enterprise")

full = assembler.assemble()
tight = assembler.assemble(token_budget=40)

print(full.estimated_tokens, full.included)
print(tight.estimated_tokens, tight.excluded)   # ['examples', 'methodology']
```

Drop order when the budget is tight:

1. `Examples` (priority 4)
2. `Section` blocks at their configured priority (default 5)
3. `ContextSlot` blocks at their configured priority (default 6)
4. `OutputFormat` (priority 8)
5. `Rules` (priority 9)
6. `Identity` (priority 10), dropped only if nothing else is left to drop

Token counts are estimates (about 1.3 tokens per word), not a tokenizer count. A budget applies where blocks are assembled with one: `PromptAssembler(token_budget=...)`, `assemble(token_budget=...)` and `ConversationFlow(token_budget=...)`. The `@prompt` decorator itself has no budget parameter; blocks attached with `@blocks` are always assembled in full.

---

## Step 3: Reasoning Strategies

A strategy adds reasoning instructions to the prompt before the call (`wrap()`) and strips the reasoning from the reply after it (`parse()`), so your code gets only the final answer:

```python
from promptise.prompts import prompt
from promptise.prompts.strategies import (
    chain_of_thought, self_critique, structured_reasoning,
    plan_and_execute, decompose,
)

@prompt(model="openai:gpt-5-mini")
async def analyze(text: str) -> str:
    """Analyze: {text}"""

# Chain of thought: think step by step, answer after ---ANSWER---
result = await analyze.with_strategy(chain_of_thought)("Revenue: Q1 $2.1M, Q2 $2.8M")

# Compose strategies with +
result = await analyze.with_strategy(chain_of_thought + self_critique)("Revenue: Q1 $2.1M, Q2 $2.8M")
print(result)
```

| Strategy | How it reasons |
|----------|---------------|
| `chain_of_thought` | Break down, analyze, conclude, then the final answer |
| `structured_reasoning` | Named phases, by default Understand, Analyze, Evaluate, Conclude (`StructuredReasoningStrategy(steps=[...])` for your own) |
| `self_critique` | Initial answer, critique, improved answer (`SelfCritiqueStrategy(rounds=1)`) |
| `plan_and_execute` | Numbered plan, execute each step, synthesize |
| `decompose` | Independent subproblems, solve each, combine |

Every built-in strategy asks the model to write `---ANSWER---` before the final answer and returns only the text after it. If the model leaves the marker out, the whole reply is returned.

---

## Step 4: Perspectives

A perspective prepends a framing sentence ("Approach this as an analyst...") to the prompt. It is independent of the strategy, so any pair works:

```python
from promptise.prompts.strategies import (
    analyst, critic, advisor, creative, perspective, chain_of_thought, self_critique,
)

# Built-in perspectives: analyst, critic, advisor, creative
result = await analyze.with_perspective(critic)("Plan: double the sales team next quarter")

# Custom perspective: a role plus optional instructions
security_reviewer = perspective(
    "senior security engineer",
    "Look for missing authentication, injection points and unsafe defaults.",
)
result = await analyze.with_perspective(security_reviewer)("Architecture: public S3 bucket, no auth")

# Strategy and perspective together
configured = (
    analyze
    .with_strategy(chain_of_thought + self_critique)
    .with_perspective(analyst)
)
result = await configured("Revenue: Q1 $2.1M, Q2 $2.8M, Q3 $3.2M")
print(result)
```

`perspective(role, instructions="")` returns a `CustomPerspective`; you can also construct `CustomPerspective(role=..., instructions=...)` directly. Every `with_*()` method returns a new `Prompt`, so `analyze` itself is unchanged.

---

## Step 5: Guards

Guards run before the LLM call (`check_input`) and after it (`check_output`). Each guard is attached to both sides and decides what to check:

```python
from promptise.prompts import prompt
from promptise.prompts.guards import guard, content_filter, length, schema_strict

# Blocked words: rejected in the outgoing prompt and in the reply
@prompt(model="openai:gpt-5-mini")
@guard(content_filter(blocked=["password", "ssn"]))
async def process(data: str) -> str:
    """Process: {data}"""

# Length bounds on the reply (characters)
@prompt(model="openai:gpt-5-mini")
@guard(length(min_length=50, max_length=2000))
async def summarize(text: str) -> str:
    """Summarize in two sentences: {text}"""

# The reply must be valid JSON
@prompt(model="openai:gpt-5-mini")
@guard(schema_strict())
async def analyze_structured(data: str) -> str:
    """Analyze the data and reply with a JSON object with keys
    "findings" (list of strings) and "confidence" (0 to 1). No prose: {data}"""

print(await analyze_structured("Revenue: $10M, $12M, $15M"))
```

A rejected input or output raises `GuardError` (with `guard_name` and `reason`). Guards don't retry: `schema_strict()` takes no arguments and only checks that a string reply parses as JSON. To get a typed, validated result, annotate the return type with a Pydantic model instead (see the complete example below); to try again on a rejection, wrap the prompt in [`retry()`](#prompt-chaining).

!!! warning "Input guards see the whole rendered prompt"
    In a standalone call, `check_input` receives the full text sent to the model: blocks, template, context, strategy and constraints. A `content_filter(blocked=["password"])` therefore also rejects a prompt whose own `Rules` mention "password". When the prompt is an agent's `instructions`, input guards check each user message instead.

**Custom guards**: wrap any function. An input validator gets the text and returns it (possibly changed); an output validator gets the parsed result and returns it. Raise to reject:

```python
from promptise.prompts import prompt
from promptise.prompts.guards import guard, input_validator, output_validator, GuardError
import re

SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

def redact_ssn(text: str) -> str:
    return SSN.sub("[REDACTED]", text)

def no_ssn_in_reply(output):
    if isinstance(output, str) and SSN.search(output):
        raise GuardError("Output contains an SSN", guard_name="no_ssn")
    return output

@prompt(model="openai:gpt-5-mini")
@guard(input_validator(redact_ssn), output_validator(no_ssn_in_reply))
async def process(data: str) -> str:
    """Summarize this customer note in one sentence: {data}"""

print(await process("Customer 123-45-6789 asked for a refund on order A-1001."))
```

---

## Step 6: Context Providers

A context provider is an object with `async def provide(ctx) -> str`. Before every call, each provider's text is appended to the prompt; an empty string adds nothing. Attach them with `@context(...)` or `with_context(...)`:

```python
from promptise.prompts import prompt
from promptise.prompts.context import (
    context, static_context, callable_context, conditional_context,
    user_context, UserContext,
)

# Fixed text on every call
policy = static_context(
    "All analysis must include confidence intervals.",
    header="## Policy",
)

# Text computed per call: sync or async, receives the PromptContext
async def user_prefs(ctx):
    if ctx.user is None:
        return ""
    return f"Language: {ctx.user.language}, expertise: {ctx.user.expertise_level}"

prefs = callable_context(user_prefs, header="## Preferences")

# Run an inner provider only when a condition holds
premium = conditional_context(
    condition=lambda ctx: ctx.user is not None and ctx.user.get("plan") == "premium",
    provider=static_context("You may use the premium data sources."),
)

@prompt(model="openai:gpt-5-mini")
@context(policy, user_context(), prefs, premium)
async def analyze(data: str) -> str:
    """Analyze: {data}"""

# World contexts are the data the providers read
personalized = analyze.with_world(user=UserContext(name="Dana", expertise_level="expert", plan="premium"))
print(await personalized.render_async(data="Q3 revenue $3.2M"))   # see the prompt, no LLM call
```

| Provider | Factory | What it injects |
|----------|---------|----------------|
| `UserContextProvider` | `user_context()` | Name, expertise, language, preferences and custom fields of `world["user"]` |
| `EnvironmentContextProvider` | `env_context()` | Platform, timezone, APIs and custom fields of `world["environment"]` |
| `ConversationContextProvider` | `conversation_context(last_n=10)` | Summary and last messages of `world["conversation"]` |
| `TeamContextProvider` | `team_context()` | Team members and the last 5 completed tasks of `world["team"]` |
| `ErrorContextProvider` | `error_context()` | Retry count, last error and the last 3 errors of `world["errors"]` |
| `OutputContextProvider` | `output_context()` | Format, schema, examples and constraints of `world["output"]` |
| `WorldContextProvider` | `world_context(key, header=None)` | Every field of any custom `world[key]` |
| `StaticContextProvider` | `static_context(text, header=None)` | Fixed text |
| `CallableContextProvider` | `callable_context(fn, header=None)` | The string your function returns |
| `ConditionalContextProvider` | `conditional_context(condition, provider)` | The inner provider's text when `condition(ctx)` is true |
| `MemoryContextProvider` | `memory_context(limit=5, min_score=0.3)` | Memories from the agent's memory provider (agent `instructions` only) |
| `TaskContextProvider` | `task_context()` | Goal, description and expected output of `ctx.task` (when set) |
| `BlackboardContextProvider` | `blackboard_context(prefix="result.")` | Prior results from `ctx.blackboard` (when set) |
| `ToolContextProvider` | `tool_context(include_schemas=False)` | Nothing: a placeholder, tools reach the model as tool definitions |

---

## Step 7: ConversationFlow

A conversational agent often needs a different system prompt at each stage. A `ConversationFlow` subclass defines phases with the `@phase` decorator; each phase handler activates or removes blocks, and the prompt is reassembled every turn:

```python
from promptise.prompts.flows import ConversationFlow, TurnContext, phase
from promptise.prompts.blocks import Identity, Rules, Section, OutputFormat

class AnalystFlow(ConversationFlow):
    base_blocks = [
        Identity("Senior data analyst"),
        Rules(["Use only data the user provides"]),
    ]

    @phase("greeting", initial=True)
    async def greet(self, ctx: TurnContext) -> None:
        ctx.activate(Section("greeting", "Ask which dataset the user wants analyzed."))
        if ctx.turn >= 1:
            ctx.transition("investigation")

    @phase("investigation")
    async def investigate(self, ctx: TurnContext) -> None:
        ctx.deactivate("greeting")
        ctx.activate(Section("investigation", "Ask clarifying questions about the data."))
        if "that's all" in ctx.history[-1]["content"].lower():
            ctx.state["has_enough_info"] = True
            ctx.transition("resolution")

    @phase("resolution", blocks=[OutputFormat(format="markdown")])
    async def resolve(self, ctx: TurnContext) -> None:
        ctx.deactivate("investigation")
        ctx.activate(Section("resolution", "Give findings with confidence levels."))

flow = AnalystFlow()
p = await flow.start("Hi")
print(flow.current_phase, p.included)    # greeting ['identity', 'rules', 'greeting']
p = await flow.next_turn("Monthly users: Jan=10k, Feb=12k")
print(flow.current_phase, p.included)    # investigation
p = await flow.next_turn("That's all the data")
print(flow.current_phase, p.included)    # resolution, with output_format
```

`ctx.transition()` inside a handler moves to the next phase after the handler returns, and the new phase's handler runs in the same turn. Blocks given in `@phase(blocks=[...])` are activated on entry and removed on exit; blocks added with `ctx.activate()` stay until you remove them. Pass the flow to `build_agent(flow=AnalystFlow())` and the agent keeps one copy per conversation (see [ConversationFlow](../prompting/flows.md#integration-with-build_agent)).

---

## Step 8: PromptBuilder, Versions and Suites

**Runtime construction** when decorators aren't convenient, for example when the template comes from a database:

```python
from pydantic import BaseModel
from promptise.prompts import PromptBuilder
from promptise.prompts.strategies import chain_of_thought, analyst
from promptise.prompts.guards import length

class Findings(BaseModel):
    findings: list[str]
    confidence: float

analyze_data = (
    PromptBuilder("analyze_data")
    .system("You are a senior data analyst. Cite all sources and use only the data provided.")
    .template("Analyze the following data: {data}")
    .strategy(chain_of_thought)
    .perspective(analyst)
    .constraint("Keep each finding under 20 words")
    .guard(length(max_length=5000))
    .output_type(Findings)
    .build()
)

result = await analyze_data(data="Revenue: $10M, $12M, $15M")
print(result.findings, result.confidence)
```

With a Pydantic `output_type` (or return annotation) the prompt adds the JSON schema to the request and parses the reply into the model.

**Versions**: `@version()` registers a prompt in the global `registry`. Put it above `@prompt`:

```python
from promptise.prompts import prompt, version, registry

@version("1.0.0")
@prompt(model="openai:gpt-5-mini")
async def summarize(data: str) -> str:
    """Summarize: {data}"""

@version("2.0.0")
@prompt(model="openai:gpt-5-mini")
async def summarize(data: str) -> str:
    """Summarize in three bullet points with one number each: {data}"""

print(registry.list())                       # {'summarize': ['1.0.0', '2.0.0']}
latest = registry.get("summarize")           # the most recently registered version
pinned = registry.get("summarize", "1.0.0")
registry.rollback("summarize")               # drops 2.0.0, 1.0.0 is latest again
```

**Shared defaults** with a `PromptSuite` subclass: every `@prompt` method inherits the class attributes, and its own settings win:

```python
from promptise.prompts import prompt, PromptSuite
from promptise.prompts.strategies import chain_of_thought, analyst
from promptise.prompts.guards import length

class AnalysisSuite(PromptSuite):
    default_strategy = chain_of_thought
    default_perspective = analyst
    default_guards = [length(max_length=3000)]
    default_constraints = ["Cite the numbers you use"]

    @prompt(model="openai:gpt-5-mini")
    async def analyze(self, data: str) -> str:
        """Analyze: {data}"""

    @prompt(model="openai:gpt-5-mini")
    async def summarize(self, data: str) -> str:
        """Summarize: {data}"""

suite = AnalysisSuite()
print(list(suite.prompts))                   # ['analyze', 'summarize']
print(await suite.summarize(data="Q1 $2.1M, Q2 $2.8M"))
```

---

## Step 9: Testing and Debugging

**Unit tests without an API key**: `PromptTestCase.mock_llm()` replaces the model with a fixed reply. Run with `pytest` and `pytest-asyncio` (`asyncio_mode = "auto"`):

```python
import pytest
from promptise.prompts import prompt, PromptTestCase, PromptInspector
from promptise.prompts.guards import guard, content_filter, GuardError
from promptise.prompts.context import context, user_context, UserContext

inspector = PromptInspector()

@prompt(model="openai:gpt-5-mini", inspect=inspector)
@context(user_context())
@guard(content_filter(blocked=["ssn"]))
async def analyze(data: str) -> str:
    """Analyze: {data}"""

class TestAnalyze(PromptTestCase):
    prompt = analyze

    async def test_reply_is_returned(self):
        with self.mock_llm("Growth is stable at 20% per month."):
            result, stats = await self.run_with_stats("Revenue: $10M, $12M")
        self.assert_contains(result, "stable")
        self.assert_latency(stats, max_ms=1000)

    async def test_guard_blocks_ssn(self):
        with self.mock_llm("never reached"):
            with pytest.raises(GuardError):
                await self.run_prompt("SSN 123-45-6789")

    async def test_user_context_reaches_prompt(self):
        with self.mock_context(user=UserContext(name="Dana", expertise_level="expert")):
            with self.mock_llm("ok"):
                await self.run_prompt("Q3 data")
        assert "**Expertise:** expert" in inspector.last().input_text
```

**Debugging** with `PromptInspector`: attach it with `@prompt(inspect=...)` or `with_inspector()`, then read the trace of each call:

```python
trace = inspector.last()
print(trace.prompt_name, trace.model)
print("Blocks included:", trace.blocks_included)
print("Blocks dropped:", trace.blocks_excluded)
print("Estimated block tokens:", trace.total_tokens_estimated)
print("Guards passed:", trace.guards_passed, "failed:", trace.guards_failed)
print("Prompt sent:", trace.input_text)
print("Latency:", trace.latency_ms, "ms")
print(inspector.summary())
```

When your agent produces unexpected output, the trace shows the exact text the model received, which blocks were included or dropped, and which output guards passed.

---

## Prompt Chaining

Compose prompts into multi-step pipelines with the operators in `promptise.prompts.chain`:

```python
from promptise.prompts import prompt
from promptise.prompts.chain import chain, parallel, branch, retry, fallback

@prompt(model="openai:gpt-5-mini")
async def extract(text: str) -> str:
    """List the numeric facts in: {text}"""

@prompt(model="openai:gpt-5-mini")
async def interpret(text: str) -> str:
    """Interpret these facts in two sentences: {text}"""

@prompt(model="openai:gpt-5-mini")
async def risks(text: str) -> str:
    """Name the two biggest risks in: {text}"""

@prompt(model="openai:gpt-5-mini")
async def summary(text: str) -> str:
    """One-sentence summary of: {text}"""

report = "Q3 revenue $3.2M (+14%), churn 6%, cash runway 9 months."

# Sequential: each output feeds the next prompt
pipeline = chain(extract, interpret)
print(await pipeline(report))

# Parallel: same input, results by name
results = await parallel(risks=risks, summary=summary)(report)
print(results["risks"], results["summary"])

# Conditional routing: the condition returns a route key
router = branch(
    condition=lambda text: "risk" if "churn" in text else "summary",
    routes={"risk": risks, "summary": summary},
    default=summary,
)
print(await router(report))

# Retry with exponential backoff (1s, 2s, 4s) on any exception, including GuardError
resilient = retry(interpret, max_retries=3, backoff=1.0)

# Fallback: try alternatives in order, the last one's error propagates
safe = fallback(interpret, summary)
print(await safe(report))
```

---

## YAML Prompt Files

Define a prompt as a portable `.prompt` file (YAML). The model is a runtime choice; blocks are not part of the file format, so put that text in the template:

```yaml
# customer_analysis.prompt
name: customer_analysis
version: "1.0.0"
description: Retention risk for one customer
template: |
  You are a senior customer analytics specialist.
  Base all conclusions on the provided data only and flag data quality issues.

  Analyze this customer's behavior: {customer_data}
  Reply as JSON with keys "retention_risk" and "reason".
arguments:
  customer_data:
    description: Customer purchase history as JSON
    required: true
strategy: [chain_of_thought, self_critique]
perspective: analyst
guards:
  - type: schema_strict
  - type: length
    max_length: 2000
```

Load from a file, URL, or directory:

```python
from promptise.prompts.loader import load_prompt, load_directory

customer_analysis = load_prompt("customer_analysis.prompt")
result = await customer_analysis(customer_data='{"purchases": 5, "returns": 0}')

# Every .prompt file in a directory, in a PromptRegistry
prompts = load_directory("prompts/")
```

See [Loader & Templates](../prompting/loader-templates.md) for every field, the template syntax and `${ENV_VAR}` resolution.

---

## Complete Example

```python
import asyncio
from pydantic import BaseModel
from promptise.prompts import prompt, PromptSuite
from promptise.prompts.blocks import blocks, Identity, Rules, OutputFormat, Examples
from promptise.prompts.strategies import chain_of_thought, analyst
from promptise.prompts.guards import guard, content_filter
from promptise.prompts.chain import retry

class Analysis(BaseModel):
    findings: list[str]
    confidence: float
    recommendations: list[str]

class DataTeam(PromptSuite):
    default_strategy = chain_of_thought
    default_perspective = analyst
    default_constraints = ["Cite data sources", "Include a confidence between 0 and 1"]
    default_guards = [content_filter(blocked=["password", "ssn"])]

    @prompt(model="openai:gpt-5-mini")
    @blocks(
        Identity("Senior data analyst", traits=["statistical rigor", "clear communication"]),
        Rules(["Use only provided data", "Never fabricate numbers", "Flag data quality issues"]),
        OutputFormat(format="json"),
        Examples([
            {
                "input": "Revenue: $10M, $12M, $15M",
                "output": '{"findings": ["50% growth over 3 months"], "confidence": 0.9, "recommendations": ["Continue current strategy"]}',
            },
        ]),
    )
    async def analyze_data(self, dataset: str, question: str) -> Analysis:
        """Dataset:
        {dataset}

        Question: {question}"""

async def main():
    team = DataTeam()
    analyze = retry(team.analyze_data, max_retries=2, backoff=0.5)
    result = await analyze(
        dataset="Q1: $2.1M, Q2: $2.8M, Q3: $3.2M, Q4: $3.9M",
        question="What is the annual growth trend and what should we expect next year?",
    )
    print(result.findings)
    print(result.confidence, result.recommendations)

asyncio.run(main())
```

The return annotation `-> Analysis` makes the prompt send the JSON schema and parse the reply into an `Analysis`; a reply that does not validate raises, and `retry()` tries again.

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

- [Building AI Agents](building-agents.md): the agents that use your prompts
- [Building Production MCP Servers](production-mcp-servers.md): the tools your agents call
- [Building Agentic Runtime Systems](agentic-runtime.md): autonomous agents with governance

!!! tip "Learn the concept"

    - [System Prompt Best Practices for AI Agents in Python](https://www.promptise.com/guides/view/ai-agent-system-prompt-best-practices): build system prompts from reusable blocks and test them like code.
    - [How to Test LLM Prompts and AI Agents in Python](https://www.promptise.com/guides/view/test-llm-prompts-ai-agents-python): catch prompt regressions with pytest and an LLM judge.
