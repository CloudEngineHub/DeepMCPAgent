---
title: "Set a Confidence Threshold to Stop or Escalate an Agent"
description: "Auto-stopping the instant a model claims success is how agents quit early or run forever; shows gating completion and escalation on the judge's confidence …"
keywords: "confidence threshold to stop ai agent, agent auto-complete on success, escalate low confidence agent, mission confidence threshold, when to auto stop an agent, stop agent early or run forever"
date: 2026-07-16
slug: confidence-threshold-to-stop-ai-agent
categories:
  - Governance
---

# Set a Confidence Threshold to Stop or Escalate an Agent

A useful confidence threshold to stop an AI agent is only trustworthy when the confidence comes from an *independent judge*, not from the agent's own claim that it finished. That distinction is the whole game. The moment you let an agent stop itself the instant it says "done," you have built a rubber stamp: the model declares victory, the loop halts, and nobody checks whether the mission was actually accomplished. Flip the failure around and it is just as bad — an agent with no completion signal at all runs on its trigger forever, re-doing finished work and burning tokens. This post shows the middle path Promptise Foundry makes first-class: a mission whose completion and escalation are both **gated on a separate evaluator's numeric confidence** — `auto_complete` on a confident success, `escalate` below your threshold, and a hard `timeout_hours` backstop so a shaky agent gets a human, not a rubber stamp.

The thesis in one line: don't stop when the *agent* says it's done — stop when an *independent judge* is confident it's done, and page a human when that judge is not.

## Quit early or run forever: two ways auto-stop fails

Autonomous agents fail completion in two opposite directions, and both come from the same root cause — treating "done" as a boolean the actor emits.

**Quit early (the rubber stamp).** Most agent loops terminate the moment the model produces a final-answer marker or a termination keyword. The model is a confident narrator of its own success: it will happily announce "all disputes reconciled" while three are still open, because from inside the transcript the job *looks* finished. Stop on that self-report and you ship a half-done mission with a green checkmark on it. This is the "stop agent early" half of the problem.

**Run forever.** The over-correction is to give the agent no self-stopping authority at all and let a trigger keep firing. Now the agent never decides it is finished; it wakes every five minutes, re-checks a queue that emptied an hour ago, and spends real tokens confirming there is nothing to do. That is the "run forever" half.

Neither a boolean self-report nor an uncapped loop can tell the difference between *confidently finished*, *confidently off-track*, and *not sure yet*. Those are three different states, and the only thing that separates them is a **calibrated confidence signal from something other than the agent under test.** That signal is exactly what a mission confidence threshold gives you.

## What a confidence threshold actually decides

Promptise's [Mission Model](../../runtime/governance/mission.md) turns a task-runner into a mission-driven process. You declare an `objective` and `success_criteria`, and every `eval_every` invocations the runtime runs an **LLM-as-judge** — a *separate* `eval_model`, not the actor — over a `MissionEvidence` bundle (recent conversation, accumulated state, tool-call log, trigger). The judge returns a structured `MissionEvaluation`: `achieved` (bool), `confidence` (0.0–1.0), `reasoning`, and a `progress_summary`.

Those two numbers drive a three-way decision on every evaluation:

| Judge says | Decision | What happens |
|------------|----------|--------------|
| `achieved=True` and `auto_complete=True` | **Stop** | Process transitions `RUNNING → STOPPING → STOPPED`. The mission is genuinely complete. |
| `confidence < confidence_threshold` (default `0.7`) | **Escalate** | `escalate()` fires — a webhook POST plus an EventBus event — and the mission suspends for a human. |
| Otherwise | **Keep going** | The agent's own context is updated with the latest confidence and summary, and it runs again. |

This is the difference between agent auto-complete on success and a rubber stamp: `auto_complete` does not fire because the *agent* claimed success — it fires because an independent judge assessed the evidence and marked the objective `achieved`. And when that judge is *unsure* the mission is on track, the run does not silently limp forward; it escalates a low-confidence agent to a human before more damage is done.

Underneath the judge sits a non-negotiable backstop: a wall-clock `timeout_hours` deadline. Before every invocation the runtime checks it, and the moment the clock passes it the mission is failed and no further invocations run — a hard floor that holds even if the judge keeps returning "not sure yet." A companion `max_invocations` ceiling lives in the same `MissionConfig` for bounding a run by count. Between the judge (semantic drift) and the timeout (wall-clock cost), a mission cannot quietly run forever.

## What other frameworks do today

To be fair and precise: every serious framework ships real termination controls, and each is useful within its scope. But look at *what signal* they stop on.

