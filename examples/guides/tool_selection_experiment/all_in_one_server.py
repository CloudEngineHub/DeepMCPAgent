"""Platform and customer tools in one server: 122 + 26 = 148 tools, past OpenAI's 128-tool limit.

    python experiment.py --server all_in_one_server.py --settings "all tools" minimal standard --reps 1 --tasks 1 --out runs148.jsonl
"""
from promptise.mcp.server import MCPServer

import customer_server
import platform_server
from acme_tools import add_tools

server = MCPServer("acme-all")
add_tools(server, platform_server.RESOURCES, platform_server.ACTIONS)
add_tools(server, customer_server.RESOURCES, customer_server.ACTIONS)

if __name__ == "__main__":
    server.run()
