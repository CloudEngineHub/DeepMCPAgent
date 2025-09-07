import pytest  # noqa: I001


pytest.skip(
    "Integration test requires a live MCP server and model credentials.",
    allow_module_level=True,
)
