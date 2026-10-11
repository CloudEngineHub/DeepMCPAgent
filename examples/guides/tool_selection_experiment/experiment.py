"""Run every task against every tool optimization setting and record each model call.

One agent per setting, all on the same MCP server. Tasks run interleaved (task 1 on every
setting, then task 2, ...) so the settings share the same network conditions. Each run
appends one JSON line to the output file: the tools offered to each model call, the tools
the model called, tokens, time and the answer. summarize.py turns the file into the table.

Run from this directory:

    python experiment.py                                    # 20 tasks x 3 reps x 5 settings = 300 runs
    python experiment.py --reps 1 --tasks 1 5 --settings "all tools" semantic   # a quick look
    python summarize.py runs.jsonl

A run that reaches an iteration limit in the middle of a tool loop raises
GraphIterationLimitError (Promptise 1.3.2 and later). It is recorded with the tools the
model called up to that point and "iteration_limit" set, and it counts as a run.
"""

import argparse
import asyncio
import json
import sys
import time

from langchain_core.callbacks import AsyncCallbackHandler

from promptise import OptimizationLevel, ToolOptimizationConfig, build_agent
from promptise.config import StdioServerSpec
from promptise.engine import GraphIterationLimitError

from tasks import TASKS

MODEL = "openai:gpt-5-mini"
INSTRUCTIONS = "You are an admin assistant for the Acme platform. Do what the user asks with your tools."

SETTINGS = {
    "all tools": None,
    "minimal": "minimal",
    "standard": "standard",
    "semantic": "semantic",
    "semantic, full descriptions": ToolOptimizationConfig(
        level=OptimizationLevel.SEMANTIC, max_description_length=400
    ),
}


class ModelCalls(AsyncCallbackHandler):
    """Records the tools offered, the tools called, token usage and duration of every model call."""

    def __init__(self):
        self.calls = []

    async def on_chat_model_start(self, serialized, messages, **kwargs):
        tools = (kwargs.get("invocation_params") or {}).get("tools") or []
        self.calls.append({"tools": [t["function"]["name"] for t in tools], "start": time.perf_counter()})

    async def on_llm_end(self, response, **kwargs):
        call = self.calls[-1]
        message = response.generations[0][0].message
        usage = message.usage_metadata or {}
        call["seconds"] = time.perf_counter() - call.pop("start")
        call["called"] = [c["name"] for c in message.tool_calls]
        call["input_tokens"] = usage.get("input_tokens", 0)
        call["cached_tokens"] = (usage.get("input_token_details") or {}).get("cache_read", 0)
        call["output_tokens"] = usage.get("output_tokens", 0)


def short(text: str, limit: int = 300) -> str:
    return " ".join(str(text).split())[:limit]


async def run_task(agent, setting: str, rep: int, number: int) -> dict:
    text, expected = TASKS[number - 1]
    recorder = ModelCalls()
    started = time.perf_counter()
    row = {"setting": setting, "rep": rep, "task": number, "expected": expected}
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": text}]}, config={"callbacks": [recorder]}
        )
        row["answer"] = result["messages"][-1].content
    except GraphIterationLimitError as error:
        # The model was still calling tools when the limit stopped the run: no answer.
        row["answer"] = None
        row["iteration_limit"] = str(error)
        row["pending_tools"] = error.pending_tools
    except Exception as error:  # the run never got going, e.g. the provider refused the request
        return {"error": f"{type(error).__name__}: {error}", **row}
    called = [name for call in recorder.calls for name in call.get("called", [])]
    row.update(
        seconds=time.perf_counter() - started,
        calls=recorder.calls,
        called=called,
        right_tool_called=expected in called,
        right_tool_offered=any(expected in call["tools"] for call in recorder.calls),
    )
    return row


def show(row: dict) -> None:
    if "error" in row:
        print(json.dumps(row), flush=True)
        return
    offered = row["calls"][0]["tools"]
    print(f"[{row['setting']}] task {row['task']}, expected {row['expected']}", flush=True)
    print(f"  offered: {offered if len(offered) <= 12 else str(len(offered)) + ' tools'}", flush=True)
    print(f"  called:  {row['called']}", flush=True)
    if "iteration_limit" in row:
        print(f"  stopped: GraphIterationLimitError: {short(row['iteration_limit'])}", flush=True)
    else:
        print(f"  answer:  {short(row['answer'])}", flush=True)


async def main(args):
    servers = {"platform": StdioServerSpec(command=sys.executable, args=[args.server])}
    names = args.settings or list(SETTINGS)
    task_numbers = args.tasks or range(1, len(TASKS) + 1)
    agents = {}
    try:
        for name in names:
            agents[name] = await build_agent(
                model=MODEL, servers=servers, instructions=INSTRUCTIONS, optimize_tools=SETTINGS[name]
            )
        with open(args.out, "a") as log:
            for rep in range(args.reps):
                for number in task_numbers:
                    for name, agent in agents.items():
                        row = await run_task(agent, name, rep, number)
                        log.write(json.dumps(row) + "\n")
                        log.flush()
                        show(row)
    finally:
        for agent in agents.values():
            await agent.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reps", type=int, default=3, help="how often each task runs per setting")
    parser.add_argument("--server", default="platform_server.py", help="the MCP server to measure")
    parser.add_argument("--settings", nargs="+", choices=list(SETTINGS), help="settings to run (default: all)")
    parser.add_argument("--tasks", nargs="+", type=int, help="task numbers, 1-based (default: all)")
    parser.add_argument("--out", default="runs.jsonl", help="JSON lines file to append to")
    asyncio.run(main(parser.parse_args()))
