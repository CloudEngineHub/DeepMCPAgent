---
title: "How an Autonomous Agent Knows When Its Goal Is Done"
description: "A long-running agent that never decides it's finished either stops too early or burns budget forever. This deep-dives the runtime's Mission subsystem (a…"
keywords: "autonomous agent goal completion, how agent knows when task is done, mission-oriented ai agent, llm as judge agent completion, agent success criteria, when to stop an autonomous agent"
date: 2026-07-16
slug: autonomous-agent-goal-completion
categories:
  - Runtime
---

# How an Autonomous Agent Knows When Its Goal Is Done

Reliable **autonomous agent goal completion** is not a boolean the agent emits when it feels finished — it is a judgment an independent evaluator makes about accumulated evidence, and the difference between those two definitions is the difference between an agent you can leave running and one you have to babysit. A long-running agent has to answer a question no single LLM call can answer on its own: *is the goal actually done, or does it just look done from inside the transcript?* Get that answer wrong in one direction and the agent quits with the job half-finished; get it wrong in the other and it runs on its trigger forever, re-checking work that was completed an hour ago. This post digs into the subsystem Promptise Foundry uses to answer it: a **mission** — an objective, success criteria, and a periodic LLM-as-judge that scores a persistent process against them until the goal is genuinely met, then auto-completes.

<!-- more -->

The thesis in one line: an autonomous agent should not decide it is finished because it *said* so — it should be judged finished because a separate evaluator, looking at the evidence, is confident the objective is met.

## Why "done" is the hardest state in an autonomous loop

An agent that runs once behind a request and returns to a human never has to know when it is done — the human does. The moment you put that agent on a trigger and walk away, "done" becomes the agent's problem, and it fails in two opposite directions.

**It stops too early.** Most agent loops terminate when the model produces a final-answer marker or a designated stop keyword. But the model is a confident narrator of its own success. It will announce "all tables migrated" while two are still on the old schema, because from inside the conversation the work *reads* as finished. Terminate on that self-report and you ship a half-done goal wearing a green checkmark.

**It runs forever.** Over-correct by giving the agent no completion authority at all, and a trigger keeps firing indefinitely. The agent wakes every few minutes, re-inspects a queue that emptied long ago, and spends real tokens confirming there is nothing left to do. Nobody told it the goal was met, so it never stops.

Both failures share one root cause: treating goal completion as a signal the *actor* produces about its own work. "Confidently finished," "confidently off-track," and "not sure yet" are three different states, and nothing the agent says about itself can separate them. Only an assessment made *about* the agent — by something other than the agent — can. That is what a mission-oriented AI agent adds to the loop.

## How a Promptise mission decides "done"

A mission turns a task-runner into a goal-driven process. You declare two things — an `objective` and `success_criteria` — and the runtime takes over the question of completion. Every `eval_every` invocations, the [Mission-Oriented Process Model](../../runtime/governance/mission.md) runs an **LLM-as-judge**: a *separate* `eval_model`, not the actor, reads the objective, the success criteria, and a bundle of evidence, then returns a structured `MissionEvaluation` with four load-bearing fields — `achieved` (bool), `confidence` (0.0–1.0), `reasoning`, and `progress_summary`.

Those results drive completion directly. When the judge marks the objective `achieved` and `auto_complete=True`, the runtime transitions the process `ACTIVE → COMPLETED` and stops it — no further invocations, no more tokens. This is what separates a judged completion from a rubber stamp: the process stops because an *independent* evaluator assessed the evidence and found the goal met, not because the agent claimed victory.

The judge is not the only signal. A mission also carries a programmatic `success_check` — a plain `Callable[[MissionEvidence], bool | None]` that runs *before* any LLM call. Return `True` for an objectively-met criterion (a queue is empty, every row validates) and the mission records a confident `confidence=1.0` completion with `source="programmatic"` — the identical completion path `auto_complete` drives in production, minus the model call. Return `None` and control falls through to the LLM judge for the fuzzy cases. So an agent whose "done" is objectively checkable spends zero tokens deciding it; an agent whose "done" is a matter of judgment gets a judge. The two compose on the same mission.

Around that decision sit the guardrails that stop a mission from running forever even when the judge stays unsure. A wall-clock `timeout_hours` deadline fails the mission the moment it passes, and a `max_invocations` ceiling bounds it by count. Between the judge (semantic drift) and those backstops (wall-clock and count), a mission cannot quietly outlive its usefulness. The full field set — `eval_every`, `confidence_threshold`, `timeout_hours`, `max_invocations`, `auto_complete`, `eval_model`, `escalation` — is declared once on a `MissionConfig`, and the [Agent Runtime process](../../runtime/processes.md) evaluates it around every invocation without a supervising loop in your code.

