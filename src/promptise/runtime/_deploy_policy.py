"""Where a process config came from, and what that config may do.

A config written by the operator (code, or a manifest on the host) is
trusted.  A config sent to the control API (``POST /api/v1/processes``)
comes from whoever holds the API token, so the runtime limits it:

* ``${VAR}`` environment references resolve only for the variables the
  operator allowed (``OrchestrationAPI(allow_env_references=...,
  allowed_env_vars=...)``), so a deploy can't read the host's environment
  through a secret;
* an empty ``open_mode.allowed_mcp_urls`` means *no* agent-connected MCP
  server (instead of any);
* file system paths it names (journal directory, memory directory,
  watched directories, also those the agent picks later) must lie inside
  the API's ``data_dir``.

The policy is carried by the :class:`~promptise.runtime.process.AgentProcess`
and passed on to the processes it spawns.  The API-side checks (refusing
references and paths before anything is created) live in
:mod:`promptise.runtime.api`; this policy is the backstop.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["DeployPolicy", "default_data_dir", "env_references_in", "path_problem"]


def default_data_dir() -> Path:
    """The default base directory of API-chosen paths: ``./.promptise``
    (where :class:`~promptise.runtime.config.JournalConfig` writes by default)."""
    return Path.cwd() / ".promptise"


def path_problem(value: str, base: str | Path) -> str | None:
    """Why *value* is not a path inside *base* (``None`` when it is).

    *value* is resolved the way the process uses it (relative to the
    working directory, symlinks followed); ``..`` components and ``~``
    are refused outright.
    """
    raw = str(value)
    candidate = Path(raw)
    if raw.startswith("~") or ".." in candidate.parts:
        return f"{raw!r} may not use '..' or '~'"
    root = Path(base).resolve()
    resolved = (candidate if candidate.is_absolute() else Path.cwd() / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        return f"{raw!r} is outside the data directory {str(root)!r}"
    return None


@dataclass(frozen=True)
class DeployPolicy:
    """Limits for a process, by where its config came from.

    Attributes:
        source: ``"operator"`` (the default: code or a manifest on the
            host, no limits) or ``"api"`` (a control-API deploy, or a
            process spawned by one).
        allow_env_references: For ``source="api"``: any ``${VAR}``
            reference may resolve.
        allowed_env_vars: For ``source="api"``: the variables a
            reference may read when ``allow_env_references`` is off.
        data_dir: For ``source="api"``: the directory file system paths
            must lie in (see :func:`path_problem`); ``None`` for no limit.
    """

    source: str = "operator"
    allow_env_references: bool = False
    allowed_env_vars: frozenset[str] = field(default_factory=frozenset)
    data_dir: str | None = None

    def path_problem(self, value: str) -> str | None:
        """Why the process may not use the path *value* (``None``: it may)."""
        if not self.from_api or self.data_dir is None:
            return None
        return path_problem(value, self.data_dir)

    @property
    def from_api(self) -> bool:
        """Whether the config came from the control API."""
        return self.source == "api"

    def env_references(self) -> bool | frozenset[str]:
        """What :class:`~promptise.runtime.secrets.SecretScope` may resolve."""
        if not self.from_api or self.allow_env_references:
            return True
        return self.allowed_env_vars

    def env_var_allowed(self, name: str) -> bool:
        """Whether a reference to *name* may resolve."""
        policy = self.env_references()
        return policy is True or (policy is not False and name in policy)

    @classmethod
    def for_api(
        cls,
        *,
        allow_env_references: bool = False,
        allowed_env_vars: Collection[str] = (),
        data_dir: str | Path | None = None,
    ) -> DeployPolicy:
        """The policy of a control-API deploy."""
        return cls(
            source="api",
            allow_env_references=bool(allow_env_references),
            allowed_env_vars=frozenset(allowed_env_vars),
            data_dir=None if data_dir is None else str(Path(data_dir).resolve()),
        )


def env_references_in(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Every ``${VAR}`` reference in a JSON-like value: ``(path, variable)``.

    Walks dicts (keys and values), lists and tuples; *path* is the dotted
    location of the string (``secrets.secrets.k``, ``triggers[0].hmac_secret``).
    """
    from promptise.env_resolver import iter_env_references

    if isinstance(value, str):
        # Linear-time scan: *value* comes from API requests (CodeQL
        # py/polynomial-redos on ENV_VAR_PATTERN).
        for ref in iter_env_references(value):
            yield path or "<value>", ref.name
    elif isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                yield from env_references_in(key, child)
            yield from env_references_in(item, child)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from env_references_in(item, f"{path}[{index}]")
