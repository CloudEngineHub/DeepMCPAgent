"""Single runs behind the guide's tuning notes: semantic_top_k, preserve_tools and
request_more_tools called without arguments.

    python selection_probes.py topk preserve noargs
"""

import asyncio
import json
import sys

from langchain_core.callbacks import AsyncCallbackHandler

from promptise import OptimizationLevel, ToolOptimizationConfig, build_agent
from promptise.config import StdioServerSpec

SERVERS = {"platform": StdioServerSpec(command=sys.executable, args=["platform_server.py"])}
INSTRUCTIONS = "You are an admin assistant for the Acme platform. Do what the user asks with your tools."


class Offered(AsyncCallbackHandler):
    """Records the tool definitions sent with every model call and prints what request_more_tools said."""

    def __init__(self):
        self.calls = []

    async def on_chat_model_start(self, serialized, messages, **kwargs):
        self.calls.append((kwargs.get("invocation_params") or {}).get("tools") or [])

    async def on_tool_end(self, output, **kwargs):
        if kwargs.get("name") == "request_more_tools":
            text = str(getattr(output, "content", output))
            print("  request_more_tools said:", " ".join(text.split())[:400], flush=True)


async def run(label, config, message, instructions=INSTRUCTIONS) -> Offered:
    agent = await build_agent(model="openai:gpt-5-mini", servers=SERVERS, instructions=instructions,
                              optimize_tools=config)
    try:
        seen = Offered()
        result = await agent.ainvoke({"messages": [{"role": "user", "content": message}]},
                                     config={"callbacks": [seen]})
        calls = [(c["name"], c["args"]) for m in result["messages"] for c in (getattr(m, "tool_calls", None) or [])]
        print(f"== {label}: tools per model call {[len(t) for t in seen.calls]}; called {calls}", flush=True)
        print("   answer:", " ".join(str(result["messages"][-1].content).split())[:300], flush=True)
        return seen
    finally:
        await agent.shutdown()


async def main(which):
    if "topk" in which:
        seen = await run("semantic_top_k=3",
                         ToolOptimizationConfig(level=OptimizationLevel.SEMANTIC, semantic_top_k=3),
                         "dep_9f2c is building the wrong branch. Stop it.")
        print("   first call:", [t["function"]["name"] for t in seen.calls[0]])
    if "preserve" in which:
        seen = await run("preserve_tools",
                         ToolOptimizationConfig(level=OptimizationLevel.SEMANTIC,
                                                preserve_tools={"get_organization", "list_audit_events"}),
                         "dep_9f2c is building the wrong branch. Stop it.")
        first = seen.calls[0]
        print("   first call:", [t["function"]["name"] for t in first])
        audit = next(t for t in first if t["function"]["name"] == "list_audit_events")
        print("   list_audit_events as sent:", json.dumps(audit["function"]))
    if "noargs" in which:
        await run("request_more_tools()", "semantic",
                  "Call request_more_tools with no arguments at all, three times in a row, one call per step. "
                  "Then reply with the number of tools you can see.",
                  instructions="You are a test harness. Follow the user's instructions about tool calls exactly.")


asyncio.run(main(sys.argv[1:] or ["topk", "preserve", "noargs"]))