- **LangChain** — `AgentExecutor` takes `max_iterations` (default 15) and `max_execution_time`, with `early_stopping_method` deciding how the run ends when the cap is hit. These are step/time caps, plus termination when the model emits its final-answer marker. There is no numeric-confidence gate.
- **LangGraph** — a `recursion_limit` (default 25) that raises `GraphRecursionError`, and conditional edges you author to route to `END`. You *can* build a confidence gate here — a judge node that parses a score and a conditional edge that branches on it — but you write the judge, the parsing, the threshold, and the escalation branch yourself. The primitives are there; the *control* is not.
- **AutoGen** — termination conditions like `TextMentionTermination` (stop when a designated string such as "TERMINATE" appears), `MaxMessageTermination`, and token-usage termination end a team chat. `TextMentionTermination` is precisely the boolean self-report failure mode — the loop halts because the model *said* a word, with no independent confidence assessment.
- **CrewAI** — an `Agent`'s `max_iter` caps reasoning iterations and `max_rpm` throttles request rate; tasks can carry a `guardrail` callable that validates output. Completion is "the task returned"; there is no built-in judge that scores mission confidence and escalates below a threshold.
- **Pydantic AI** — `UsageLimits` (`request_limit`, `total_tokens_limit`) raise `UsageLimitExceeded` on volume mid-run. A quantity limit, not a completion judgment.

Here is the exact delta. Every one of these stops on either a **count/usage cap that raises an exception** or a **boolean signal the model emits about itself**. None of them expose, as a first-class declared control, the decision *"if an independent judge's confidence that the mission is on track drops below 0.7, page a human; if it is confidently achieved, stop the process; otherwise keep going."* You can assemble that by hand in the more composable ones — wire the judge, parse the confidence, compare it to a threshold, and fan out the escalation — and in the keyword-termination ones you get a self-report stop, which is the very thing that quits early. Promptise's edge is not that these frameworks "can't stop." It is that Promptise makes the confidence-gated stop/escalate decision a **structural property of the mission** — `confidence_threshold`, `auto_complete`, and `escalation` are fields you set, not a supervising loop you build and remember to wire into every agent.

## Runnable: gate completion on the judge's confidence

The completion gate is real code you can exercise without an API key. The `MissionTracker` accepts a programmatic `success_check` — a `Callable[[MissionEvidence], bool | None]` — that runs *before* any LLM judge. Return `True` for an objectively-met criterion and the tracker records a confident (`confidence=1.0`) success and completes the mission; the exact same completion path `auto_complete` drives in production, minus the model call. This makes the auto-complete-on-success behavior fully deterministic and testable:

```python
import asyncio

from promptise.runtime import MissionConfig, MissionEvidence
from promptise.runtime.mission import MissionTracker, MissionState


def disputes_cleared(evidence: MissionEvidence) -> bool | None:
    # Deterministic, programmatic success check — no LLM, no API key.
    remaining = evidence.state.get("open_disputes", 1)
    return remaining == 0


async def main() -> None:
    tracker = MissionTracker(
        config=MissionConfig(
            enabled=True,
            objective="Reconcile every open payment dispute",
            success_criteria="Zero disputes remain in the open queue",
            eval_every=3,              # judge every 3rd invocation
            confidence_threshold=0.7,  # below this -> escalate to a human
            auto_complete=True,        # confident success -> stop the process
        ),
        process_id="dispute-agent",
        success_check=disputes_cleared,
    )

    open_disputes = 6
    for turn in range(1, 7):
        open_disputes = max(0, open_disputes - 1)  # agent clears one per turn
        tracker.increment_invocation()

        if not tracker.should_evaluate():
            print(f"turn {turn}: {open_disputes} left  ->  no evaluation due")
            continue

        evidence = MissionEvidence(state={"open_disputes": open_disputes})
        result = await tracker.evaluate(evidence, model="openai:gpt-5-mini")
        print(
            f"turn {turn}: {open_disputes} left  ->  achieved={result.achieved} "
            f"confidence={result.confidence} state={tracker.state.value}"
        )

        if tracker.state is MissionState.COMPLETED:
            print("Mission COMPLETED — auto_complete stops the process here.")
            break


asyncio.run(main())
```

Run it and the mission holds until the criterion is genuinely met, then completes on the confident success:

```
turn 1: 5 left  ->  no evaluation due
turn 2: 4 left  ->  no evaluation due
turn 3: 3 left  ->  achieved=False confidence=1.0 state=active
turn 4: 2 left  ->  no evaluation due
turn 5: 1 left  ->  no evaluation due
turn 6: 0 left  ->  achieved=True confidence=1.0 state=completed
Mission COMPLETED — auto_complete stops the process here.
```

Notice the cadence: evaluation only runs when `should_evaluate()` says it is due (every `eval_every` invocations), and the mission does not complete at turn 3 just because the agent has been busy — it completes at turn 6 because the objective is *actually* satisfied. Swap the `success_check` for `None` and the same `evaluate()` call falls through to the LLM judge, which is where the `confidence < confidence_threshold` escalation branch lives.

