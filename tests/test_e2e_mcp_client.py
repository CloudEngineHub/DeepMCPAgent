"""End-to-end tests for the Promptise MCP Client subsystem.

Covers MCPClient, MCPMultiClient, MCPToolAdapter, and ToolInfo —
exercising construction, auth header injection, transport
auto-detection, schema conversion, and tool discovery with mocked
MCP transports.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from pydantic import BaseModel

from promptise.mcp.client import MCPClient, MCPMultiClient, MCPToolAdapter
from promptise.tools import ToolInfo

# =====================================================================
# TestMCPClient — 6 tests
# =====================================================================


class TestMCPClient:
    """Unit tests for MCPClient construction, fields, and auth headers."""

    def test_client_creation_with_url(self):
        """MCPClient(url=...) stores the URL and defaults to http transport."""
        client = MCPClient(url="http://localhost:9000/mcp")
        assert client._url == "http://localhost:9000/mcp"
        assert client._transport == "http"
        assert client._session is None

    def test_client_fields_set_correctly(self):
        """All constructor fields should be stored correctly."""
        client = MCPClient(
            url="http://example.com/mcp",
            transport="sse",
            headers={"x-trace-id": "abc"},
            timeout=60.0,
        )
        assert client._url == "http://example.com/mcp"
        assert client._transport == "sse"
        assert client._headers["x-trace-id"] == "abc"
        assert client._timeout == 60.0
        assert client.session is None

    def test_transport_auto_detection_defaults_to_http(self):
        """Default transport is 'http'; 'sse' must be explicitly set."""
        http_client = MCPClient(url="http://localhost:8080/mcp")
        assert http_client._transport == "http"

        sse_client = MCPClient(url="http://localhost:8080/sse", transport="sse")
        assert sse_client._transport == "sse"

        stdio_client = MCPClient(transport="stdio", command="python", args=["-m", "server"])
        assert stdio_client._transport == "stdio"

    def test_bearer_token_creates_auth_header(self):
        """bearer_token should inject an Authorization: Bearer header."""
        client = MCPClient(
            url="http://localhost:8080/mcp",
            bearer_token="eyJhbGciOi.test.token",
        )
        assert client._headers["authorization"] == "Bearer eyJhbGciOi.test.token"
        assert client.headers["authorization"] == "Bearer eyJhbGciOi.test.token"

    def test_api_key_creates_x_api_key_header(self):
        """api_key should inject an x-api-key header."""
        client = MCPClient(
            url="http://localhost:8080/mcp",
            api_key="sk-secret-key-123",
        )
        assert client._headers["x-api-key"] == "sk-secret-key-123"
        assert "authorization" not in client._headers

    def test_custom_headers_merged_with_auth(self):
        """Custom headers, bearer_token, and api_key should all coexist."""
        client = MCPClient(
            url="http://localhost:8080/mcp",
            headers={"x-request-id": "req-42", "x-org": "acme"},
            bearer_token="jwt-tok",
            api_key="api-key-val",
        )
        assert client._headers["authorization"] == "Bearer jwt-tok"
        assert client._headers["x-api-key"] == "api-key-val"
        assert client._headers["x-request-id"] == "req-42"
        assert client._headers["x-org"] == "acme"
        # headers property returns a copy
        h = client.headers
        h["tampered"] = "yes"
        assert "tampered" not in client._headers


# =====================================================================
# TestMCPMultiClient — 4 tests
# =====================================================================


class TestMCPMultiClient:
    """Unit tests for MCPMultiClient construction and lifecycle."""

    def test_multi_client_creation_with_server_specs(self):
        """MCPMultiClient accepts a dict of name -> MCPClient."""
        c1 = MCPClient(url="http://server-a/mcp")
        c2 = MCPClient(url="http://server-b/mcp", api_key="key-b")
        multi = MCPMultiClient({"svc_a": c1, "svc_b": c2})
        assert len(multi.servers) == 2
        assert "svc_a" in multi.servers
        assert "svc_b" in multi.servers

    def test_fields_initialized_correctly(self):
        """Internal state should be clean before connection."""
        c1 = MCPClient(url="http://a")
        multi = MCPMultiClient({"alpha": c1})
        assert multi._connected is False
        assert multi._tool_to_server == {}
        assert multi.tool_to_server == {}

    def test_server_name_tracking(self):
        """Server names should be retrievable via the servers property."""
        clients = {
            "finance": MCPClient(url="http://finance/mcp"),
            "hr": MCPClient(url="http://hr/mcp"),
            "docs": MCPClient(url="http://docs/mcp"),
        }
        multi = MCPMultiClient(clients)
        names = set(multi.servers.keys())
        assert names == {"finance", "hr", "docs"}

    async def test_disconnect_all_safe_when_not_connected(self):
        """Calling __aexit__ when never connected should not raise."""
        c1 = MCPClient(url="http://localhost/mcp")
        multi = MCPMultiClient({"x": c1})
        # Should not raise even though we never called __aenter__
        await multi.__aexit__(None, None, None)
        assert multi._connected is False


# =====================================================================
# TestMCPToolAdapter — 6 tests
# =====================================================================


class TestMCPToolAdapter:
    """Tests for MCPToolAdapter — MCP tool to LangChain BaseTool conversion."""

    def _make_mcp_tool(self, name: str, description: str, schema: dict) -> MagicMock:
        """Create a mock MCP Tool object."""
        tool = MagicMock()
        tool.name = name
        tool.description = description
        tool.inputSchema = schema
        return tool

    async def test_adapt_simple_tool_preserves_name_and_description(self):
        """Adapted tool should preserve the original name and description."""
        from langchain_core.tools import BaseTool

        tool = self._make_mcp_tool(
            "search_docs",
            "Search documentation",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        )

        multi = MagicMock(spec=MCPMultiClient)
        multi.list_tools = AsyncMock(return_value=[tool])
        multi.tool_to_server = {"search_docs": "docs-server"}

        adapter = MCPToolAdapter(multi)
        lc_tools = await adapter.as_langchain_tools()

        assert len(lc_tools) == 1
        assert lc_tools[0].name == "search_docs"
        assert lc_tools[0].description == "Search documentation"
        assert isinstance(lc_tools[0], BaseTool)

    async def test_adapt_tool_with_parameters_schema_correct(self):
        """Adapted tool should have an args_schema with correct fields."""
        tool = self._make_mcp_tool(
            "create_user",
            "Create a new user",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "User name"},
                    "age": {"type": "integer", "description": "Age in years"},
                    "active": {"type": "boolean", "default": True},
                },
                "required": ["name", "age"],
            },
        )

        multi = MagicMock(spec=MCPMultiClient)
        multi.list_tools = AsyncMock(return_value=[tool])
        multi.tool_to_server = {"create_user": "user-svc"}

        adapter = MCPToolAdapter(multi)
        lc_tools = await adapter.as_langchain_tools()
        schema_cls = lc_tools[0].args_schema

        assert "name" in schema_cls.model_fields
        assert "age" in schema_cls.model_fields
        assert "active" in schema_cls.model_fields
        assert schema_cls.model_fields["name"].is_required()
        assert schema_cls.model_fields["age"].is_required()
        assert schema_cls.model_fields["active"].is_required() is False

    async def test_nested_pydantic_model_schema_handling(self):
        """Nested object schemas should produce Pydantic sub-models."""
        tool = self._make_mcp_tool(
            "register_employee",
            "Register employee",
            {
                "type": "object",
                "properties": {
                    "employee": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "address": {
                                "type": "object",
                                "properties": {
                                    "city": {"type": "string"},
                                    "zip": {"type": "string"},
                                },
                                "required": ["city"],
                            },
                        },
                        "required": ["name", "address"],
                    },
                },
                "required": ["employee"],
            },
        )

        multi = MagicMock(spec=MCPMultiClient)
        multi.list_tools = AsyncMock(return_value=[tool])
        multi.tool_to_server = {"register_employee": "hr"}

        adapter = MCPToolAdapter(multi)
        lc_tools = await adapter.as_langchain_tools()
        schema_cls = lc_tools[0].args_schema

        # employee field should be a Pydantic model
        emp_ann = schema_cls.model_fields["employee"].annotation
        assert issubclass(emp_ann, BaseModel)
        # address inside employee should also be a model
        addr_ann = emp_ann.model_fields["address"].annotation
        assert issubclass(addr_ann, BaseModel)
        assert "city" in addr_ann.model_fields

    async def test_ref_defs_resolution_in_schemas(self):
        """$ref/$defs in input schemas should be resolved correctly."""
        tool = self._make_mcp_tool(
            "update_record",
            "Update a record",
            {
                "$defs": {
                    "Location": {
                        "type": "object",
                        "properties": {
                            "lat": {"type": "number"},
                            "lon": {"type": "number"},
                        },
                        "required": ["lat", "lon"],
                    },
                },
                "type": "object",
                "properties": {
                    "location": {"$ref": "#/$defs/Location"},
                },
                "required": ["location"],
            },
        )

        multi = MagicMock(spec=MCPMultiClient)
        multi.list_tools = AsyncMock(return_value=[tool])
        multi.tool_to_server = {"update_record": "geo-svc"}

        adapter = MCPToolAdapter(multi)
        lc_tools = await adapter.as_langchain_tools()
        schema_cls = lc_tools[0].args_schema

        loc_ann = schema_cls.model_fields["location"].annotation
        assert issubclass(loc_ann, BaseModel)
        assert "lat" in loc_ann.model_fields
        assert "lon" in loc_ann.model_fields

    async def test_tool_adapter_creates_langchain_base_tool(self):
        """Each adapted tool should be an instance of LangChain BaseTool."""
        from langchain_core.tools import BaseTool

        tools = [
            self._make_mcp_tool(
                "t1",
                "Tool one",
                {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
            ),
            self._make_mcp_tool(
                "t2",
                "Tool two",
                {
                    "type": "object",
                    "properties": {"y": {"type": "integer"}},
                },
            ),
        ]

        multi = MagicMock(spec=MCPMultiClient)
        multi.list_tools = AsyncMock(return_value=tools)
        multi.tool_to_server = {"t1": "s1", "t2": "s2"}

        adapter = MCPToolAdapter(multi)
        lc_tools = await adapter.as_langchain_tools()

        assert len(lc_tools) == 2
        for t in lc_tools:
            assert isinstance(t, BaseTool)

    async def test_adapted_tool_has_correct_args_schema(self):
        """args_schema on adapted tools should be a Pydantic BaseModel subclass."""
        tool = self._make_mcp_tool(
            "query",
            "Run a query",
            {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL query"},
                    "limit": {"type": "integer", "default": 100},
                },
                "required": ["sql"],
            },
        )

        multi = MagicMock(spec=MCPMultiClient)
        multi.list_tools = AsyncMock(return_value=[tool])
        multi.tool_to_server = {"query": "db-svc"}

        adapter = MCPToolAdapter(multi)
        lc_tools = await adapter.as_langchain_tools()
        schema_cls = lc_tools[0].args_schema

        assert issubclass(schema_cls, BaseModel)
        assert "sql" in schema_cls.model_fields
        assert schema_cls.model_fields["sql"].description == "SQL query"
        assert schema_cls.model_fields["sql"].is_required()
        assert schema_cls.model_fields["limit"].default == 100


# =====================================================================
# TestMCPToolAdapterWithMultiClient — 3 tests
# =====================================================================


class TestMCPToolAdapterWithMultiClient:
    """Tests for the MCPToolAdapter that wraps MCPMultiClient."""

    def test_tool_info_fields(self):
        """ToolInfo dataclass should hold name, description, server_name."""
        info = ToolInfo(
            server_guess="finance-server",
            name="calculate_tax",
            description="Calculate tax for an amount",
            input_schema={"type": "object", "properties": {"amount": {"type": "number"}}},
        )
        assert info.name == "calculate_tax"
        assert info.description == "Calculate tax for an amount"
        assert info.server_guess == "finance-server"
        assert "amount" in info.input_schema["properties"]

    async def test_get_tool_info_returns_correct_info(self):
        """MCPToolAdapter.list_tool_info should return ToolInfo for each tool."""
        tool = MagicMock()
        tool.name = "search"
        tool.description = "Search records"
        tool.inputSchema = {"type": "object", "properties": {"q": {"type": "string"}}}

        multi = MagicMock(spec=MCPMultiClient)
        multi.list_tools = AsyncMock(return_value=[tool])
        multi.tool_to_server = {"search": "main-server"}

        adapter = MCPToolAdapter(multi)
        infos = await adapter.list_tool_info()

        assert len(infos) == 1
        assert infos[0].name == "search"
        assert infos[0].description == "Search records"
        assert infos[0].server_guess == "main-server"

    async def test_list_tools_returns_all_discovered_tools(self):
        """MCPToolAdapter.as_langchain_tools should return all tools from all servers."""
        tools = [MagicMock() for _ in range(3)]
        for i, t in enumerate(tools):
            t.name = f"tool_{i}"
            t.description = f"Tool number {i}"
            t.inputSchema = {
                "type": "object",
                "properties": {"param": {"type": "string"}},
                "required": ["param"],
            }

        multi = MagicMock(spec=MCPMultiClient)
        multi.list_tools = AsyncMock(return_value=tools)
        multi.tool_to_server = {f"tool_{i}": f"server_{i}" for i in range(3)}

        adapter = MCPToolAdapter(multi)
        lc_tools = await adapter.as_langchain_tools()

        assert len(lc_tools) == 3
        names = {t.name for t in lc_tools}
        assert names == {"tool_0", "tool_1", "tool_2"}
