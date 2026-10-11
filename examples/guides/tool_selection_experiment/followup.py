"""Semantic selection in a conversation: the tools offered for a direct request and for "Yes, go ahead."

Asks for a rollback directly, then has the same job confirmed as a follow-up to an assistant
reply, and prints the tools the first model call of each turn was offered.

    python followup.py
"""

import asyncio
import sys

from langchain_core.callbacks import AsyncCallbackHandler

from promptise import build_agent
from promptise.config import StdioServerSpec


class FirstCallTools(AsyncCallbackHandler):
    """Remembers the tools offered to the first model call of a run."""

    def __init__(self):
        self.tools = None

    async def on_chat_model_start(self, serialized, messages, **kwargs):
        if self.tools is None:
            tools = (kwargs.get("invocation_params") or {}).get("tools") or []
            self.tools = [t["function"]["name"] for t in tools]


def called_tools(result) -> list[str]:
    return [c["name"] for m in result["messages"] for c in (getattr(m, "tool_calls", None) or [])]


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"platform": StdioServerSpec(command=sys.executable, args=["platform_server.py"])},
        optimize_tools="semantic",  # each request gets only the tools that fit it
    )
    try:
        direct = FirstCallTools()
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": "The last deploy of prj_web_shop broke checkout. "
                                                      "Put production back the way it was."}]},
            config={"callbacks": [direct]},
        )

        history = [{"role": "user", "content": "The last deploy of prj_web_shop broke checkout. "
                                               "What can we do? Don't change anything yet."}]
        first = await agent.ainvoke({"messages": history})
        history.append({"role": "assistant", "content": first["messages"][-1].content})
        history.append({"role": "user", "content": "Yes, go ahead."})
        follow = FirstCallTools()
        second = await agent.ainvoke({"messages": history}, config={"callbacks": [follow]})

        print("One message:", direct.tools)
        print("Follow-up:  ", follow.tools)
        print("first reply:", " ".join(first["messages"][-1].content.split())[:300])
        print("after 'Yes, go ahead.' it called:", called_tools(second))
        print("and answered:", " ".join(second["messages"][-1].content.split())[:300])
    finally:
        await agent.shutdown()


asyncio.run(main())
