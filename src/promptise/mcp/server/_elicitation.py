"""Elicitation support for MCP servers.

Allows tools to request structured input from the user during
execution via the MCP elicitation protocol.

Example::

    from promptise.mcp.server import MCPServer, Elicitor, Depends

    server = MCPServer(name="deploy")

    @server.tool()
    async def deploy(
        env: str,
        elicit: Elicitor = Depends(Elicitor),
    ) -> str:
        answer = await elicit.ask(
            message=f"Deploy to {env}?",
            schema={"type": "object", "properties": {"confirm": {"type": "boolean"}}},
        )
        if not answer or not answer.get("confirm"):
            return "Cancelled"
        return f"Deployed to {env}"

Protocol versions
-----------------

On a connection that used the ``initialize`` handshake (every 1.x client,
and 2.x clients with ``mode="legacy"``), ``ask()`` sends an
``elicitation/create`` request and waits for the answer.

The 2026-07-28 protocol revision has no server-to-client requests.  There
``ask()`` ends the call with an ``InputRequiredResult`` that carries the
question; the client asks its user and sends the same ``tools/call`` again
with the answer, and the tool runs again from the start: this time
``ask()`` returns the answer.  Code before ``ask()`` therefore runs once per
round and must not have side effects that may not repeat (do them after the
answer).  Answers to earlier questions travel in the result's
``request_state``, sealed by the server (see
``MCPServer(request_state_security=...)``).
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
from collections import Counter
from typing import Any

logger = logging.getLogger("promptise.server")

# Keys of the input requests this module puts in an InputRequiredResult.
_KEY_PREFIX = "promptise.elicit."
_STATE_VERSION = 1


def _response_dict(response: Any) -> dict[str, Any] | None:
    """A client's answer to one input request as ``{"action", "content"}``."""
    if isinstance(response, dict):
        action, content = response.get("action"), response.get("content")
    else:
        action, content = getattr(response, "action", None), getattr(response, "content", None)
    if not isinstance(action, str):
        return None
    return {"action": action, "content": content if isinstance(content, dict) else None}


