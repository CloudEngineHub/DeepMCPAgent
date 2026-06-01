"""Logger configuration for ``promptise.identity``.

The subsystem uses a single root logger — ``promptise.identity`` —
and follows the standard library logging convention of installing a
:class:`logging.NullHandler` at import time so application code, not
the framework, decides whether log output is emitted.

Sub-loggers (``promptise.identity.aws``, ``promptise.identity.entra``,
etc.) created via :func:`logging.getLogger` inherit from this root.
"""

from __future__ import annotations

import logging

#: The single root logger for the identity subsystem.
logger: logging.Logger = logging.getLogger("promptise.identity")


def _configure_default_handler() -> None:
    """Install a :class:`logging.NullHandler` on the root logger.

    Idempotent — repeated calls add no extra handlers. Called once
    from :mod:`promptise.identity.__init__` so the library does not
    emit "no handlers could be found" warnings when used in an
    application that does not configure logging.
    """
    for existing in logger.handlers:
        if isinstance(existing, logging.NullHandler):
            return
    logger.addHandler(logging.NullHandler())
