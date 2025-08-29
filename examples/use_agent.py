"""
Example: Using DeepMCP with a custom model over HTTP.

Now with fancy console output:
- Shows discovered tools
- Shows each tool call + result
- Shows final LLM answer
"""

import asyncio
from dotenv import load_dotenv
from deepmcpagent import HTTPServerSpec, build_deep_agent
from langchain_openai import ChatOpenAI
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


async def main() -> None:
    console = Console()
    load_dotenv()

    servers = {
        "math": HTTPServerSpec(
            url="http://127.0.0.1:8000/mcp",
            transport="http",
        ),
    }

    model = ChatOpenAI(model="gpt-4.1")

    graph, loader = await build_deep_agent(
        servers=servers,
        model=model,
        instructions="You are a helpful agent. Use MCP math tools to solve problems."
    )

    # Show discovered tools
    infos = await loader.list_tool_info()
    table = Table(title="Discovered MCP Tools", show_lines=True)
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Description", style="green")
    for t in infos:
        table.add_row(t.name, t.description or "-")
    console.print(table)

    # Run a query
    query = "What is (3 + 5) * 7 using math tools?"
    console.print(Panel.fit(query, title="User Query", style="bold magenta"))

    result = await graph.ainvoke({
        "messages": [{"role": "user", "content": query}]
    })

    # Iterate messages for tool calls and outputs
    console.print("\n[bold yellow]Agent Trace:[/bold yellow]")
    for msg in result["messages"]:
        role = msg.__class__.__name__
        if role == "AIMessage" and msg.tool_calls:
            for call in msg.tool_calls:
                console.print(f"[cyan]→ Invoking tool:[/cyan] [bold]{call['name']}[/bold] with {call['args']}")
        elif role == "ToolMessage":
            console.print(f"[green]✔ Tool result from {msg.name}:[/green] {msg.content}")
        elif role == "AIMessage" and msg.content:
            console.print(Panel(msg.content, title="Final LLM Answer", style="bold green"))


if __name__ == "__main__":
    asyncio.run(main())
