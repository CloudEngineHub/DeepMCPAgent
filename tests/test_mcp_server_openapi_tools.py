"""OpenAPIProvider tools: the spec's input schema, annotations and argument mapping."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from promptise.mcp.server import MCPServer, OpenAPIProvider, TestClient

SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Users", "version": "1"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/users/{id}": {
            "parameters": [{"name": "X-Trace-Id", "in": "header", "schema": {"type": "string"}}],
            "get": {
                "operationId": "getUser",
                "summary": "Get a user",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "page[size]", "in": "query", "schema": {"type": "integer"}},
                ],
            },
            "put": {
                "operationId": "putUser",
                "summary": "Replace a user",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/User"}}
                    },
                },
            },
            "delete": {
                "operationId": "deleteUser",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
            },
        },
        "/tags": {
            "post": {
                "operationId": "addTags",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"type": "array", "items": {"type": "string"}}
                        }
                    }
                },
            }
        },
    },
    "components": {
        "schemas": {
            "User": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "address": {"$ref": "#/components/schemas/Address"},
                },
            },
            "Address": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    },
}


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Answer every request the tools make and record it."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient

    class _Client(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(answer)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return seen


def _client() -> TestClient:
    server = MCPServer("users")
    OpenAPIProvider(SPEC, auth_header=("Authorization", "Bearer t")).register(server)
    return TestClient(server)


def _wire(tool: Any) -> dict[str, Any]:
    """The tool as sent on the wire (camelCase on both SDK majors)."""
    return tool.model_dump(mode="json", by_alias=True, exclude_none=True)


async def _tools() -> dict[str, Any]:
    return {t.name: _wire(t) for t in await _client().list_tools()}


async def test_input_schema_is_the_operations_parameters() -> None:
    tools = await _tools()
    schema = tools["getUser"]["inputSchema"]
    assert set(schema["properties"]) == {"X-Trace-Id", "id", "page[size]"}
    assert schema["required"] == ["id"]
    assert schema["properties"]["page[size]"]["type"] == "integer"


async def test_body_schema_is_inlined() -> None:
    schema = (await _tools())["putUser"]["inputSchema"]
    assert schema["properties"]["address"] == {
        "type": "object",
        "properties": {"city": {"type": "string"}},
    }
    assert "$ref" not in json.dumps(schema)
    assert set(schema["required"]) == {"id", "name"}


async def test_annotations_follow_the_method() -> None:
    tools = await _tools()
    get, put, delete = tools["getUser"], tools["putUser"], tools["deleteUser"]
    assert get["annotations"]["readOnlyHint"] is True
    assert get["annotations"]["title"] == "Get a user"
    assert put["annotations"]["readOnlyHint"] is False
    assert put["annotations"]["idempotentHint"] is True
    assert delete["annotations"]["destructiveHint"] is True
    assert get["annotations"]["openWorldHint"] is True


async def test_get_maps_path_query_and_header(requests: list[httpx.Request]) -> None:
    result = await _client().call_tool(
        "getUser", {"id": "a/b c", "page[size]": 5, "X-Trace-Id": "tr-1"}
    )
    assert json.loads(result[0].text) == {"ok": True}
    (req,) = requests
    assert req.method == "GET"
    assert req.url.raw_path.decode() == "/v1/users/a%2Fb%20c?page%5Bsize%5D=5"
    assert req.headers["x-trace-id"] == "tr-1"
    assert req.headers["authorization"] == "Bearer t"


async def test_arguments_not_given_are_not_sent(requests: list[httpx.Request]) -> None:
    await _client().call_tool("getUser", {"id": "1"})
    (req,) = requests
    assert req.url.query == b""
    assert "x-trace-id" not in req.headers


async def test_put_sends_body_fields_as_json(requests: list[httpx.Request]) -> None:
    await _client().call_tool("putUser", {"id": "7", "name": "Ada", "address": {"city": "Bern"}})
    (req,) = requests
    assert req.method == "PUT"
    assert req.url.path == "/v1/users/7"
    assert json.loads(req.content) == {"name": "Ada", "address": {"city": "Bern"}}


async def test_non_object_body_is_the_body_argument(requests: list[httpx.Request]) -> None:
    await _client().call_tool("addTags", {"body": ["a", "b"]})
    (req,) = requests
    assert json.loads(req.content) == ["a", "b"]


async def test_invalid_arguments_are_rejected_before_any_request(
    requests: list[httpx.Request],
) -> None:
    result = await _client().call_tool("getUser", {"id": "1", "page[size]": "many"})
    assert json.loads(result[0].text)["error"]["code"] == "VALIDATION_ERROR"
    missing = await _client().call_tool("putUser", {"id": "1"})
    assert json.loads(missing[0].text)["error"]["code"] == "VALIDATION_ERROR"
    assert requests == []


async def test_swagger2_parameters(requests: list[httpx.Request]) -> None:
    spec = {
        "swagger": "2.0",
        "host": "api.example.com",
        "basePath": "/v2",
        "paths": {
            "/pets": {
                "post": {
                    "operationId": "addPet",
                    "parameters": [
                        {"name": "limit", "in": "query", "type": "integer"},
                        {
                            "name": "pet",
                            "in": "body",
                            "required": True,
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                            },
                        },
                    ],
                }
            }
        },
    }
    server = MCPServer("pets")
    OpenAPIProvider(spec).register(server)
    client = TestClient(server)
    (tool,) = await client.list_tools()
    assert _wire(tool)["inputSchema"]["properties"]["limit"]["type"] == "integer"
    await client.call_tool("addPet", {"limit": 2, "pet": {"name": "Rex"}})
    (req,) = requests
    assert str(req.url) == "https://api.example.com/v2/pets?limit=2"
    assert json.loads(req.content) == {"name": "Rex"}


def _one_op_spec(servers: list[dict[str, str]] | None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": {"title": "Ping", "version": "1"},
        "paths": {"/ping": {"get": {"operationId": "ping", "responses": {"200": {}}}}},
    }
    if servers is not None:
        spec["servers"] = servers
    return spec


def test_private_base_url_is_refused_with_the_opt_in_named() -> None:
    provider = OpenAPIProvider(_one_op_spec([{"url": "http://127.0.0.1:8080"}]))
    with pytest.raises(ValueError, match="allow_private=True"):
        provider.parse()


def test_base_url_override_to_a_private_host_is_refused_too() -> None:
    provider = OpenAPIProvider(_one_op_spec(None), base_url="http://10.0.0.5")
    with pytest.raises(ValueError, match="allow_private=True"):
        provider.parse()


async def test_allow_private_reaches_an_internal_api(requests: list[httpx.Request]) -> None:
    server = MCPServer("internal")
    OpenAPIProvider(
        _one_op_spec([{"url": "http://127.0.0.1:8080/v1"}]), allow_private=True
    ).register(server)
    await TestClient(server).call_tool("ping", {})
    (req,) = requests
    assert str(req.url) == "http://127.0.0.1:8080/v1/ping"


def test_relative_servers_url_resolves_against_the_spec_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            200, json=_one_op_spec([{"url": "/v1"}]), request=httpx.Request("GET", url)
        ),
    )
    monkeypatch.setattr(
        "promptise.mcp.server._openapi._validate_url_not_private", lambda url, **kw: None
    )
    (op,) = OpenAPIProvider("https://api.example.com/openapi.json").parse()
    assert op["base_url"] == "https://api.example.com/v1"


def test_relative_servers_url_without_a_spec_url_fails_at_register() -> None:
    provider = OpenAPIProvider(_one_op_spec([{"url": "/v1"}]))
    with pytest.raises(ValueError, match="'/v1'.*base_url="):
        provider.register(MCPServer("x"))


def test_no_servers_entry_fails_at_register() -> None:
    provider = OpenAPIProvider(_one_op_spec(None))
    with pytest.raises(ValueError, match="no servers entry"):
        provider.register(MCPServer("x"))


def test_relative_servers_url_with_base_url_override_is_fine() -> None:
    server = MCPServer("x")
    count = OpenAPIProvider(
        _one_op_spec([{"url": "/v1"}]), base_url="https://api.example.com/v1"
    ).register(server)
    assert count == 1
