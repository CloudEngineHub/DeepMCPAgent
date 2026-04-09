"""Tests for schema dereferencing, JSON pre-parsing, and Pydantic model tools.

Covers:
- ``dereference_schema`` inlining of ``$ref`` / ``$defs``
- ``_preparse_json_strings`` for broken MCP clients
- End-to-end Pydantic model tool registration and invocation
- Nested model preservation through the validation pipeline
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, Field

from promptise.mcp.server import MCPServer, TestClient, dereference_schema
from promptise.mcp.server._validation import (
    _preparse_json_strings,
    build_input_model,
)

# =====================================================================
# Test models
# =====================================================================


class Address(BaseModel):
    """Physical address."""

    street: str = Field(description="Street name")
    city: str = Field(description="City name")
    zip_code: str = Field(default="00000", description="ZIP code")


class Employee(BaseModel):
    """Employee record."""

    name: str = Field(min_length=1, description="Full name")
    email: str
    address: Address
    skills: list[str] = Field(default_factory=list)
    is_remote: bool = False


class Tag(BaseModel):
    label: str
    color: str = "blue"


class Item(BaseModel):
    title: str
    tags: list[Tag] = Field(default_factory=list)


# =====================================================================
# dereference_schema
# =====================================================================


class TestDereferenceSchema:
    """Test ``$ref`` / ``$defs`` inlining."""

    def test_no_refs_passthrough(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        result = dereference_schema(schema)
        assert result == schema

    def test_simple_ref(self):
        schema = {
            "$defs": {
                "Addr": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                }
            },
            "type": "object",
            "properties": {
                "address": {"$ref": "#/$defs/Addr"},
            },
        }
        result = dereference_schema(schema)
        # $defs and $ref should be gone
        assert "$defs" not in result
        assert "$ref" not in result["properties"]["address"]
        # Inlined content should be present
        assert result["properties"]["address"]["type"] == "object"
        assert "city" in result["properties"]["address"]["properties"]

    def test_nested_refs(self):
        """Nested $ref → $ref should be fully resolved."""
        schema = {
            "$defs": {
                "ZipCode": {
                    "type": "string",
                    "pattern": "^\\d{5}$",
                },
                "Addr": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "zip": {"$ref": "#/$defs/ZipCode"},
                    },
                },
            },
            "type": "object",
            "properties": {
                "address": {"$ref": "#/$defs/Addr"},
            },
        }
        result = dereference_schema(schema)
        assert "$defs" not in result
        addr = result["properties"]["address"]
        assert addr["properties"]["zip"]["type"] == "string"
        assert addr["properties"]["zip"]["pattern"] == "^\\d{5}$"

    def test_allof_with_ref(self):
        """``allOf`` containing ``$ref`` should be inlined."""
        schema = {
            "$defs": {
                "Base": {"type": "object", "properties": {"id": {"type": "integer"}}},
            },
            "type": "object",
            "properties": {
                "item": {
                    "allOf": [{"$ref": "#/$defs/Base"}],
                },
            },
        }
        result = dereference_schema(schema)
        assert "$defs" not in result
        all_of = result["properties"]["item"]["allOf"]
        assert all_of[0]["type"] == "object"
        assert "id" in all_of[0]["properties"]

    def test_array_items_ref(self):
        """``items`` with ``$ref`` should be inlined."""
        schema = {
            "$defs": {
                "Tag": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                }
            },
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/Tag"},
                },
            },
        }
        result = dereference_schema(schema)
        assert "$defs" not in result
        items_schema = result["properties"]["tags"]["items"]
        assert items_schema["type"] == "object"
        assert "label" in items_schema["properties"]

    def test_multiple_refs_to_same_def(self):
        """Multiple properties referencing the same $def both get inlined."""
        schema = {
            "$defs": {
                "Addr": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                }
            },
            "type": "object",
            "properties": {
                "home": {"$ref": "#/$defs/Addr"},
                "work": {"$ref": "#/$defs/Addr"},
            },
        }
        result = dereference_schema(schema)
        assert result["properties"]["home"]["type"] == "object"
        assert result["properties"]["work"]["type"] == "object"
        assert "$ref" not in json.dumps(result)

    def test_empty_defs_stripped(self):
        schema = {
            "$defs": {},
            "type": "object",
            "properties": {"x": {"type": "integer"}},
        }
        result = dereference_schema(schema)
        assert "$defs" not in result

    def test_circular_ref_breaks_cleanly(self):
        """Circular references should not cause infinite recursion."""
        schema = {
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "child": {"$ref": "#/$defs/Node"},
                    },
                }
            },
            "type": "object",
            "properties": {
                "root": {"$ref": "#/$defs/Node"},
            },
        }
        result = dereference_schema(schema)
        # Should not hang or crash
        assert "$defs" not in result
        root = result["properties"]["root"]
        assert root["type"] == "object"
        # The circular child should be broken with {"type": "object"}
        assert root["properties"]["child"]["type"] == "object"

    def test_ref_with_sibling_description(self):
        """$ref alongside description should preserve the description."""
        schema = {
            "$defs": {
                "Addr": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
            "type": "object",
            "properties": {
                "address": {
                    "$ref": "#/$defs/Addr",
                    "description": "Home address",
                },
            },
        }
        result = dereference_schema(schema)
        addr = result["properties"]["address"]
        assert addr["description"] == "Home address"
        assert addr["type"] == "object"

    def test_real_pydantic_schema(self):
        """Integration: a real Pydantic v2 schema gets fully dereferenced."""

        async def create(employee: Employee) -> dict:
            return {}

        _, schema = build_input_model(create)
        # After dereferencing in build_input_model, no $ref or $defs
        schema_str = json.dumps(schema)
        assert "$ref" not in schema_str
        assert "$defs" not in schema_str
        # But the nested Address fields are present
        emp_props = schema["properties"]["employee"]["properties"]
        assert "name" in emp_props
        assert "address" in emp_props
        addr_props = emp_props["address"]["properties"]
        assert "street" in addr_props
        assert "city" in addr_props

    def test_deeply_nested_pydantic(self):
        """Item -> list[Tag] schema is fully dereferenced."""

        async def add_item(item: Item) -> str:
            return "ok"

        _, schema = build_input_model(add_item)
        schema_str = json.dumps(schema)
        assert "$ref" not in schema_str
        assert "$defs" not in schema_str


# =====================================================================
# JSON string pre-parsing
# =====================================================================


class TestJsonStringPreparsing:
    """Test broken-client mitigation."""

    def test_parses_dict_string(self):
        """String that should be a dict gets parsed."""
        from pydantic import create_model

        model = create_model("T", address=(Address, ...))
        model.__promptise_has_models__ = True  # type: ignore[attr-defined]

        args = {"address": '{"street": "123 Main", "city": "NYC"}'}
        result = _preparse_json_strings(args, model)
        assert isinstance(result["address"], dict)
        assert result["address"]["city"] == "NYC"

    def test_parses_list_string(self):
        """String that should be a list gets parsed."""
        from pydantic import create_model

        model = create_model("T", tags=(list[str], ...))
        model.__promptise_has_models__ = False  # type: ignore[attr-defined]

        args = {"tags": '["python", "rust"]'}
        result = _preparse_json_strings(args, model)
        assert isinstance(result["tags"], list)
        assert result["tags"] == ["python", "rust"]

    def test_leaves_real_strings_alone(self):
        """String fields that ARE strings should not be parsed."""
        from pydantic import create_model

        model = create_model("T", name=(str, ...), age=(int, ...))
        model.__promptise_has_models__ = False  # type: ignore[attr-defined]

        args = {"name": "Alice", "age": 30}
        result = _preparse_json_strings(args, model)
        assert result["name"] == "Alice"
        assert result["age"] == 30

    def test_invalid_json_string_left_alone(self):
        """Malformed JSON string should pass through to Pydantic for error."""
        from pydantic import create_model

        model = create_model("T", address=(Address, ...))
        model.__promptise_has_models__ = True  # type: ignore[attr-defined]

        args = {"address": "not valid json {"}
        result = _preparse_json_strings(args, model)
        # Should NOT crash — just leave the string for Pydantic to reject
        assert result["address"] == "not valid json {"

    def test_optional_basemodel_parsed(self):
        """Optional[BaseModel] field with JSON string gets parsed."""
        from pydantic import create_model

        model = create_model("T", address=(Optional[Address], None))
        model.__promptise_has_models__ = True  # type: ignore[attr-defined]

        args = {"address": '{"street": "1 Oak", "city": "LA"}'}
        result = _preparse_json_strings(args, model)
        assert isinstance(result["address"], dict)

    def test_missing_fields_skipped(self):
        """Fields not present in arguments are skipped."""
        from pydantic import create_model

        model = create_model("T", address=(Address, ...), name=(str, ...))
        model.__promptise_has_models__ = True  # type: ignore[attr-defined]

        args = {"name": "Bob"}
        result = _preparse_json_strings(args, model)
        assert result == {"name": "Bob"}

    def test_empty_arguments(self):
        from pydantic import create_model

        model = create_model("T", x=(str, ...))
        result = _preparse_json_strings({}, model)
        assert result == {}


# =====================================================================
# End-to-end: Pydantic model tools via TestClient
# =====================================================================


class TestPydanticModelTools:
    """Tools with Pydantic model parameters work end-to-end."""

    async def test_nested_model_tool(self):
        server = MCPServer(name="test")

        @server.tool()
        async def create_employee(employee: Employee) -> dict:
            return {
                "name": employee.name,
                "city": employee.address.city,
                "skills": employee.skills,
            }

        client = TestClient(server)
        result = await client.call_tool(
            "create_employee",
            {
                "employee": {
                    "name": "Alice",
                    "email": "alice@test.com",
                    "address": {"street": "123 Main", "city": "SF", "zip_code": "94105"},
                    "skills": ["python"],
                }
            },
        )
        data = json.loads(result[0].text)
        assert data["name"] == "Alice"
        assert data["city"] == "SF"
        assert data["skills"] == ["python"]

    async def test_nested_model_validation_error(self):
        server = MCPServer(name="test")

        @server.tool()
        async def create_employee(employee: Employee) -> dict:
            return {}

        client = TestClient(server)
        # Missing required 'address' field
        result = await client.call_tool(
            "create_employee",
            {
                "employee": {
                    "name": "Alice",
                    "email": "alice@test.com",
                }
            },
        )
        data = json.loads(result[0].text)
        assert "error" in data
        assert data["error"]["code"] == "VALIDATION_ERROR"

    async def test_doubly_nested_model(self):
        """Item with list[Tag] works end-to-end."""
        server = MCPServer(name="test")

        @server.tool()
        async def add_item(item: Item) -> dict:
            return {
                "title": item.title,
                "tag_count": len(item.tags),
                "first_tag": item.tags[0].label if item.tags else None,
            }

        client = TestClient(server)
        result = await client.call_tool(
            "add_item",
            {
                "item": {
                    "title": "My Item",
                    "tags": [
                        {"label": "urgent", "color": "red"},
                        {"label": "review"},
                    ],
                }
            },
        )
        data = json.loads(result[0].text)
        assert data["title"] == "My Item"
        assert data["tag_count"] == 2
        assert data["first_tag"] == "urgent"

    async def test_json_string_preparsing_e2e(self):
        """Broken client sending JSON string instead of object still works."""
        server = MCPServer(name="test")

        @server.tool()
        async def create_employee(employee: Employee) -> dict:
            return {"name": employee.name, "city": employee.address.city}

        client = TestClient(server)
        # Simulate a broken client: employee value is a JSON string
        result = await client.call_tool(
            "create_employee",
            {
                "employee": json.dumps(
                    {
                        "name": "Bob",
                        "email": "bob@test.com",
                        "address": {"street": "1 Oak", "city": "LA", "zip_code": "90001"},
                    }
                )
            },
        )
        data = json.loads(result[0].text)
        assert data["name"] == "Bob"
        assert data["city"] == "LA"

    async def test_schema_has_no_refs(self):
        """Registered tool's inputSchema has no $ref/$defs."""
        server = MCPServer(name="test")

        @server.tool()
        async def create_employee(employee: Employee) -> dict:
            return {}

        tdef = server._tool_registry.get("create_employee")
        schema_str = json.dumps(tdef.input_schema)
        assert "$ref" not in schema_str
        assert "$defs" not in schema_str

    async def test_flat_params_no_change(self):
        """Tools with only flat params are unaffected by dereferencing."""
        server = MCPServer(name="test")

        @server.tool()
        async def search(query: str, limit: int = 10) -> list:
            return []

        tdef = server._tool_registry.get("search")
        assert "query" in tdef.input_schema["properties"]
        assert tdef.input_schema["properties"]["query"]["type"] == "string"
        # No $defs for flat tools
        assert "$defs" not in tdef.input_schema

    async def test_model_instance_preserved_in_handler(self):
        """Handler receives actual Pydantic model instances, not dicts."""
        server = MCPServer(name="test")
        received_type = None

        @server.tool()
        async def inspect_type(employee: Employee) -> str:
            nonlocal received_type
            received_type = type(employee).__name__
            return received_type

        client = TestClient(server)
        await client.call_tool(
            "inspect_type",
            {
                "employee": {
                    "name": "Alice",
                    "email": "a@b.com",
                    "address": {"street": "1 St", "city": "X"},
                }
            },
        )
        assert received_type == "Employee"
