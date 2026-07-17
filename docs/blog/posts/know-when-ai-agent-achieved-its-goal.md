---
title: "Know When Your AI Agent Hit Its Goal (LLM Judge)"
description: "A turn cap tells you the agent stopped, not that it succeeded; shows a separate judge model scoring achievement and confidence every N invocations against…"
keywords: "know when ai agent achieved its goal, llm as a judge agent completion, ai agent success evaluation, llm as judge for agents, agent goal completion detection, mission success eval"
date: 2026-07-16
slug: know-when-ai-agent-achieved-its-goal
categories:
  - Governance
---

# Know When Your AI Agent Hit Its Goal (LLM Judge)

To **know when your AI agent achieved its goal** — not merely that it stopped — you need something that evaluates the objective, because a turn cap only proves a counter ran out. This is the quiet failure mode of every long-running agent: it exits cleanly, your logs go green, and nobody notices that the migration is half-done, the queue still has open items, or the research brief never actually answered the question. "Stopped" and "succeeded" are different facts, and almost every framework only measures the first one. This post shows the difference between a hard limit and an evaluated judgment, and how Promptise Foundry wires a separate judge model into the runtime so a supervised process ends when the mission is genuinely met.

The thesis in one line: `max_turns` tells you the agent quit; only a judge scoring your success criteria tells you it won.

## Why "it stopped" is not "it succeeded"

Every completion signal a framework hands you falls into one of three buckets, and none of them answers "is the goal met?"

- **A hard limit.** `max_iterations`, `recursion_limit`, `max_turns`, a token budget. These stop the agent when it does *too much*. Hitting one is, by definition, a failure signal — the agent ran out of runway before finishing. Treating "we hit the cap" as "we're done" is backwards.
- **A self-declaration.** The agent emits a final answer, or a magic string like `"TERMINATE"`. This is the agent grading its own homework. A model that has drifted off-task is exactly the model most likely to confidently announce it is finished.
- **A static match.** An `expected_output` description or a termination string that fires when the transcript contains some text. It checks shape, not achievement — "the output looks like a summary," not "the summary is correct and the queue is empty."

None of these look at the world and ask whether the *objective* is satisfied. For a chatbot that a human reads turn by turn, that gap does not matter. For an agent that runs unattended on a cron, a webhook, or a file trigger — accumulating work across dozens of invocations — it is the whole ballgame. A counter that resets to zero every invocation cannot tell you the mission is complete, and it cannot tell you the mission has silently failed either. This is the same reframe behind [How to Stop a Runaway AI Agent (Runtime Kill Switches)](stop-a-runaway-ai-agent.md): the control that matters lives *above* the invocation, on the process, not inside a single `ainvoke()`.

## What other frameworks do today

To be fair and precise: LLM-as-a-judge is not a new idea, and every serious framework ships *some* stop condition. The point is not that competitors "can't" — it is where each draws the line, and what none of them make first-class.

