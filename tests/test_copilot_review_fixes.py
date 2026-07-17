"""Regression tests for the Copilot review findings on PR #43.

Each test fails against the pre-fix code (mutation-verified) and passes
against the fix:

  F1  ``ObservabilityCollector.record()`` stored ``delegated_by`` by
      reference to the shared delegation contextvar dict, so every entry
      recorded during one delegation aliased the same dict — a later
      mutation of one entry's metadata retroactively altered its siblings.
  F3  Runtime ``dict`` -> :class:`HTTPServerSpec` normalization silently
      dropped the ``auth`` field.
  F4  Runtime ``dict`` -> :class:`StdioServerSpec` normalization silently
      dropped ``cwd`` and ``keep_alive``.

F2 is a docstring-only fix (``cache._scoped_user_id``) with no runtime
behavior to assert; it is covered by the ``mkdocs --strict`` build and the
existing cache injectivity tests.

A post-review sweep found the same bug *classes* in files Copilot did not
review; those fixes are regression-tested at the bottom of this module:

  S1  ``StdioServerSpec.cwd`` never reached the launched subprocess — the
      native ``MCPClient`` had no ``cwd`` parameter, so a configured working
      directory was silently dropped (completes F4 end-to-end).
  S2  The ``.agent`` manifest loader dropped ``keep_alive`` from stdio
      servers (same drop as F4, a different loader).
  S3  ``AgentContext.put`` / initial-state aliased the mutable value into the
      timestamped audit history (same class as F1), so an in-place mutation
      retroactively rewrote past audit records.
"""

from __future__ import annotations

from promptise.config import HTTPServerSpec, StdioServerSpec
from promptise.observability import (
    ObservabilityCollector,
    TimelineEventType,
    _delegation_ctx_var,
)
from promptise.runtime.process import _resolve_server_specs

# ---------------------------------------------------------------------------
# F3 / F4 — dict server specs must carry through every supported field so a
# dict-based config behaves identically to passing the spec object directly.
# ---------------------------------------------------------------------------


def test_http_dict_spec_carries_auth_and_all_fields() -> None:
    resolved = _resolve_server_specs(
        {
            "api": {
                "type": "http",
                "url": "http://host/mcp",
                "headers": {"X-Env": "prod"},
                "auth": "legacy-hint",
                "bearer_token": "tok",
                "api_key": "k",
                "audience": "api://data",
            }
        }
    )
    spec = resolved["api"]
    assert isinstance(spec, HTTPServerSpec)
    assert spec.url == "http://host/mcp"
    assert spec.transport == "http"
    assert spec.headers == {"X-Env": "prod"}
    assert spec.auth == "legacy-hint"  # F3: previously dropped
    assert spec.bearer_token is not None
    assert spec.api_key is not None
    assert spec.audience == "api://data"


def test_stdio_dict_spec_carries_cwd_and_keep_alive() -> None:
    resolved = _resolve_server_specs(
        {
            "tools": {
                "command": "python",
                "args": ["srv.py"],
                "env": {"A": "1"},
                "cwd": "/srv/app",
                "keep_alive": False,
            }
        }
    )
    spec = resolved["tools"]
    assert isinstance(spec, StdioServerSpec)
    assert spec.command == "python"
    assert spec.args == ["srv.py"]
    assert spec.env == {"A": "1"}
    assert spec.cwd == "/srv/app"  # F4: previously dropped
    assert spec.keep_alive is False  # F4: previously dropped


def test_stdio_dict_spec_preserves_defaults_when_omitted() -> None:
    spec = _resolve_server_specs({"t": {"command": "python"}})["t"]
    assert isinstance(spec, StdioServerSpec)
    assert spec.cwd is None
    assert spec.keep_alive is True


def test_http_dict_spec_omits_absent_optional_fields() -> None:
    spec = _resolve_server_specs({"h": {"url": "http://x/mcp"}})["h"]
    assert isinstance(spec, HTTPServerSpec)
    assert spec.auth is None
    assert spec.bearer_token is None
    assert spec.api_key is None


def test_typed_specs_pass_through_unchanged() -> None:
    http = HTTPServerSpec(url="http://x/mcp")
    stdio = StdioServerSpec(command="python")
    resolved = _resolve_server_specs({"h": http, "s": stdio})
    assert resolved["h"] is http
    assert resolved["s"] is stdio


def test_unrecognized_dict_spec_is_skipped() -> None:
    resolved = _resolve_server_specs({"bad": {"nonsense": 1}})
    assert "bad" not in resolved


def test_dict_spec_transport_alias_via_type() -> None:
    spec = _resolve_server_specs({"s": {"type": "sse", "url": "http://x/sse"}})["s"]
    assert isinstance(spec, HTTPServerSpec)
    assert spec.transport == "sse"


