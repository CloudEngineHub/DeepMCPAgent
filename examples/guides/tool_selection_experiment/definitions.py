"""How big are the tool definitions the model receives, at each optimization level?

An offline estimate with tiktoken, which langchain-openai brings; no model is called.

    python definitions.py
"""

import asyncio
import json
import sys

import tiktoken
from langchain_core.utils.function_calling import convert_to_openai_tool

from promptise import build_agent
from promptise.config import StdioServerSpec

SERVERS = {"platform": StdioServerSpec(command=sys.executable, args=["platform_server.py"])}
enc = tiktoken.get_encoding("o200k_base")


def definition_tokens(tools) -> int:
    payload = [convert_to_openai_tool(t) for t in tools]
    return len(enc.encode(json.dumps(payload)))


async def main():
    for level in [None, "minimal", "standard", "semantic"]:
        agent = await build_agent(model="openai:gpt-5-mini", servers=SERVERS, optimize_tools=level)
        try:
            tools = agent.tools
            suspend = next(t for t in tools if t.name == "suspend_user")
            print(
                f"--- optimize_tools={level!r}: {len(tools)} tools, {definition_tokens(tools)} tokens"
            )
            print(json.dumps(convert_to_openai_tool(suspend)["function"]))
        finally:
            await agent.shutdown()


asyncio.run(main())
