"""Before you pay for a single model call: would semantic selection hand the model the right tool?

Builds the same index the agent builds (same tools, same truncated descriptions,
same embedding model) and checks each task's expected tool against the top 8.
No model is called. BAAI/bge-small-en-v1.5 is downloaded from Hugging Face on first use.

    python retrieval_check.py
"""
import asyncio
import sys

from promptise import OptimizationLevel, ToolOptimizationConfig, build_agent
from promptise.config import StdioServerSpec
from promptise.tool_optimization import ToolIndex

from tasks import TASKS

SERVERS = {"platform": StdioServerSpec(command=sys.executable, args=["platform_server.py"])}

SETUPS = {
    "semantic preset": (ToolOptimizationConfig(level=OptimizationLevel.SEMANTIC), "all-MiniLM-L6-v2"),
    "full descriptions": (ToolOptimizationConfig(level=OptimizationLevel.SEMANTIC, max_description_length=400),
                          "all-MiniLM-L6-v2"),
    "full descriptions, bge-small": (ToolOptimizationConfig(level=OptimizationLevel.SEMANTIC, max_description_length=400),
                                     "BAAI/bge-small-en-v1.5"),
}


async def main():
    for label, (config, embedding_model) in SETUPS.items():
        agent = await build_agent(model="openai:gpt-5-mini", servers=SERVERS, optimize_tools=config)
        try:
            tools = [t for t in agent.tools if t.name != "request_more_tools"]
            index = ToolIndex(tools, model_name_or_path=embedding_model)
            misses = []
            for text, expected in TASKS:
                names = [t.name for t in index.select(text, top_k=8)]
                if expected not in names:
                    misses.append(f"{expected} (got {', '.join(names[:3])}, ...)")
            print(f"{label}: right tool in the top 8 for {len(TASKS) - len(misses)}/{len(TASKS)} tasks")
            for miss in misses:
                print("  missed", miss)
        finally:
            await agent.shutdown()


asyncio.run(main())
