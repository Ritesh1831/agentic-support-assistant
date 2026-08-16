"""Structured JSON logging setup for the whole application.

Every log record emitted anywhere in the app (agent retries, tool failures,
session-store fallbacks, unhandled-exception reports) goes through the root
logger, so configuring it once here — rather than per-module — is enough to
make every existing `logger.warning(...)`/`logger.error(...)` call in the
codebase emit one JSON object per line, suitable for a log aggregator instead
of free-text lines.
"""

from __future__ import annotations

import logging
import os

from pythonjsonlogger import jsonlogger

# session_id / tool_name / attempt are not standard LogRecord attributes; a
# given record only carries one when its call site passed it via `extra=`
# (e.g. `logger.warning(..., extra={"tool_name": name})`). python-json-logger
# fills in `null` for whichever of these a particular record didn't set
# rather than raising, so every emitted line still has a consistent shape.
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s %(session_id)s %(tool_name)s %(attempt)s"


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = jsonlogger.JsonFormatter(LOG_FORMAT)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
