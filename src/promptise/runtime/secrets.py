"""Agent-native secret scoping for the runtime.

Provides per-process secrets with TTL, rotation, access logging,
and zero-retention revocation.  Secret values live **only** in memory
(Fernet-encrypted with a per-scope key) and are never serialised to the
journal, checkpoints, status output or API responses: the scope's own
journal entries hold names only, and :meth:`SecretScope.sanitize_text`
redacts values (including values replaced by a rotation and values that
expired) from text before it is stored or shown.

Environment references in the config (``${VAR}``, ``${VAR:-default}``,
or text containing them) are resolved when :meth:`SecretScope.resolve_initial`
runs (process start), not when the config is built, so configs and
manifests never hold the resolved value.  A scope for a config that came
from the control API (``source="api"``) resolves only the references its
``env_references`` policy allows, so a deploy can't read the host's
environment.

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
    await scope.rotate("api_key", new_token, ttl=1800)
    scope.list_metadata()          # names, expiry, rotations — never values
"""

from __future__ import annotations

import logging
import time
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet

if TYPE_CHECKING:
    from .config import SecretScopeConfig
    from .journal import JournalProvider

logger = logging.getLogger("promptise.runtime.secrets")

#: Text that replaces a secret value in :meth:`SecretScope.sanitize_text`.
REDACTED = "[REDACTED]"

#: Previous values kept per secret (encrypted) so they stay redacted.
MAX_RETIRED_VALUES = 10


class SecretResolutionError(KeyError):
    """A secret's environment reference could not be resolved.

    Raised by :meth:`SecretScope.resolve_initial`.  Subclasses
    :class:`KeyError` (what was raised before).  For a scope with
    ``source="api"`` the message is the same whether the variable is not
    allowed or not set, so it can't be used to probe the host's
    environment; the details go to the log.

    Attributes:
        secret_name: The secret whose reference failed.
    """

    def __init__(self, message: str, secret_name: str) -> None:
        super().__init__(message)
        self.message = message
        self.secret_name = secret_name

    def __str__(self) -> str:
        return self.message


class SecretNotFoundError(KeyError):
    """No secret with this name is configured in the scope.

    Subclasses :class:`KeyError` (what :meth:`SecretScope.rotate` raised
    in 1.4.3 and earlier).

    Attributes:
        name: The requested secret name.
    """

    def __init__(self, name: str, known: list[str]) -> None:
        super().__init__(name)
        self.name = name
        self.known = known

    def __str__(self) -> str:
        known = ", ".join(self.known) or "none"
        return f"Secret {self.name!r} not found (configured secrets: {known})"


@dataclass
class SecretEntry:
    """A single scoped secret.

    Attributes:
        name: Logical secret name.
        value: The secret value, Fernet-encrypted (in memory only —
            **never** serialised).
        expires_at: Monotonic timestamp when this entry expires, or
            ``None`` for no expiry.
        source: How this secret was provisioned (``"env"`` for a resolved
            reference, ``"literal"`` for a value written in the config, or
            ``"rotation"``).
        ttl: The TTL in seconds the current value was given (``None`` =
            no expiry).
        rotation_count: How many times the secret was rotated.
        rotated_at: When it was last rotated (UTC), or ``None``.
        access_count: How many times :meth:`SecretScope.get` returned it.
    """

    name: str
    value: bytes  # Fernet-encrypted — never plaintext in memory
    expires_at: float | None = None
    source: str = "env"
    ttl: float | None = None
    rotation_count: int = 0
    rotated_at: datetime | None = None
    access_count: int = 0

    def __repr__(self) -> str:
        return (
            f"SecretEntry(name={self.name!r}, value=<encrypted>, source={self.source!r}, "
            f"ttl={self.ttl!r}, rotation_count={self.rotation_count})"
        )