# ---------------------------------------------------------------------------
# F1 — ``delegated_by`` must be an independent snapshot, not a live reference
# to the shared delegation contextvar dict.
# ---------------------------------------------------------------------------


def test_delegated_by_is_snapshotted_not_aliased() -> None:
    collector = ObservabilityCollector()
    claims = {"agent_id": "orchestrator", "verifiable": True}
    token = _delegation_ctx_var.set(claims)
    try:
        e1 = collector.record(TimelineEventType.TOOL_CALL, agent_id="peer")
        e2 = collector.record(TimelineEventType.TOOL_CALL, agent_id="peer")
    finally:
        _delegation_ctx_var.reset(token)

    expected = {"agent_id": "orchestrator", "verifiable": True}
    assert e1.metadata["delegated_by"] == expected
    assert e2.metadata["delegated_by"] == expected

    # Each entry holds its OWN copy — not the shared source, not each other.
    assert e1.metadata["delegated_by"] is not claims
    assert e1.metadata["delegated_by"] is not e2.metadata["delegated_by"]

    # Mutating the source, or one entry's copy, must not bleed into siblings.
    claims["agent_id"] = "TAMPERED"
    e1.metadata["delegated_by"]["agent_id"] = "ALSO_TAMPERED"
    assert e2.metadata["delegated_by"]["agent_id"] == "orchestrator"


def test_no_delegated_by_stamp_outside_delegation() -> None:
    collector = ObservabilityCollector()
    # No ambient delegation in scope for this call.
    assert _delegation_ctx_var.get() is None
    entry = collector.record(TimelineEventType.TOOL_CALL, agent_id="solo")
    assert "delegated_by" not in entry.metadata


# ---------------------------------------------------------------------------
# S1 — cwd must be plumbed end-to-end: StdioServerSpec.cwd -> MCPClient ->
# the MCP SDK's StdioServerParameters (the actual subprocess launch).
# ---------------------------------------------------------------------------


def test_mcpclient_forwards_cwd_to_stdio_params() -> None:
    from promptise.mcp.client import MCPClient

    client = MCPClient(
        transport="stdio",
        command="python",
        args=["s.py"],
        env={"A": "1"},
        cwd="/srv/app",
    )
    assert client._cwd == "/srv/app"
    params = client._stdio_params()
    assert params.cwd == "/srv/app"  # reaches the SDK launch params
    assert str(params.command) == "python"


def test_mcpclient_cwd_defaults_to_none() -> None:
    from promptise.mcp.client import MCPClient

    client = MCPClient(transport="stdio", command="python")
    assert client._cwd is None
    assert client._stdio_params().cwd is None


# ---------------------------------------------------------------------------
# S2 — a .agent manifest stdio server must honor keep_alive (and cwd).
# ---------------------------------------------------------------------------


def test_manifest_stdio_server_carries_keep_alive_and_cwd() -> None:
    from promptise.runtime.manifest import (
        AgentManifestSchema,
        manifest_to_process_config,
    )

    manifest = AgentManifestSchema.model_validate(
        {
            "name": "t",
            "model": "openai:gpt-5-mini",
            "servers": {
                "tools": {
                    "command": "python",
                    "args": ["s.py"],
                    "cwd": "/w",
                    "keep_alive": False,
                }
            },
        }
    )
    spec = manifest_to_process_config(manifest).servers["tools"]
    assert isinstance(spec, StdioServerSpec)
    assert spec.keep_alive is False  # previously dropped -> defaulted to True
    assert spec.cwd == "/w"


# ---------------------------------------------------------------------------
# S3 — AgentContext's timestamped audit history must be an immutable
# snapshot, not a live alias of the mutable blackboard value.
# ---------------------------------------------------------------------------


def test_agent_context_history_is_snapshot_not_alias() -> None:
    import asyncio

    from promptise.runtime.context import AgentContext

    async def scenario():
        ctx = AgentContext(initial_state={"seed": [1]})
        ctx.put("plan", ["a"])
        ctx.get("plan").append("MUTATED")  # mutate the live blackboard value
        ctx.get("seed").append("MUTATED2")  # mutate an initial-state value
        return (
            ctx._history["plan"][-1].value,
            ctx._history["seed"][-1].value,
            ctx.get("plan"),
        )

    hist_plan, hist_seed, live = asyncio.run(scenario())
    assert hist_plan == ["a"]  # put() history snapshot intact
    assert hist_seed == [1]  # initial_state history snapshot intact
    assert live == ["a", "MUTATED"]  # live blackboard remains mutable
