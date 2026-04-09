"""Custom exception types for SuperAgent functionality.

This module defines all custom exceptions used by the .superagent file loading
and validation system. These exceptions provide clear, actionable error messages
to help users fix configuration issues.
"""

from __future__ import annotations

from typing import Any


class SuperAgentError(RuntimeError):
    """Base exception for all SuperAgent-related errors.

    This is the base class for all exceptions raised by the .superagent
    file loading and validation system. Catching this exception will catch
    all SuperAgent-specific errors.

    Examples:
        >>> raise SuperAgentError("Failed to load configuration")
    """


class SuperAgentValidationError(SuperAgentError):
    """Raised when .superagent schema validation fails.

    This exception is raised when a .superagent file fails Pydantic schema
    validation. It includes detailed error information to help users fix
    the configuration.

    Attributes:
        errors: List of Pydantic validation errors with location and message.
        file_path: Path to the file that failed validation.

    Examples:
        >>> raise SuperAgentValidationError(
        ...     "Schema validation failed",
        ...     errors=[{"loc": ["agent", "model"], "msg": "field required"}],
        ...     file_path="/path/to/agent.superagent"
        ... )
    """

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        file_path: str | None = None,
    ) -> None:
        """Initialize validation error with details.

        Args:
            message: Human-readable error message.
            errors: List of Pydantic validation errors (from ValidationError.errors()).
            file_path: Path to the configuration file that failed validation.
        """
        super().__init__(message)
        self.errors = errors or []
        self.file_path = file_path

    def __str__(self) -> str:
        """Format validation errors for display.

        Returns:
            Formatted error message including file path and detailed validation errors.
        """
        msg = super().__str__()

        if self.file_path:
            msg += f"\nFile: {self.file_path}"

        if self.errors:
            msg += "\n\nValidation errors:"
            for err in self.errors:
                loc = " -> ".join(str(loc_item) for loc_item in err.get("loc", []))
                err_msg = err.get("msg", "Unknown error")
                msg += f"\n  {loc}: {err_msg}"

        return msg


class EnvVarNotFoundError(SuperAgentError):
    """Raised when a required environment variable is not found.

    This exception is raised when a configuration references an environment
    variable using ${VAR_NAME} syntax, but the variable is not set in the
    environment.

    Attributes:
        var_name: The name of the missing environment variable.
        context: Optional context about where the variable was referenced
                 (e.g., "servers.math.url").

    Examples:
        >>> raise EnvVarNotFoundError("OPENAI_API_KEY", context="agent.model.api_key")
    """

    def __init__(self, var_name: str, context: str | None = None) -> None:
        """Initialize environment variable error.

        Args:
            var_name: Name of the missing environment variable.
            context: Optional context string indicating where the variable
                    was referenced (e.g., field path in configuration).
        """
        msg = f"Environment variable '{var_name}' not found"
        if context:
            msg += f" (referenced in: {context})"
        super().__init__(msg)
        self.var_name = var_name
        self.context = context