class SecretScope:
    """Per-process secret vault with TTL, access logging, and rotation.

    Args:
        config: Secret scoping configuration.
        process_id: Owning process ID (for journal entries).
        journal: Optional journal provider for access logging.
        source: Where the config came from: ``"operator"`` (code or a
            manifest on the host) or ``"api"`` (a control-API deploy, or a
            process spawned by one).  Only changes the error messages:
            for ``"api"`` a failed reference gives the same generic error
            whether the variable is refused or not set.
        env_references: Which environment variables ``${VAR}`` references
            may read: ``True`` (any, the default), ``False`` (none: a
            reference fails to resolve) or a collection of variable names.
    """

    def __init__(
        self,
        config: SecretScopeConfig,
        process_id: str,
        journal: JournalProvider | None = None,
        *,
        source: str = "operator",
        env_references: bool | Collection[str] = True,
    ) -> None:
        self._config = config
        self._process_id = process_id
        self._journal = journal
        self._source = source
        self._env_references: bool | frozenset[str] = (
            env_references if isinstance(env_references, bool) else frozenset(env_references)
        )
        self._secrets: dict[str, SecretEntry] = {}
        # Values that are no longer current (rotated or expired), kept
        # encrypted so sanitize_text still redacts them; cleared by revoke_all.
        self._retired: dict[str, list[bytes]] = {}
        self._access_count: int = 0
        self._fernet = Fernet(Fernet.generate_key())
        # Journal writes in flight (kept referenced so they aren't collected).
        self._pending_logs: set[Any] = set()

    def __repr__(self) -> str:
        return (
            f"SecretScope(process_id={self._process_id!r}, "
            f"active_secrets={self.active_secret_names!r})"
        )

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

    @property
    def active_secrets(self) -> list[str]:
        """Names of non-expired secrets (alias of :attr:`active_secret_names`)."""
        return self.active_secret_names

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve_initial(self, *, missing_only: bool = False) -> None:
        """Resolve all secrets from config on startup.

        Each value in ``config.secrets`` is either a literal string or
        contains environment references (``${VAR}``, ``${VAR:-default}``),
        which are resolved from ``os.environ`` now.  Calling it again
        re-resolves every secret and resets its TTL.

        Args:
            missing_only: Resolve only the secrets the scope doesn't hold
                (never resolved, revoked or expired) and keep the others,
                including values set by :meth:`rotate`.  A process uses it
                when it starts again (``revoke_on_stop=False``), so an
                operator's rotation survives a restart.

        Raises:
            SecretResolutionError: A referenced environment variable is
                not set and has no default, or ``env_references`` doesn't
                allow it (a :class:`KeyError`).  The message names the
                variable and the secret, except for ``source="api"``.
        """
        for name, template in self._config.secrets.items():
            if missing_only and name in self._secrets:
                continue
            raw = template.get_secret_value() if hasattr(template, "get_secret_value") else template
            value, source = self._resolve_env(name, str(raw))
            ttl = self._config.ttls.get(name, self._config.default_ttl)
            old = self._secrets.get(name)
            if old is not None:
                self._retire(old)
            self._secrets[name] = SecretEntry(
                name=name,
                value=self._fernet.encrypt(value.encode()),
                expires_at=(time.monotonic() + ttl) if ttl is not None else None,
                source=source,
                ttl=ttl,
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
            self._expire(name)
            return None
        self._access_count += 1
        entry.access_count += 1
        self._log_sync("access", name)
        return self._fernet.decrypt(entry.value).decode()

    def list_names(self) -> list[str]:
        """Return names of all active (non-expired) secrets."""
        self._prune_expired()
        return list(self._secrets.keys())

    def list_metadata(self) -> list[dict[str, Any]]:
        """Describe every configured secret — never its value.

        Returns:
            One JSON-safe dict per secret in ``config.secrets``:
            ``name``, ``active`` (present and not expired), ``source``,
            ``ttl`` (seconds the current value was given, ``None`` = no
            expiry), ``expires_in`` (seconds left, ``None`` = no expiry or
            not active), ``rotation_count``, ``rotated_at`` (ISO UTC or
            ``None``) and ``access_count``.
        """
        self._prune_expired()
        now = time.monotonic()
        result: list[dict[str, Any]] = []
        names = list(self._config.secrets) + [
            n for n in self._secrets if n not in self._config.secrets
        ]
        for name in names:
            entry = self._secrets.get(name)
            if entry is None:
                result.append(
                    {
                        "name": name,
                        "active": False,
                        "source": None,
                        "ttl": self._config.ttls.get(name, self._config.default_ttl),
                        "expires_in": None,
                        "rotation_count": 0,
                        "rotated_at": None,
                        "access_count": 0,
                    }
                )
                continue
            result.append(
                {
                    "name": name,
                    "active": True,
                    "source": entry.source,
                    "ttl": entry.ttl,
                    "expires_in": (
                        round(max(0.0, entry.expires_at - now), 3)
                        if entry.expires_at is not None
                        else None
                    ),
                    "rotation_count": entry.rotation_count,
                    "rotated_at": entry.rotated_at.isoformat() if entry.rotated_at else None,
                    "access_count": entry.access_count,
                }
            )
        return result

    async def rotate(self, name: str, new_value: str, *, ttl: float | None = None) -> None:
        """Replace a secret's value immediately.

        The old value is overwritten in memory (and kept, encrypted, only
        so that :meth:`sanitize_text` keeps redacting it).  The rotation
        event is logged to the journal (name only).

        Args:
            name: A secret configured in ``config.secrets``.  An expired
                secret can be rotated: it becomes active again.
            new_value: The new value (non-empty).
            ttl: Seconds the new value is valid for, from now.  ``None``
                keeps the current expiry; for an expired secret, ``None``
                applies its configured TTL (``ttls`` / ``default_ttl``)
                from now.

        Raises:
            SecretNotFoundError: The name isn't a configured secret (a
                :class:`KeyError`).
            ValueError: ``new_value`` is empty or ``ttl`` isn't positive.
        """
        if not isinstance(new_value, str) or not new_value:
            raise ValueError("new_value must be a non-empty string")
        if ttl is not None and ttl <= 0:
            raise ValueError(f"ttl must be a positive number of seconds, got {ttl!r}")
        entry = self._secrets.get(name)
        if entry is None and name not in self._config.secrets:
            raise SecretNotFoundError(name, sorted(set(self._config.secrets) | set(self._secrets)))

        now = time.monotonic()
        if entry is None or self._is_expired(entry):
            if entry is not None:
                self._expire(name)
            configured = self._config.ttls.get(name, self._config.default_ttl)
            effective_ttl = ttl if ttl is not None else configured
            entry = SecretEntry(
                name=name,
                value=b"",
                expires_at=(now + effective_ttl) if effective_ttl is not None else None,
                ttl=effective_ttl,
                rotation_count=0,
            )
            self._secrets[name] = entry
        else:
            self._retire(entry)
            if ttl is not None:
                entry.expires_at = now + ttl
                entry.ttl = ttl

        entry.value = self._fernet.encrypt(new_value.encode())
        entry.source = "rotation"
        entry.rotation_count += 1
        entry.rotated_at = datetime.now(timezone.utc)
        self._log_sync("rotation", name)
        logger.info("Rotated secret '%s' for process %s", name, self._process_id)

    async def revoke_all(self) -> None:
        """Remove all secrets from the in-memory store.

        Called on process stop.  Overwrites values with null bytes
        as a best-effort measure (Python's immutable strings mean the
        original value may persist in the interpreter's memory pool
        until garbage-collected).  Retired values are discarded too, so
        :meth:`sanitize_text` no longer knows any value afterwards.
        """
        for entry in self._secrets.values():
            entry.value = b"\x00" * len(entry.value)
        self._secrets.clear()
        self._retired.clear()
        # Rotate the Fernet key so the old encryption key is also discarded
        self._fernet = Fernet(Fernet.generate_key())
        # Force garbage collection to clear copies sooner
        import gc

        gc.collect()
        self._log_sync("revoke_all", "*")
        logger.info("Revoked all secrets for process %s", self._process_id)

    def sanitize_text(self, text: str) -> str:
        """Replace secret values found in *text* with ``[REDACTED]``.

        Redacts current values and values that were replaced by
        :meth:`rotate` or expired (the last :data:`MAX_RETIRED_VALUES` per
        secret), longest first.  The runtime uses it to keep secret values
        out of the journal; call it on any text you log or show.
        """
        if not text or not isinstance(text, str):
            return text
        for plaintext in self._known_values():
            if plaintext in text:
                text = text.replace(plaintext, REDACTED)
        return text

    def sanitize(self, value: Any) -> Any:
        """:meth:`sanitize_text` applied to every string in a nested value.

        Dicts (keys and values), lists and tuples are walked; other values
        are returned unchanged.  Returns a new object, the input is not
        modified.
        """
        values = self._known_values()
        if not values:
            return value
        return _redact_nested(value, values)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _known_values(self) -> list[str]:
        """Every value to redact, longest first (so overlaps redact fully)."""
        found: set[str] = set()
        for entry in self._secrets.values():
            found.add(self._fernet.decrypt(entry.value).decode())
        for retired in self._retired.values():
            for token in retired:
                found.add(self._fernet.decrypt(token).decode())
        found.discard("")
        return sorted(found, key=len, reverse=True)

    def _retire(self, entry: SecretEntry) -> None:
        if not entry.value:
            return
        retired = self._retired.setdefault(entry.name, [])
        retired.append(entry.value)
        del retired[:-MAX_RETIRED_VALUES]

    def _expire(self, name: str) -> None:
        entry = self._secrets.pop(name, None)
        if entry is not None:
            self._retire(entry)
            self._log_sync("expired", name)

    def _resolve_env(self, name: str, template: str) -> tuple[str, str]:
        """Resolve environment references; return ``(value, source)``."""
        from promptise.env_resolver import has_env_reference, iter_env_references, resolve_env_var
        from promptise.exceptions import EnvVarNotFoundError

        if not has_env_reference(template):
            # Not an env reference — treat as literal value
            return template, "literal"
        policy = self._env_references
        refused = sorted(
            {
                ref.name
                for ref in iter_env_references(template)
                if policy is False or (policy is not True and ref.name not in policy)
            }
        )
        if refused:
            # The log names the variables, not the secret: secret names stay
            # out of logs (the error below carries the name to the caller).
            logger.warning(
                "A secret of process %s references environment variable(s) %s, which this "
                "process may not read (source=%s)",
                self._process_id,
                ", ".join(refused),
                self._source,
            )
            raise self._resolution_error(
                name,
                f"Secret '{name}' references environment variable(s) {', '.join(refused)}, "
                "which this process may not read",
            )
        try:
            return resolve_env_var(template.strip(), context=f"secrets.{name}"), "env"
        except EnvVarNotFoundError as exc:
            var_name = getattr(exc, "var_name", None) or str(exc)
            raise self._resolution_error(
                name,
                f"Environment variable '{var_name}' not found "
                f"(required by secret '{name}'); set it before starting the process",
            ) from None

    def _resolution_error(self, name: str, detail: str) -> SecretResolutionError:
        """The error for a reference that can't be resolved (generic for the API)."""
        if self._source != "api":
            return SecretResolutionError(detail, name)
        # The detail names the secret; it goes to the operator's log only in
        # this generic form, and to no API answer at all.
        logger.warning(
            "Process %s: a secret from a config deployed through the control API "
            "could not be resolved",
            self._process_id,
        )
        return SecretResolutionError(
            f"Secret '{name}' could not be resolved: a config deployed through the "
            "control API can only reference the environment variables the operator "
            "allowed, and they must be set",
            name,
        )

    def _is_expired(self, entry: SecretEntry) -> bool:
        if entry.expires_at is None:
            return False
        return time.monotonic() > entry.expires_at

    def _prune_expired(self) -> None:
        expired = [name for name, entry in self._secrets.items() if self._is_expired(entry)]
        for name in expired:
            self._expire(name)

    def _log_sync(self, action: str, name: str) -> None:
        """Best-effort journal logging (fire-and-forget)."""
        if self._journal is None:
            return
        try:
            from .journal import JournalEntry

            entry = JournalEntry(
                process_id=self._process_id,
                timestamp=datetime.now(timezone.utc),
                entry_type="secret_access",
                data={"action": action, "secret_name": name},
            )
            try:
                import asyncio

                loop = asyncio.get_running_loop()
                task = loop.create_task(self._journal.append(entry))
                self._pending_logs.add(task)
                task.add_done_callback(self._pending_logs.discard)
            except RuntimeError:
                pass  # No event loop — skip
        except Exception:
            pass  # Never fail on logging


def _redact_nested(value: Any, secrets_: list[str]) -> Any:
    if isinstance(value, str):
        for plaintext in secrets_:
            if plaintext in value:
                value = value.replace(plaintext, REDACTED)
        return value
    if isinstance(value, dict):
        return {
            _redact_nested(k, secrets_) if isinstance(k, str) else k: _redact_nested(v, secrets_)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_nested(v, secrets_) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_nested(v, secrets_) for v in value)
    return value
