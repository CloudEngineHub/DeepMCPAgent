"""Pydantic models returned by tools and resources are sent as JSON."""

from __future__ import annotations

import json
from datetime import date

from pydantic import BaseModel

from promptise.mcp.server import MCPServer, TestClient


class Book(BaseModel):
    title: str
    published: date


DUNE = Book(title="Dune", published=date(1965, 8, 1))
DUNE_JSON = {"title": "Dune", "published": "1965-08-01"}


def _server() -> MCPServer:
    server = MCPServer("books")

    @server.tool()
    async def get_book() -> Book:
        return DUNE

    @server.tool()
    async def list_books() -> list[Book]:
        return [DUNE, DUNE]

    @server.tool()
    async def shelf() -> dict:
        return {"top": DUNE, "count": 1}

    @server.resource("books://all")
    async def all_books() -> list[Book]:
        return [DUNE]

    @server.resource("books://dune")
    async def dune() -> Book:
        return DUNE

    return server


async def test_tool_returning_a_model_sends_its_json() -> None:
    result = await TestClient(_server()).call_tool("get_book", {})
    assert json.loads(result[0].text) == DUNE_JSON


async def test_tool_returning_a_list_of_models() -> None:
    result = await TestClient(_server()).call_tool("list_books", {})
    assert json.loads(result[0].text) == [DUNE_JSON, DUNE_JSON]


async def test_models_inside_a_dict() -> None:
    result = await TestClient(_server()).call_tool("shelf", {})
    assert json.loads(result[0].text) == {"top": DUNE_JSON, "count": 1}


async def test_resources_and_tools_agree() -> None:
    client = TestClient(_server())
    assert json.loads(await client.read_resource("books://dune")) == DUNE_JSON
    assert json.loads(await client.read_resource("books://all")) == [DUNE_JSON]