| Framework | Completion / stop mechanism | An evaluated judgment of the goal? |
|-----------|-----------------------------|------------------------------------|
| **LangChain** | `AgentExecutor` `max_iterations` / `max_execution_time`; Final-Answer detection; `early_stopping_method` | No — a step cap or the agent's own self-declared final answer |
| **LangGraph** | `recursion_limit` (default 25) → `GraphRecursionError`; execution ends at the `END` node | No — a hard cap or graph topology, not goal achievement |
| **AutoGen** | `TerminationCondition` family: `MaxMessageTermination`, `TextMentionTermination` (fires on a string like `"TERMINATE"`), `TokenUsageTermination`, `TimeoutTermination` | Partial — `TextMentionTermination` is a static string match the agent triggers itself |
| **CrewAI** | `max_iter` / `max_rpm`; `Task.expected_output` (a target description); optional `Task.guardrail` (callable or LLM validation of a task's output, with retry) | Partial — a `guardrail` validates one task's output at completion, not a periodic mission judge across invocations |
| **Pydantic AI** | `UsageLimits`; a typed final result ends the run; `@agent.output_validator` can raise `ModelRetry` | Partial — validates a single run's output shape, not whether a multi-step objective is met over time |
| **Promptise** | `MissionConfig` + a separate periodic `eval_model` → `MissionEvaluation(achieved, confidence, reasoning, progress_summary)`; `auto_complete` stops the process | Yes — an independent judge scores achievement *and* confidence against your criteria, and completes or escalates the process |

Two of these deserve their partial credit stated plainly. CrewAI's task `guardrail` genuinely uses an LLM to validate output and can force a retry — a real, useful feature — but it fires once, on a single task's output, checking that output against criteria; it is not a judge that runs every N invocations across a long-lived process and decides the *mission* is done. Pydantic AI's output validators are similar: they police the shape and validity of one run's typed result, not the achievement of an objective accumulated over many runs.

There is a second, honest caveat worth naming: LLM-as-judge is a well-established *evaluation* technique. Tools like LangSmith evaluators, Ragas, and DeepEval let you score recorded agent runs with a judge model — offline, in a test harness or CI, after the fact. That is exactly the right place for regression testing. What none of them do is wire that judge into the **live runtime** as a completion gate: a periodic evaluator that scores a running, trigger-driven process and either stops it on success or escalates on low confidence. Promptise's edge is not inventing the judge — it is making the judge a structural, first-class property of a supervised process instead of a script you bolt on around your own loop.

## The mission judge: a separate model scoring achievement and confidence

In Promptise Foundry's [Agent Runtime](../../runtime/index.md), an agent can run as a *mission-oriented process*: it keeps working across invocations until an objective is met, and the runtime — not the agent — decides when that is. You configure it with a [`MissionConfig`](../../runtime/governance/mission.md):

```python
from promptise.runtime import ProcessConfig, MissionConfig, EscalationTarget

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Reconcile open payment disputes until the queue is clear.",
    mission=MissionConfig(
        enabled=True,
        objective="Reconcile every open payment dispute",
        success_criteria="Zero disputes remain in the open queue, each with an audit note",
        eval_model="openai:gpt-5-mini",   # a SEPARATE judge, distinct from the worker
        eval_every=3,                     # judge every 3 invocations
        confidence_threshold=0.7,         # below this -> escalate to a human
        auto_complete=True,               # goal met -> stop the process
        timeout_hours=8,
        max_invocations=50,
        escalation=EscalationTarget(webhook_url="https://hooks.slack.com/services/XXX"),
    ),
)
```

Every `eval_every` invocations the runtime pauses the work loop, gathers the recent conversation, the agent's state, and its tool-call history, and hands that bundle to the `eval_model`. The judge returns a structured `MissionEvaluation` with four fields that matter:

- **`achieved`** (`bool`) — is the objective actually met?
- **`confidence`** (`0.0`–`1.0`) — how sure is the judge?
- **`reasoning`** — why it decided that, in words you can read in the journal.
- **`progress_summary`** — where things stand, injected back into the agent's next system prompt so it knows what remains.

Then the runtime acts on that judgment, not on a counter:

- If `achieved` is true and `auto_complete=True`, the process transitions `RUNNING → STOPPING → STOPPED`. The next cron tick or webhook has nothing to run against — the agent stopped *because it won*.
- If `confidence` falls below `confidence_threshold`, the mission pauses and fires an escalation (webhook plus an EventBus event) so a human can look before the agent wanders further. Low confidence is treated as "get a person," not "keep going."

This is the deliberate complement to a cost cap. An [Autonomy Budget](../../runtime/governance/budget.md) stops an agent that does *too much*; the mission judge stops an agent when it has done *enough*. One is a ceiling, the other is a finish line, and a serious autonomous process wants both.

## Runnable: a deterministic success_check plus an LLM judge

An LLM judge is the right tool when "done" is fuzzy ("the research brief adequately answers the question"). But when "done" is an objective, checkable fact, you should not spend tokens asking a model to confirm arithmetic. Promptise lets you attach a programmatic `success_check(MissionEvidence) -> bool | None` that runs *first*: return `True`/`False` for a definitive answer, or `None` to fall through to the LLM judge. It is the same `MissionEvaluation` contract either way — only the `source` field changes.

The snippet below is fully runnable and needs **no API key**, because the deterministic check short-circuits before any model call. Every symbol is a real runtime export.

```python
import asyncio
from promptise.runtime import MissionConfig, MissionTracker, MissionEvidence, MissionState


def all_disputes_closed(evidence: MissionEvidence) -> bool | None:
    """Objective completion check — no LLM, no tokens, no ambiguity."""
    open_count = evidence.state.get("open_disputes")
    if open_count is None:
        return None  # inconclusive -> defer to the LLM judge
    return open_count == 0


async def main() -> None:
    tracker = MissionTracker(
        config=MissionConfig(
            enabled=True,
            objective="Reconcile every open payment dispute",
            success_criteria="Zero disputes remain in the open queue",
            eval_every=3,
            confidence_threshold=0.7,
            auto_complete=True,
        ),
        process_id="dispute-agent",
        success_check=all_disputes_closed,
    )

    # Simulate three invocations; the mission is evaluated on the third.
    for _ in range(3):
        tracker.increment_invocation()
    print("eval due?", tracker.should_evaluate())  # True at invocation 3

    # State carries an objective, measurable fact the check can read.
    evidence = MissionEvidence(state={"open_disputes": 0}, invocation_count=3)
    result = await tracker.evaluate(evidence, model="openai:gpt-5-mini")

    print("achieved:  ", result.achieved)    # True
    print("confidence:", result.confidence)  # 1.0
    print("source:    ", result.source)      # programmatic
    print("state:     ", tracker.state)      # MissionState.COMPLETED
    assert result.achieved is True
    assert result.source == "programmatic"
    assert tracker.state is MissionState.COMPLETED


asyncio.run(main())
```

Running it prints `achieved: True`, `source: programmatic`, and `MissionState.COMPLETED` — the mission completed on a fact, for free. Flip `open_disputes` to a non-zero value and `should_evaluate()` still fires, but the check returns `False` and the mission stays `ACTIVE`. Set the fact to something the check *cannot* determine (return `None`) and the same `evaluate()` call falls through to the `eval_model` and produces an LLM-sourced judgment instead. Combined mode gives you the best of both: cheap, deterministic checks for the objective parts of "done," and a judge for the parts that need reading comprehension.

Because the judge scores *behavior over time* rather than a single output, it also catches the agent that looks busy but is quietly spinning — the failure walked through in [Catch an AI Agent Stuck Repeating the Same Tool Call](ai-agent-stuck-repeating-tool-call.md). A stuck agent racks up invocations without moving the `progress_summary`; the next evaluation sees no progress and escalates instead of declaring victory.

## When you don't need a mission judge

A mission judge earns its keep for unattended agents that accumulate work toward a goal. It is honest overkill elsewhere, and a simpler control is the right answer:

- **Request-response chatbots.** A human reads every reply; there is no multi-invocation objective to evaluate. A plain `build_agent()` with a sensible iteration cap is enough.
- **Single-shot handlers.** One tool call behind an endpoint has no trajectory. Adding a periodic judge just adds latency and an extra model bill.
- **Objectively checkable outcomes.** If "done" is fully decidable in code — a row count, a checksum, an exit status — use only the `success_check` callable and skip the LLM entirely. Reserve the judge for the fuzzy criteria that genuinely need a model.

The line is crossed the moment an agent runs on a trigger, works across many invocations, and needs to keep going until a goal that a counter cannot see is met. That is exactly when "it stopped" stops being a useful signal and "a judge says it succeeded" starts being one.

## Frequently asked questions

### Does `max_turns` or `recursion_limit` tell me my agent succeeded?

No. Those are hard limits that stop the agent when it exceeds an amount of work, and hitting one is a *failure* signal — the run was cut off. They say nothing about whether the objective was met. To know that, you need an evaluated judgment against your success criteria, which is what a `MissionConfig` judge (or a deterministic `success_check`) provides.

### Is the judge the same model as the agent doing the work?

It can be, but it does not have to be — that is the point of the separate `eval_model` field. Using a distinct (often cheaper) evaluator model keeps the judgment independent of the worker's own optimism, and lets you evaluate on a small, fast model even when the agent runs on a larger one. If you leave `eval_model` unset, the process model is used.

### What happens when the judge is not confident?

If `confidence` drops below `confidence_threshold`, the mission pauses and fires the configured escalation — a webhook plus an EventBus event — so a human is pulled in before the agent drifts further. Low confidence routes to a person; it does not silently continue. High confidence with `achieved=True` and `auto_complete=True` stops the process.

### Can I complete a mission without any LLM call?

Yes. Attach a `success_check(MissionEvidence) -> bool | None` callable. Returning `True`/`False` decides the mission with `source="programmatic"` and no model call at all; returning `None` defers to the `eval_model`. This is ideal when part of "done" is an objective fact (a queue is empty, a checksum matches) and part needs a judgment call.

### How often does the judge run, and does it slow the agent down?

It runs every `eval_every` invocations, so you control the cost/latency trade-off directly — evaluate every invocation for tight control, or every tenth for cheap, coarse checks. Between evaluations there is zero judge overhead, and a `success_check` that resolves the objective parts short-circuits the LLM path entirely.

## Next steps

Define a `MissionConfig` with a real `objective` and `success_criteria`, point `eval_model` at a small judge, and let `auto_complete` stop the process the moment the goal is genuinely met — then add a `success_check` for the parts of "done" you can decide in code. Start from the runnable snippet above, read the [Mission-Oriented Process Model](../../runtime/governance/mission.md) reference for the full evaluation cycle and `MissionEvaluation` contract, and use the [Agent Runtime overview](../../runtime/index.md) plus the [Autonomy Budget reference](../../runtime/governance/budget.md) to pair the finish line with a ceiling so your agent both stops when it is done and never runs away before it gets there.