## Wire the full envelope into a supervised process

In production you rarely touch `MissionTracker` directly. You declare a `MissionConfig` on a `ProcessConfig` and let the [Agent Runtime](../../runtime/index.md) drive it around every invocation of a supervised `AgentProcess`. Here the confidence gate, the auto-complete, the escalation target, and the wall-clock backstop all come together:

```python
from promptise.runtime import ProcessConfig, MissionConfig, EscalationTarget

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Reconcile open payment disputes until the queue is clear.",
    mission=MissionConfig(
        enabled=True,
        objective="Reconcile every open payment dispute",
        success_criteria="Zero disputes remain in the open queue",
        eval_model="openai:gpt-5-mini",  # the JUDGE — separate from the actor
        eval_every=3,                    # LLM-as-judge every 3 invocations
        confidence_threshold=0.7,        # below this -> escalate() to a human
        auto_complete=True,              # confident success -> STOP the process
        timeout_hours=8,                 # hard wall-clock backstop
        max_invocations=50,              # companion invocation ceiling
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/services/XXX",
            event_type="agent.mission.low_confidence",
        ),
    ),
)
```

That single block is the entire envelope. `auto_complete=True` is your stop-on-confident-success switch; `confidence_threshold=0.7` is the line below which a shaky run escalates instead of muddling on; `timeout_hours` is the deadline the mission cannot outlive. Because the escalation shares the same `escalate()` path as the runtime's [Behavioral Health](../../runtime/governance/health.md) and budget subsystems, one webhook integration covers low-confidence missions, stuck loops, and runaway cost alike. And because completion and escalation act on the *process* — not on your prompt — the decision survives across trigger-driven invocations, the deeper reason a runtime beats an in-call check, unpacked in [How to Stop a Runaway AI Agent (Runtime Kill Switches)](stop-a-runaway-ai-agent.md).

Mission is the trajectory-level trip wire; it pairs naturally with the behavior-level one. A mission judge catches an agent that has quietly drifted off-goal even while every individual step looks fine; the health monitor catches an agent [stuck repeating the same tool call](ai-agent-stuck-repeating-tool-call.md) regardless of whether it is on-mission. Run both and you cover both failure surfaces.

## Frequently asked questions

### What is a good confidence threshold to stop an AI agent?

`0.7` is the built-in default and a sensible starting point: the judge must be at least 70% confident before a *below-threshold* escalation fires. Raise it toward `0.85` for high-stakes missions where you would rather over-escalate to a human than let a marginal run continue; lower it for exploratory work where interruptions are costly. The threshold governs *escalation*, not completion — completion is gated on the judge's `achieved` verdict together with `auto_complete`.

### Isn't stopping when the model says it's done the same thing?

No, and that difference is the whole point. A keyword or final-answer termination stops because the *actor* emitted a signal about its own work — a rubber stamp. Promptise runs a *separate* `eval_model` as the judge over an evidence bundle, and completion fires on the judge's `achieved` verdict. The actor cannot vote itself finished.

### When is a plain boolean "done" actually enough?

When a human reads every result and there is no autonomous loop across invocations — a request-response chatbot, or a single-shot handler behind an endpoint — a mission judge is honest overkill. Reach for `confidence_threshold` and `auto_complete` the moment an agent runs unattended on a trigger and must decide *for itself* when it is finished. That is exactly when to auto-stop an agent on a judged signal rather than a self-report.

### What happens between evaluations, and can the agent see its own confidence?

Evaluation runs every `eval_every` invocations, so most turns carry no judge overhead. Between evaluations the agent's system prompt is injected with a mission summary — objective, status, invocation progress, and the last confidence and progress summary — so the actor is aware of how the judge scored it and can course-correct before the next evaluation.

### Does the confidence gate require extra LLM calls?

The escalation gate uses one LLM-as-judge call per evaluation (every `eval_every` invocations), and you can point it at a cheaper `eval_model` than the actor. The programmatic `success_check` path shown in the runnable example uses *zero* model calls — if your success criterion is objectively checkable, the mission completes for free.

## Next steps

Set `confidence_threshold` and `auto_complete` on your `MissionConfig` so confident wins stop the agent and shaky runs escalate to a human — start from the runnable tracker above, then move the full envelope onto a supervised process. Read the [Mission Model reference](../../runtime/governance/mission.md) for the evaluation cycle and `MissionEvaluation` fields, the [Agent Runtime overview](../../runtime/index.md) for how processes and triggers fit together, and the [Behavioral Health reference](../../runtime/governance/health.md) to pair the trajectory judge with a behavior-level trip wire.
