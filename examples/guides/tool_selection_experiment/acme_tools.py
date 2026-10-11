"""Generate many echo tools on an MCP server from tables of resources and actions.

Every tool only echoes its arguments back. That is all the experiment needs: it
records which tool the model picked, not what the tool did.
"""

import inspect
from typing import Annotated

from pydantic import Field


def make_tool(server, name: str, description: str, params: dict[str, str]):
    """Register one tool. Parameters whose description starts with "Optional" may be left out."""

    async def handler(**kwargs):
        return {"ok": True, "tool": name, "arguments": kwargs}

    parameters, annotations = [], {}
    for p, d in params.items():
        optional = d.startswith("Optional")
        annotation = Annotated[str | None if optional else str, Field(description=d)]
        parameters.append(inspect.Parameter(p, inspect.Parameter.KEYWORD_ONLY, annotation=annotation,
                                            default=None if optional else inspect.Parameter.empty))
        annotations[p] = annotation
    handler.__signature__ = inspect.Signature(parameters)
    handler.__annotations__ = annotations
    handler.__name__ = name
    server.tool(name=name, description=description)(handler)


def add_tools(server, resources: dict, actions: dict):
    """Five standard tools per resource, plus one tool per action."""
    for res, (plural, example, what, fields) in resources.items():
        noun = res.replace("_", " ")
        id_param = {f"{res}_id": f'The {noun} ID, for example "{example}".'}
        make_tool(server, f"list_{res}s",
                  f"List {plural} in the organization, newest first. A {noun} is {what}. "
                  f"Returns IDs you can pass to get_{res}.",
                  {"filter": "Optional text to filter by.", "limit": "Optional maximum number of results."})
        make_tool(server, f"get_{res}", f"Get one {noun} by ID, with all its fields.", id_param)
        make_tool(server, f"create_{res}", f"Create a {noun}.", dict(fields))
        make_tool(server, f"update_{res}", f"Change fields of an existing {noun}.",
                  {**id_param, **{k: "Optional " + v for k, v in fields.items()}})
        make_tool(server, f"delete_{res}", f"Delete a {noun} permanently. This cannot be undone.", id_param)
    for name, (description, params) in actions.items():
        make_tool(server, name, description, params)