class InputRound:
    """The multi round-trip state of one ``tools/call`` at 2026-07-28.

    Holds the answers the client gave so far (from the request's
    ``input_responses`` and ``request_state``) and hands out a stable key
    per question, so the question asked on one round finds its answer on
    the next.
    """

    def __init__(
        self,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
        client_capabilities: Any = None,
    ) -> None:
        self.client_capabilities = client_capabilities
        self.answers: dict[str, dict[str, Any]] = self._read_state(request_state)
        for key, response in (input_responses or {}).items():
            answer = _response_dict(response)
            if key.startswith(_KEY_PREFIX) and answer is not None:
                self.answers[key] = answer
        self._asked: Counter[str] = Counter()

    @staticmethod
    def _read_state(state: str | None) -> dict[str, dict[str, Any]]:
        if not state:
            return {}
        try:
            data = json.loads(state)
            if data.get("v") != _STATE_VERSION or not isinstance(data.get("answers"), dict):
                raise ValueError("unknown request_state format")
            return {
                key: answer
                for key, raw in data["answers"].items()
                if isinstance(key, str) and (answer := _response_dict(raw)) is not None
            }
        except (ValueError, TypeError, AttributeError):
            logger.warning("Ignoring a request_state this server did not write")
            return {}

    def key(self, message: str, schema: dict[str, Any]) -> str:
        """The input-request key of this question (the n-th time it is asked)."""
        digest = hashlib.sha256(
            json.dumps([message, schema], sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        self._asked[digest] += 1
        return f"{_KEY_PREFIX}{digest}.{self._asked[digest]}"

    def state(self) -> str | None:
        """The ``request_state`` carrying the answers so far (``None``: none yet)."""
        if not self.answers:
            return None
        return json.dumps({"v": _STATE_VERSION, "answers": self.answers}, sort_keys=True)

    @property
    def can_elicit(self) -> bool:
        """Whether the request declared the elicitation capability."""
        caps = self.client_capabilities
        return caps is not None and getattr(caps, "elicitation", None) is not None


#: The InputRound of the tools/call being handled (unset: a handshake connection).
current_round: contextvars.ContextVar[InputRound | None] = contextvars.ContextVar(
    "promptise_mcp_input_round", default=None
)


class InputRequired(BaseException):  # noqa: N818 - a control-flow signal, not an error
    """Ends a 2026-07-28 tool call with an ``InputRequiredResult``.

    A ``BaseException`` so that ``except Exception`` in tool code and
    middleware (error handlers, circuit breakers, retries) does not treat
    the question as a failure.  The server's ``tools/call`` handler turns
    it into the result.
    """

    def __init__(self, key: str, message: str, schema: dict[str, Any], round_: InputRound):
        super().__init__(key)
        self.key = key
        self.message = message
        self.schema = schema
        self.round = round_

    def to_result(self) -> Any:
        """The ``InputRequiredResult`` asking the client this question."""
        from mcp import types

        # Only reached with the 2.x SDK (1.x has no 2026-07-28 rounds).
        request = types.ElicitRequest.model_validate(
            {
                "method": "elicitation/create",
                "params": {"mode": "form", "message": self.message, "requestedSchema": self.schema},
            }
        )
        result_type: Any = getattr(types, "InputRequiredResult")  # noqa: B009 - 2.x only
        return result_type(input_requests={self.key: request}, request_state=self.round.state())


class Elicitor:
    """Request structured input from the user mid-execution.

    Bound to the MCP session by the framework's DI wiring.  Sends an
    ``elicitation/create`` request through ``ServerSession.elicit`` and
    resolves the client's :class:`mcp.types.ElicitResult`.

    ``ask()`` returns ``None`` when the answer is not a usable acceptance:
    the client declined or cancelled, did not declare the elicitation
    capability, the request timed out, or the transport failed.  Every one
    of these is logged at WARNING/INFO level so an operator can tell *why*
    a gated call was denied instead of guessing, and :attr:`last_failure`
    says which it was.  On a 2026-07-28 connection, a question the client
    has not answered yet ends the call with an ``InputRequiredResult``
    instead (see the module docstring); the tool runs again with the answer.

    Args:
        timeout: Default timeout in seconds for elicitation requests.
    """

    def __init__(self, timeout: float = 60.0) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self._session: Any = None
        self._request_id: str | int | None = None
        self._timeout = timeout
        self.last_failure: str | None = None

    def _bind(self, session: Any, request_id: str | int | None = None) -> None:
        """Bind to the current MCP session (called by framework)."""
        self._session = session
        self._request_id = request_id

    async def ask(
        self,
        message: str,
        schema: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        """Ask the user for structured input.

        Args:
            message: Human-readable prompt.
            schema: JSON Schema for the expected response.  Defaults to an
                empty object schema (a bare confirmation).
            timeout: Override the default timeout, in seconds.

        Returns:
            The submitted form values when the user accepted, or ``None``
            when the elicitor is unbound, the client declined or cancelled,
            the client does not support elicitation, the request timed
            out, or the transport failed.
        """
        self.last_failure = None
        if self._session is None:
            self.last_failure = "no MCP session"
            return None

        effective_timeout = self._timeout if timeout is None else timeout
        requested_schema = schema or {"type": "object", "properties": {}}
        round_ = current_round.get()
        if round_ is not None:
            return self._answer_in_round(round_, message, requested_schema)
        from .._compat import MCP_V2

        # The schema keyword is requestedSchema in mcp 1.x, requested_schema in 2.x.
        schema_kwarg = {("requested_schema" if MCP_V2 else "requestedSchema"): requested_schema}
        try:
            # ``ServerSession.elicit`` is the SDK entry point for form-mode
            # elicitation across the supported mcp range; the contract test in
            # tests/test_approval_gate.py fails loudly if the SDK renames it.
            result = await asyncio.wait_for(
                self._session.elicit(
                    message=message,
                    related_request_id=self._request_id,
                    **schema_kwarg,
                ),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Elicitation timed out after %.0fs (request_id=%s), treated as declined",
                effective_timeout,
                self._request_id,
            )
            self.last_failure = f"no answer within {effective_timeout:.0f}s"
            return None
        except Exception as exc:
            # McpError when the client rejects the request (no elicitation
            # capability), or a transport failure. Either way there is no
            # answer — callers treat None as a decline.
            logger.warning(
                "Elicitation request failed (%s: %s), treated as declined",
                type(exc).__name__,
                exc,
            )
            self.last_failure = f"the elicitation request failed ({type(exc).__name__})"
            return None

        return self._accepted(getattr(result, "action", None), getattr(result, "content", None))

    def _accepted(self, action: Any, content: Any) -> dict[str, Any] | None:
        """The form values of an accepted answer, else ``None`` (and why)."""
        if action != "accept":
            logger.info("Elicitation not accepted by client (action=%r)", action)
            self.last_failure = f"the client answered {action!r}"
            return None
        if not isinstance(content, dict):
            logger.warning(
                "Elicitation accepted without form content (%s), treated as declined",
                type(content).__name__,
            )
            self.last_failure = "the client accepted without form content"
            return None
        return dict(content)

    def _answer_in_round(
        self, round_: InputRound, message: str, schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        """``ask()`` on a 2026-07-28 connection (see the module docstring).

        Raises:
            InputRequired: The client has not answered this question yet.
        """
        if not round_.can_elicit:
            logger.warning(
                "Elicitation is not possible: the client did not declare the elicitation "
                "capability (request_id=%s), treated as declined",
                self._request_id,
            )
            self.last_failure = "the client did not declare the elicitation capability"
            return None
        key = round_.key(message, schema)
        answer = round_.answers.get(key)
        if answer is None:
            raise InputRequired(key, message, schema, round_)
        return self._accepted(answer["action"], answer["content"])
