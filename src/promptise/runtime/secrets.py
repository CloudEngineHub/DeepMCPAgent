"""Agent-native secret scoping for the runtime.

Provides per-process secrets with TTL, rotation, access logging,
and zero-retention revocation.  Secret values live **only** in memory
and are never serialised to journal, checkpoint, or status output.

Example::

    from promptise.runtime.secrets import SecretScope
    from promptise.runtime.config import SecretScopeConfig

    scope = SecretScope(
        config=SecretScopeConfig(
            secrets={"api_key": "${MY_API_KEY}"},
            default_ttl=3600,
        ),
        process_id="proc-1",
    )
    await scope.resolve_initial()
    token = scope.get("api_key")   # access-logged
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet

if TYPE_CHECKING:
    from .config import SecretScopeConfig
    from .journal import JournalProvider

logger = logging.getLogger("promptise.runtime.secrets")

_ENV_PATTERN = re.compile(r"^\$\{(\w+)\}$")


@dataclass
class SecretEntry:
    """A single scoped secret.

    Attributes:
        name: Logical secret name.
        value: The secret value (in-memory only — **never** serialised).
        expires_at: Monotonic timestamp when this entry expires, or
            ``None`` for no expiry.
        source: How this secret was provisioned (``"env"`` or
            ``"rotation"``).
    """

    name: str
    value: bytes  # Fernet-encrypted — never plaintext in memory
    expires_at: float | None = None
    source: str = "env"


class SecretScope:
    """Per-process secret vault with TTL, access logging, and rotation.

    Args:
        config: Secret scoping configuration.
        process_id: Owning process ID (for journal entries).
        journal: Optional journal provider for access logging.
    """

    def __init__(
        self,
        config: SecretScopeConfig,
        process_id: str,
        journal: JournalProvider | None = None,
    ) -> None:
        self._config = config
        self._process_id = process_id
        self._journal = journal
        self._secrets: dict[str, SecretEntry] = {}
        self._access_count: int = 0
        self._fernet = Fernet(Fernet.generate_key())

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active_secret_count(self) -> int:
        """Number of non-expired secrets currently stored."""
        return sum(1 for entry in self._secrets.values() if not self._is_expired(entry))

    @property
    def active_secret_names(self) -> list[str]:
        """Names of non-expired secrets currently stored."""
        return [name for name, entry in self._secrets.items() if not self._is_expired(entry)]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve_initial(self) -> None:
        """Resolve all secrets from config on startup.

        Each value in ``config.secrets`` is either a literal string or
        an env var reference like ``${VAR_NAME}``.  References are
        resolved from ``os.environ``.

        Raises:
            KeyError: If an env var reference cannot be resolved.
        """
        for name, template in self._config.secrets.items():
            value = self._resolve_env(template)
            ttl = self._config.ttls.get(name, self._config.default_ttl)
            expires_at = (time.monotonic() + ttl) if ttl is not None else None
            self._secrets[name] = SecretEntry(
                name=name,
                value=self._fernet.encrypt(value.encode()),
                expires_at=expires_at,
                source="env",
            )
        logger.info(
            "Resolved %d secret(s) for process %s",
            len(self._secrets),
            self._process_id,
        )

    def get(self, name: str) -> str | None:
        """Get a secret value by name.

        Returns ``None`` if the secret doesn't exist or has expired.
        Every access is logged to the journal (name only, never value).
        """
        entry = self._secrets.get(name)
        if entry is None:
            return None
        if self._is_expired(entry):
            del self._secrets[name]
            self._log_sync("expired", name)
            return None
        self._access_count += 1
        self._log_sync("access", name)
        return self._fernet.decrypt(entry.value).decode()

    def list_names(self) -> list[str]:
        """Return names of all active (non-expired) secrets."""
        self._prune_expired()
        return list(self._secrets.keys())

    async def rotate(self, name: str, new_value: str) -> None:
        """Replace a secret's value immediately.

        The old value is overwritten in memory.  The rotation event
        is logged to the journal (name only).

        Raises:
            KeyError: If the secret name doesn't exist.
        """
        entry = self._secrets.get(name)
        if entry is None:
            raise KeyError(f"Secret '{name}' not found")
        entry.value = self._fernet.encrypt(new_value.encode())
        entry.source = "rotation"
        self._log_sync("rotation", name)
        logger.info("Rotated secret '%s' for process %s", name, self._process_id)

    async def revoke_all(self) -> None:
        """Remove all secrets from the in-memory store.

        Called on process stop.  Overwrites values with null bytes
        as a best-effort measure (Python's immutable strings mean the
        original value may persist in the interpreter's memory pool
        until garbage-collected).
        """
        for entry in self._secrets.values():
            entry.value = b"\x00" * len(entry.value)
        self._secrets.clear()
        # Rotate the Fernet key so the old encryption key is also discarded
        self._fernet = Fernet(Fernet.generate_key())
        # Force garbage collection to clear copies sooner
        import gc

        gc.collect()
        self._log_sync("revoke_all", "*")
        logger.info("Revoked all secrets for process %s", self._process_id)

    def sanitize_text(self, text: str) -> str:
        """Replace any secret values found in *text* with ``[REDACTED]``.

        Used to sanitise conversation buffers and status output.
        """
        for entry in self._secrets.values():
            plaintext = self._fernet.decrypt(entry.value).decode()
            if plaintext and plaintext in text:
                text = text.replace(plaintext, "[REDACTED]")
        return text

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_env(self, template: str) -> str:
        """Resolve ``${VAR_NAME}`` from ``os.environ``."""
        match = _ENV_PATTERN.match(template.strip())
        if match:
            var_name = match.group(1)
            value = os.environ.get(var_name)
            if value is None:
                raise KeyError(
                    f"Environment variable '{var_name}' not found (required by secret config)"
                )
            return value
        # Not an env reference — treat as literal value
        return template

    def _is_expired(self, entry: SecretEntry) -> bool:
        if entry.expires_at is None:
            return False
        return time.monotonic() > entry.expires_at

    def _prune_expired(self) -> None:
        expired = [name for name, entry in self._secrets.items() if self._is_expired(entry)]
        for name in expired:
            del self._secrets[name]

    def _log_sync(self, action: str, name: str) -> None:
        """Best-effort journal logging (fire-and-forget)."""
        if self._journal is None:
            return
        try:
            from .journal import JournalEntry

            entry = JournalEntry(
                entry_id="",
                process_id=self._process_id,
                timestamp=datetime.now(UTC),
                entry_type="secret_access",
                data={"action": action, "secret_name": name},
            )
            try:
                import asyncio

                loop = asyncio.get_running_loop()
                loop.create_task(self._journal.append(entry))
            except RuntimeError:
                pass  # No event loop — skip
        except Exception:
            pass  # Never fail on logging