## What actually goes into the judgment

A judge is only as good as what it sees, and the failure mode of naive "LLM-as-judge agent completion" is handing the evaluator the same transcript the actor already convinced itself with. Promptise's judge does not score a transcript — it scores a `MissionEvidence` bundle the runtime assembles automatically from four independent sources:

- **`conversation`** — recent messages, so the judge reads what the agent *said* it did.
- **`state`** — the `AgentContext` key-value snapshot, so the judge reads the ground truth the agent *changed*.
- **`tool_calls`** — the recent tool-call log, so the judge sees which actions actually fired, not just which ones were narrated.
- **`trigger_event`** — what woke the agent this invocation.
- **`invocation_count`** — how long the mission has been running.

The `state` and `tool_calls` are the antidote to the confident-narrator problem. An agent can *write* "migration complete" into the conversation, but it cannot fake a `state` snapshot that still shows `tables_remaining=2` or a tool log missing the calls that would have done the work. Judging over evidence — not over self-report — is precisely what lets the evaluator disagree with the actor. And because completion is a property of the persistent process rather than a single call, that decision is durable: every mission state transition and evaluation is written to the runtime [journal](../../runtime/journal/index.md), so a mission's progress survives a crash and can be reconstructed on replay rather than restarting from zero. If you want the deeper version of why a journaled process — not an in-call check — is what makes "done" reliable across restarts, [Durable Execution for AI Agents in Python](durable-execution-for-ai-agents.md) walks the full model.

## Runnable: let the agent decide it's finished

The completion decision is real code you can exercise without an API key. Because the programmatic `success_check` runs before any LLM call, you can drive the exact completion path `auto_complete` uses in production — deterministically, for free. Here a mission holds until its objective is genuinely met, then auto-completes on the confident success:

```python
import asyncio

from promptise.runtime import MissionConfig, MissionEvidence, MissionState, MissionTracker


def migration_complete(evidence: MissionEvidence) -> bool | None:
    """Objective, programmatic success check — runs before any LLM judge.

    Return True/False when the evidence answers the question outright;
    return None to defer to the LLM-as-judge.
    """
    remaining = evidence.state.get("tables_remaining")
    if remaining is None:
        return None
    return remaining == 0


async def main() -> None:
    tracker = MissionTracker(
        config=MissionConfig(
            enabled=True,
            objective="Migrate every table to the v2 schema",
            success_criteria="Zero tables remain on the v1 schema",
            eval_every=3,               # judge every 3rd invocation
            confidence_threshold=0.7,   # below this -> escalate to a human
            auto_complete=True,         # confident success -> stop the process
        ),
        process_id="schema-migrator",
        success_check=migration_complete,
    )

    tables_remaining = 6
    for turn in range(1, 7):
        tables_remaining = max(0, tables_remaining - 1)  # one table per turn
        tracker.increment_invocation()

        if not tracker.should_evaluate():
            print(f"turn {turn}: {tables_remaining} left  ->  no evaluation due")
            continue

        evidence = MissionEvidence(
            state={"tables_remaining": tables_remaining},
            invocation_count=turn,
        )
        result = await tracker.evaluate(evidence, model="openai:gpt-5-mini")
        print(
            f"turn {turn}: {tables_remaining} left  ->  achieved={result.achieved} "
            f"confidence={result.confidence} source={result.source} "
            f"state={tracker.state.value}"
        )

        if tracker.state is MissionState.COMPLETED:
            print("Mission COMPLETED — auto_complete stops the process here.")
            break


asyncio.run(main())
```

Running it prints the whole completion arc:

```
turn 1: 5 left  ->  no evaluation due
turn 2: 4 left  ->  no evaluation due
turn 3: 3 left  ->  achieved=False confidence=1.0 source=programmatic state=active
turn 4: 2 left  ->  no evaluation due
turn 5: 1 left  ->  no evaluation due
turn 6: 0 left  ->  achieved=True confidence=1.0 source=programmatic state=completed
Mission COMPLETED — auto_complete stops the process here.
```

Two things to notice. First, evaluation only runs when `should_evaluate()` says it is due — every `eval_every` invocations — so most turns carry zero completion overhead. Second, the mission does *not* complete at turn 3 just because the agent has been working; it completes at turn 6 because the objective is *actually* satisfied. Swap `success_check` for `None` and the same `evaluate()` call falls through to the LLM judge over the `MissionEvidence` bundle, where the `confidence`-based branch decides whether to keep going or escalate. That confidence-gated stop-or-escalate decision is unpacked on its own in [Set a Confidence Threshold to Stop or Escalate an Agent](confidence-threshold-to-stop-ai-agent.md).

