"""Console logging.

Deliberately thin. Human-readable progress goes here; the machine-readable
record of what happened goes to :mod:`personal_ai_os.observability.trace`.
Keeping those two jobs separate stops the log format from being load-bearing.
"""

from __future__ import annotations

import logging
import sys

LOGGER_NAME = "paios"

_LEVEL_COLORS = {
    logging.DEBUG: "\033[38;5;244m",
    logging.INFO: "\033[38;5;39m",
    logging.WARNING: "\033[38;5;214m",
    logging.ERROR: "\033[38;5;196m",
    logging.CRITICAL: "\033[1;38;5;196m",
}
_RESET = "\033[0m"


class _Formatter(logging.Formatter):
    def __init__(self, *, color: bool) -> None:
        super().__init__("%(message)s")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        # Strip the "paios." prefix: every line has it, so it carries no signal.
        component = record.name.removeprefix(f"{LOGGER_NAME}.")
        label = f"[{component}]"
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if self.color:
            tint = _LEVEL_COLORS.get(record.levelno, "")
            return f"{tint}{label:<16}{_RESET} {message}"
        return f"{label:<16} {message}"


def _supports_color(stream: object) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def setup_logging(level: str = "INFO", *, color: bool | None = None) -> logging.Logger:
    """Configure the ``paios`` logger. Idempotent -- safe to call repeatedly."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    # Ours is the only handler; don't also emit through the root logger.
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _Formatter(color=_supports_color(sys.stderr) if color is None else color)
    )
    logger.addHandler(handler)
    return logger


def get_logger(component: str) -> logging.Logger:
    """Return the logger for one component, e.g. ``get_logger("router")``."""
    return logging.getLogger(f"{LOGGER_NAME}.{component}")
