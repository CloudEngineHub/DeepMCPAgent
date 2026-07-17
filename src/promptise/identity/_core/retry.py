"""Bounded retry-with-jittered-backoff for transient credential acquisition.

Cloud metadata/token services briefly fail under normal operation: Azure IMDS
and the GCP metadata server return 429/5xx or time out right after instance
start or under load, and AWS STS throttles. A single-shot fetch surfaces that
blip as a hard :class:`CredentialAcquisitionError` — and in ``build_agent`` that
is swallowed to a warning, silently degrading a *verifiable* agent to
**unauthenticated**, which is the opposite of what an operator asked for by
configuring identity.

These helpers retry only genuinely transient conditions (connection/timeout and
429/5xx). They never retry an authentication/authorization failure (4xx): a
missing role or wrong audience fails fast with its precise, actionable error.

Dependency-free by design (a small loop + ``random`` jitter) — no ``tenacity``.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

T = TypeVar("T")

#: HTTP statuses worth retrying — throttling plus transient server/proxy errors.
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: Default attempts: one try plus two retries.
DEFAULT_ATTEMPTS: int = 3


def _backoff(attempt: int, *, base: float = 0.25, cap: float = 2.0) -> float:
    """Exponential backoff with full jitter for retry ``attempt`` (0-indexed)."""
    ceiling = min(cap, base * (2.0**attempt))
    return random.uniform(0.0, ceiling)  # noqa: S311  # nosec B311 - jitter, not crypto


def http_get_with_retry(
    url: str,
    *,
    params: dict[str, str],
    headers: dict[str, str],
    timeout: float,
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """``httpx.get`` that retries transport errors and 429/5xx responses.

    Returns the final :class:`httpx.Response` (which the caller still inspects
    for a non-retryable non-200, e.g. a 403, and converts to a fatal error).
    Re-raises the last transport error if every attempt fails.
    """
    last_exc: httpx.HTTPError | None = None
    for attempt in range(attempts):
        try:
            response = httpx.get(url, params=params, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:  # timeout, connect, transport
            last_exc = exc
            if attempt + 1 >= attempts:
                raise
            sleep(_backoff(attempt))
            continue
        if response.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
            sleep(_backoff(attempt))
            continue
        return response
    assert last_exc is not None  # nosec B101  # pragma: no cover - unreachable
    raise last_exc  # pragma: no cover


def retry_call(
    fn: Callable[[], T],
    *,
    is_transient: Callable[[BaseException], bool],
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn``; retry up to ``attempts`` times on a *transient* exception.

    ``is_transient`` decides whether a raised exception is worth retrying. A
    non-transient exception (e.g. an auth failure) propagates immediately.
    """
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below when fatal/exhausted
            last_exc = exc
            if attempt + 1 >= attempts or not is_transient(exc):
                raise
            sleep(_backoff(attempt))
    assert last_exc is not None  # nosec B101  # pragma: no cover - unreachable
    raise last_exc  # pragma: no cover


def aws_is_transient(exc: BaseException) -> bool:
    """Whether a boto3/botocore error from STS is worth retrying.

    Retries throttling and 5xx service errors and connection/timeout errors;
    never an ``AccessDenied``-class 4xx. Detects by class name + the ClientError
    response dict so botocore need not be imported here.
    """
    name = type(exc).__name__
    if name in {
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "EndpointConnectionError",
        "ConnectionClosedError",
        "ConnectionError",
    }:
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "")
        if code in {
            "Throttling",
            "ThrottlingException",
            "ThrottledException",
            "TooManyRequestsException",
            "RequestLimitExceeded",
            "ServiceUnavailable",
            "ServiceUnavailableException",
            "InternalError",
            "InternalFailure",
            "InternalServerError",
        }:
            return True
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if isinstance(status, int) and status in RETRYABLE_STATUS:
            return True
    return False
