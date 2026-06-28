"""Unit tests for the subsystem logger configuration."""

from __future__ import annotations

import logging

from promptise.identity._internal import logging as id_logging


def test_logger_name_is_subsystem_root() -> None:
    assert id_logging.logger.name == "promptise.identity"


def test_configure_default_handler_is_idempotent() -> None:
    """Calling :func:`_configure_default_handler` repeatedly installs
    exactly one NullHandler — never a duplicate."""
    logger = id_logging.logger
    # Snapshot and clear any existing NullHandlers so we exercise both
    # the install branch and the early-return branch deterministically.
    original = list(logger.handlers)
    null_handlers = [h for h in logger.handlers if isinstance(h, logging.NullHandler)]
    for h in null_handlers:
        logger.removeHandler(h)
    try:
        # First call installs one.
        id_logging._configure_default_handler()
        after_first = [
            h for h in logger.handlers if isinstance(h, logging.NullHandler)
        ]
        assert len(after_first) == 1

        # Second call is a no-op — still exactly one.
        id_logging._configure_default_handler()
        after_second = [
            h for h in logger.handlers if isinstance(h, logging.NullHandler)
        ]
        assert len(after_second) == 1
    finally:
        # Restore the original handler set so we don't leak state into
        # other tests.
        for h in list(logger.handlers):
            logger.removeHandler(h)
        for h in original:
            logger.addHandler(h)


def test_configure_default_handler_skips_non_null_handlers() -> None:
    """A non-NullHandler already on the logger must not short-circuit the
    install — the loop skips past it and still adds exactly one NullHandler."""
    logger = id_logging.logger
    original = list(logger.handlers)
    try:
        # Clean slate with a single *non-null* handler present, so the
        # idempotency loop iterates past a handler that is not a NullHandler.
        for h in list(logger.handlers):
            logger.removeHandler(h)
        stream_handler = logging.StreamHandler()
        logger.addHandler(stream_handler)

        id_logging._configure_default_handler()

        null_handlers = [
            h for h in logger.handlers if isinstance(h, logging.NullHandler)
        ]
        assert len(null_handlers) == 1
        assert stream_handler in logger.handlers
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)
        for h in original:
            logger.addHandler(h)