## What other frameworks do today

To be fair and precise: every serious framework ships real termination controls, and each is genuinely useful. The distinction is *what they stop on* — a per-run condition, versus a periodic judgment about a persistent goal.

- **AutoGen** — AgentChat ships composable termination conditions: `TextMentionTermination` (stop when a designated string such as `"TERMINATE"` appears), `MaxMessageTermination`, `TokenUsageTermination`, `TimeoutTermination`, and `ExternalTermination`, combinable with `|` and `&`. These are real and expressive, but they end a *team conversation* — they signal "this chat is over." `TextMentionTermination` in particular is the boolean self-report failure mode by design: the run halts because a model *emitted a word*, with no independent confidence assessment of whether the underlying goal was met.
- **CrewAI** — an `Agent`'s `max_iter` caps reasoning iterations and `max_rpm` throttles rate; a task declares an `expected_output` and can attach a `guardrail` callable that validates the returned result. That is genuine output validation, and it is worth using. But completion still means "the task returned its output"; there is no built-in evaluator that periodically scores a *long-running* process against an objective, returns a numeric confidence, and auto-completes the process on a confident success.
- **LangGraph** — the most composable of the three: a `recursion_limit` plus conditional edges you author to route to `END`. You genuinely *can* build a periodic judge here — a node that scores progress, a conditional edge that branches on the score. But you write the judge, the evidence assembly, the threshold comparison, and the completion branch yourself, and you re-wire it into every graph. The primitives exist; the *control* is not first-class. (The related question of how LangGraph persists that progress across restarts is compared directly in [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md).)

Here is the exact delta, stated honestly. None of these frameworks *lack* a way to stop — they all stop well, per run. What none of them ship as a declared, first-class control is a **periodic LLM-as-judge that scores a persistent process against an objective and success criteria, over a structured evidence bundle of conversation + state + tool log + trigger, returns a confidence, and auto-completes the process when the goal is genuinely met.** For anything long-running, you hand-roll that stop condition. Promptise's edge is not that competitors "can't decide done" — it is that Promptise makes "is the goal achieved?" a *structural property of the mission*: `objective`, `success_criteria`, `eval_every`, `confidence_threshold`, and `auto_complete` are fields you set on a `MissionConfig`, not a supervising loop you build and remember to wire into every agent.

## Frequently asked questions

### How does the agent know when the task is done without a human checking?

An independent evaluator answers for it. Every `eval_every` invocations the runtime runs an LLM-as-judge — a separate `eval_model` — over a `MissionEvidence` bundle and returns a structured `MissionEvaluation` with `achieved` and `confidence`. When `achieved` is true and `auto_complete=True`, the process stops. For objectively checkable goals, a programmatic `success_check` decides completion with no model call at all.

### Why not just stop when the model outputs "done"?

Because the model is judging its own work from inside the transcript that convinced it. Promptise scores a `MissionEvidence` bundle that includes the `AgentContext` `state` snapshot and the real `tool_calls` log — ground truth the actor cannot fake by narrating success. The judge can, and routinely does, disagree with an agent that *says* it is finished.

### What stops a mission from running forever if the judge stays unsure?

Two backstops in the same `MissionConfig`. `timeout_hours` fails the mission on a wall-clock deadline, and `max_invocations` fails it on a count ceiling. The judge handles semantic drift; the backstops handle cost. A mission bounded by both cannot quietly run indefinitely.

### Does every invocation pay for an LLM judge call?

No. Evaluation runs only when `should_evaluate()` is true — once every `eval_every` invocations — so most turns carry no judge overhead, and you can point `eval_model` at a cheaper model than the actor. If a `success_check` resolves the goal programmatically, that completion costs zero tokens.

### What happens when the judge is not confident the goal is on track?

Completion and escalation are separate decisions. Below `confidence_threshold`, the mission escalates — a webhook plus an EventBus event via the shared `escalate()` path — and a human can intervene, rather than the agent limping forward or falsely completing. The threshold governs escalation; completion is gated on the judge's `achieved` verdict.

## Next steps

Give your agent a mission with an `objective` and `success_criteria`, then let it self-evaluate and complete when the judge is confident — start from the runnable `MissionTracker` above, then declare the same `MissionConfig` on a supervised process. Read the [Mission-Oriented Process Model reference](../../runtime/governance/mission.md) for the full evaluation cycle and `MissionEvaluation` fields, the [Agent Runtime process model](../../runtime/processes.md) for how missions ride on a persistent process, and the [journal reference](../../runtime/journal/index.md) for how a mission's "done" decision survives a crash and replays cleanly.
