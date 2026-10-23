"""OpenAPI-to-MCP tool generator.

Automatically generates MCP tools from an OpenAPI specification.
Each endpoint in the spec becomes a tool that makes HTTP requests
to the target API.

Example::

    from promptise.mcp.server import MCPServer, OpenAPIProvider

    server = MCPServer(name="api-bridge")
    provider = OpenAPIProvider("https://petstore.swagger.io/v2/swagger.json")
    provider.register(server)
"""

from __future__ import annotations

import ipaddress
import json
import logging
from typing import Any, Optional
from urllib.parse import quote, urljoin, urlparse

logger = logging.getLogger("promptise.server")


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether *ip* is anything but a public unicast address.

    Covers private, loopback, link-local (cloud metadata at
    ``169.254.169.254``), reserved, unspecified (``0.0.0.0``), multicast and
    shared/carrier-grade NAT space (``100.64.0.0/10``, where e.g. Alibaba
    Cloud's metadata service lives), plus IPv4-mapped IPv6 forms of those.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _is_private_ip(mapped)
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


_INTERNAL_API_HINT = (
    "To call an internal API on purpose, pass allow_private=True to OpenAPIProvider."
)


def _validate_url_not_private(url: str, *, hint: str = _INTERNAL_API_HINT) -> None:
    """Reject URLs that resolve to private/internal IP ranges (SSRF protection).

    Args:
        url: The URL to check.
        hint: Appended to the error: how the caller can reach an internal
            host on purpose.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Invalid URL: {url!r}")

    # Block well-known internal hostnames
    if hostname in ("localhost", "metadata.google.internal"):
        raise ValueError(f"URL targets a private/internal host: {hostname!r}. {hint}")

    # Resolve hostname and check IP ranges
    import socket

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
    except socket.gaierror:
        return  # Cannot resolve — let httpx handle the error

    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_private_ip(ip):
            raise ValueError(f"URL {url!r} resolves to private/internal IP {ip}. {hint}")


def _is_absolute_http(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


class OpenAPIProvider:
    """Generate MCP tools from an OpenAPI specification.

    Parses an OpenAPI 3.x (or Swagger 2.x) spec and registers one
    MCP tool per operation.  Each tool makes an HTTP request to the
    target API when called.

    Args:
        spec: Either a URL to fetch the spec from, a file path, or
            a pre-parsed dict.
        base_url: Override the server base URL from the spec. Required
            when the spec has no ``servers`` entry, or a relative one and
            the spec was not loaded from a URL.
        prefix: Prefix for generated tool names (e.g. ``"petstore_"``).
        include: Only include operations matching these operation IDs.
        exclude: Exclude operations matching these operation IDs.
        auth_header: Default auth header for requests (e.g.
            ``("Authorization", "Bearer tok-123")``).
        tags: Tags to apply to all generated tools.
        allow_private: Allow the spec URL and the API base URL to be on a
            loopback, private or link-local address. Off by default, so a
            spec cannot point the generated tools at internal services (SSRF
            protection). Turn it on only for an internal API you trust.
    """

    def __init__(
        self,
        spec: str | dict[str, Any],
        *,
        base_url: str | None = None,
        prefix: str = "",
        include: set[str] | None = None,
        exclude: set[str] | None = None,
        auth_header: tuple[str, str] | None = None,
        tags: list[str] | None = None,
        allow_private: bool = False,
    ) -> None:
        self._raw_spec = spec
        self._allow_private = allow_private
        self._base_url = base_url
        self._prefix = prefix
        self._include = include
        self._exclude = exclude or set()
        self._auth_header = auth_header
        self._tags = tags or []
        self._spec: dict[str, Any] | None = None
        self._operations: list[dict[str, Any]] = []

    def _load_spec(self) -> dict[str, Any]:
        """Load and parse the OpenAPI spec."""
        if isinstance(self._raw_spec, dict):
            return self._raw_spec

        raw = self._raw_spec
        # If it looks like a URL, try fetching it
        if raw.startswith("http://") or raw.startswith("https://"):
            try:
                import httpx

                if not self._allow_private:
                    _validate_url_not_private(raw)
                resp = httpx.get(raw, timeout=30)
                resp.raise_for_status()
                return resp.json()
            except ImportError:
                raise ImportError(
                    "httpx is required to fetch OpenAPI specs from URLs. "
                    "Install with: pip install httpx"
                )

        # Try as file path
        import pathlib

        path = pathlib.Path(raw)
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if path.suffix in (".yaml", ".yml"):
                try:
                    import yaml

                    return yaml.safe_load(text)
                except ImportError:
                    raise ImportError(
                        "PyYAML is required to parse YAML specs. Install with: pip install pyyaml"
                    )
            return json.loads(text)

        # Try as raw JSON string
        return json.loads(raw)

    def _extract_operations(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract operations from the spec."""
        ops: list[dict[str, Any]] = []
        paths = spec.get("paths", {})

        # Determine base URL
        base = self._base_url
        if base is None:
            servers = spec.get("servers", [])
            if servers:
                base = servers[0].get("url", "")
            elif "host" in spec:
                # Swagger 2.x
                scheme = (spec.get("schemes") or ["https"])[0]
                base_path = spec.get("basePath", "")
                base = f"{scheme}://{spec['host']}{base_path}"

        # A relative servers URL ("/v1") is relative to where the spec came from
        if base and not _is_absolute_http(base):
            spec_url = self._raw_spec if isinstance(self._raw_spec, str) else ""
            if _is_absolute_http(spec_url):
                base = urljoin(spec_url, base)

        # SSRF protection: validate base_url doesn't target private networks
        if base and _is_absolute_http(base) and not self._allow_private:
            _validate_url_not_private(base)

        for path, methods in paths.items():
            if "$ref" in methods:
                methods = {**self._resolve_ref(methods["$ref"], spec), **methods}
            # Path-level parameters apply to every operation on the path;
            # an operation's own parameter with the same name and location
            # replaces it.
            path_params = [
                self._resolve_ref(p["$ref"], spec) if "$ref" in p else p
                for p in methods.get("parameters", []) or []
            ]
            for method, operation in methods.items():
                if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                    continue

                op_id = operation.get("operationId", f"{method}_{path}")
                op_id = op_id.replace("-", "_").replace("/", "_").strip("_")

                # Filter
                if self._include and op_id not in self._include:
                    continue
                if op_id in self._exclude:
                    continue

                params: dict[tuple[str, str], dict[str, Any]] = {}
                for param in [*path_params, *(operation.get("parameters", []) or [])]:
                    if "$ref" in param:
                        param = self._resolve_ref(param["$ref"], spec)
                    params[(param.get("name", ""), param.get("in", "query"))] = param
                parameters = list(params.values())

                # Build input schema from parameters and request body
                input_schema, locations = self._build_input_schema(
                    {**operation, "parameters": parameters}, spec
                )

                ops.append(
                    {
                        "operation_id": op_id,
                        "method": method.upper(),
                        "path": path,
                        "base_url": base or "",
                        "summary": operation.get("summary", ""),
                        "description": operation.get("description", ""),
                        "input_schema": input_schema,
                        "parameters": parameters,
                        "locations": locations,
                    }
                )

        return ops

    def _build_input_schema(
        self, operation: dict[str, Any], spec: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Build a JSON Schema for the operation's inputs.

        Returns the schema (every ``$ref`` inlined) and where each argument
        goes in the request: ``"path"``, ``"query"``, ``"header"``,
        ``"cookie"``, ``"body"`` (one property of a JSON object body) or
        ``"body:whole"`` (the whole body, as the ``body`` argument).
        """
        properties: dict[str, Any] = {}
        required: list[str] = []
        locations: dict[str, str] = {}

        # Path + query + header + cookie params
        for param in operation.get("parameters", []):
            if "$ref" in param:
                param = self._resolve_ref(param["$ref"], spec)
            name = param.get("name", "")
            location = param.get("in", "query")
            if not name or name in properties:
                continue
            if location == "body":
                # Swagger 2.x body parameter: the whole JSON body
                schema = self._deref(param.get("schema", {}), spec)
                properties[name] = {**schema, "description": param.get("description", name)}
                locations[name] = "body:whole"
            else:
                # Swagger 2.x puts the type on the parameter itself
                schema = param.get("schema") or {
                    k: param[k] for k in ("type", "format", "items", "enum") if k in param
                }
                schema = self._deref(schema or {"type": "string"}, spec)
                properties[name] = {**schema, "description": param.get("description", name)}
                locations[name] = location
            if param.get("required", False) or location == "path":
                required.append(name)

        # Request body
        body = operation.get("requestBody", {})
        if "$ref" in body:
            body = self._resolve_ref(body["$ref"], spec)
        content = body.get("content", {})
        json_content = content.get("application/json", {})
        body_schema = self._deref(json_content.get("schema", {}), spec)

        if body_schema.get("properties"):
            for k, v in body_schema["properties"].items():
                if k in properties:
                    logger.debug(
                        "OpenAPI: body field %r clashes with a parameter of the same name; "
                        "the parameter is kept",
                        k,
                    )
                    continue
                properties[k] = v
                locations[k] = "body"
            if body.get("required", False):
                required.extend(r for r in body_schema.get("required", []) if r in locations)
        elif body_schema and "body" not in properties:
            properties["body"] = body_schema
            locations["body"] = "body:whole"
            if body.get("required", False):
                required.append("body")

        result: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            result["required"] = list(dict.fromkeys(required))
        return result, locations

    def _resolve_ref(self, ref: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Resolve a $ref pointer."""
        parts = ref.lstrip("#/").split("/")
        node: Any = spec
        for part in parts:
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return dict(node) if isinstance(node, dict) else {}

    def _deref(self, node: Any, spec: dict[str, Any], _seen: frozenset[str] = frozenset()) -> Any:
        """Inline every local ``$ref`` in *node*, so clients get a self-contained schema."""
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                ref = node["$ref"]
                if ref in _seen:
                    return {"type": "object"}  # recursive schema: stop here
                resolved = self._deref(self._resolve_ref(ref, spec), spec, _seen | {ref})
                siblings = {k: self._deref(v, spec, _seen) for k, v in node.items() if k != "$ref"}
                return {**resolved, **siblings}
            return {k: self._deref(v, spec, _seen) for k, v in node.items()}
        if isinstance(node, list):
            return [self._deref(item, spec, _seen) for item in node]
        return node

    def parse(self) -> list[dict[str, Any]]:
        """Parse the spec and return operations (does not register tools).

        Returns:
            List of operation dicts with ``operation_id``, ``method``,
            ``path``, ``summary``, and ``input_schema``.
        """
        self._spec = self._load_spec()
        self._operations = self._extract_operations(self._spec)
        return self._operations

    def register(self, server: Any) -> int:
        """Parse the spec and register tools on the server.

        Returns:
            Number of tools registered.
        """
        if not self._operations:
            self.parse()

        # Fail here, not on every call: a tool without an absolute base URL
        # can never reach the API.
        for op in self._operations:
            if not _is_absolute_http(op["base_url"]):
                found = repr(op["base_url"]) if op["base_url"] else "no servers entry"
                raise ValueError(
                    f"OpenAPI spec has {found} for the API address, so the tools "
                    "cannot know where to send requests. Pass "
                    "base_url='https://api.example.com' to OpenAPIProvider."
                )

        count = 0
        for op in self._operations:
            self._register_tool(server, op)
            count += 1

        logger.info(
            "OpenAPI: registered %d tools from spec (prefix=%r)",
            count,
            self._prefix,
        )
        return count

    def _register_tool(self, server: Any, op: dict[str, Any]) -> None:
        """Register a single operation as an MCP tool.

        The tool's input schema is the operation's parameters and JSON body
        from the spec; arguments are validated against it and sent where the
        spec puts them (path, query, header, cookie or body).
        """
        from dataclasses import replace

        from ._decorators import build_tool_def
        from ._types import ToolAnnotations

        tool_name = f"{self._prefix}{op['operation_id']}"
        description = op.get("summary") or op.get("description") or tool_name
        method = op["method"]
        path = op["path"]
        base_url = op["base_url"]
        auth_header = self._auth_header
        locations: dict[str, str] = op.get("locations", {})
        input_schema: dict[str, Any] = op["input_schema"]

        annotations = ToolAnnotations(
            title=description[:50] if len(description) > 50 else description,
            read_only_hint=method in ("GET", "HEAD"),
            destructive_hint=method == "DELETE",
            idempotent_hint=method in ("GET", "HEAD", "PUT", "DELETE"),
            open_world_hint=True,
        )

        async def handler(**arguments: Any) -> Any:
            try:
                import httpx
            except ImportError:
                raise ImportError(
                    "httpx is required for OpenAPI tools. Install with: pip install httpx"
                )

            url_path = path
            query: dict[str, Any] = {}
            headers: dict[str, str] = {}
            cookies: dict[str, str] = {}
            json_body: Any = None
            body_fields: dict[str, Any] = {}
            for key, value in arguments.items():
                location = locations.get(key, "query" if method in _NO_BODY else "body")
                if value is None and location in ("path", "query", "header", "cookie"):
                    continue  # an explicit null: send nothing
                if location == "path":
                    url_path = url_path.replace(f"{{{key}}}", quote(_as_text(value), safe=""))
                elif location == "query":
                    query[key] = value
                elif location == "header":
                    headers[key] = _as_text(value)
                elif location == "cookie":
                    cookies[key] = _as_text(value)
                elif location == "body:whole":
                    json_body = value
                else:
                    body_fields[key] = value
            if body_fields:
                json_body = (
                    {**json_body, **body_fields} if isinstance(json_body, dict) else body_fields
                )
            if auth_header:
                headers[auth_header[0]] = auth_header[1]

            async with httpx.AsyncClient(timeout=30, cookies=cookies or None) as client:
                resp = await client.request(
                    method,
                    f"{base_url}{url_path}",
                    params=query or None,
                    headers=headers,
                    json=json_body,
                )
                try:
                    return resp.json()
                except Exception:
                    return {"status": resp.status_code, "body": resp.text}

        handler.__name__ = tool_name
        handler.__doc__ = description

        tool_def = replace(
            build_tool_def(
                handler,
                name=tool_name,
                description=description,
                tags=list(self._tags),
                auth=bool(getattr(server, "_require_auth", False)),
                annotations=annotations,
            ),
            input_schema=input_schema,
        )
        server._tool_registry.register(tool_def)
        server._input_models[tool_name] = _arguments_model(tool_name, input_schema)


_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
_NO_BODY = frozenset({"GET", "HEAD", "DELETE", "OPTIONS", "TRACE"})


def _as_text(value: Any) -> str:
    """A path, header or cookie value as text (JSON booleans, not ``True``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _arguments_model(tool_name: str, schema: dict[str, Any]) -> type[Any]:
    """A Pydantic model that validates a call against the operation's schema.

    Property names come from the spec and need not be Python identifiers
    (``X-Request-Id``, ``page[size]``), so each field gets a safe name and
    the spec name as its alias.  Validated arguments are passed on as JSON
    under the spec names, with only the arguments the caller gave (the API
    applies its own defaults).
    """
    from pydantic import BaseModel, ConfigDict, Field, create_model

    from promptise.tools import _schema_to_annotation

    properties: dict[str, Any] = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    fields: dict[str, Any] = {}
    for index, (prop_name, prop_schema) in enumerate(properties.items()):
        prop_schema = prop_schema or {}
        annotation: Any = _schema_to_annotation(prop_schema, {}, f"{tool_name}_arg{index}")
        description = prop_schema.get("description")
        if prop_name in required:
            field = Field(..., alias=prop_name, description=description)
        else:
            field = Field(default=None, alias=prop_name, description=description)
            annotation = Optional[annotation]
        fields[f"arg_{index}"] = (annotation, field)

    def _dump(instance: BaseModel) -> dict[str, Any]:
        return instance.model_dump(mode="json", by_alias=True, exclude_unset=True)

    model = create_model(  # type: ignore[call-overload]
        f"{tool_name}_Arguments",
        __config__=ConfigDict(populate_by_name=False),
        **fields,
    )
    model.__promptise_has_models__ = False
    model.__promptise_dump_arguments__ = staticmethod(_dump)
    return model  # type: ignore[no-any-return]
