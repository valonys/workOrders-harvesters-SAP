"""Logging wiring: rotating file, console, and an optional in-app sink."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Callable, Optional

LOGGER_NAME = "iw29_export"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(suffix: str = "") -> logging.Logger:
    name = f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME
    return logging.getLogger(name)


def configure(
    log_folder: Path,
    level: str = "INFO",
    console: bool = True,
) -> Path:
    """Attach handlers to the app logger once and return the log file path."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_level_of(level))
    logger.propagate = False

    log_folder.mkdir(parents=True, exist_ok=True)
    log_path = log_folder / "iw29_export.log"

    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logger.handlers):
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=7, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
        logger.addHandler(file_handler)

    if console and not any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
        for h in logger.handlers
    ):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        logger.addHandler(stream_handler)

    return log_path


class CallbackHandler(logging.Handler):
    """Forwards formatted records to a callable, used by the GUI log pane."""

    def __init__(self, callback: Callable[[str, str], None], level: int = logging.INFO):
        super().__init__(level)
        self._callback = callback
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._callback(record.levelname, self.format(record))
        except Exception:  # never let logging break the run
            self.handleError(record)


def add_callback_handler(
    callback: Callable[[str, str], None], level: str = "INFO"
) -> CallbackHandler:
    handler = CallbackHandler(callback, _level_of(level))
    logging.getLogger(LOGGER_NAME).addHandler(handler)
    return handler


def remove_handler(handler: Optional[logging.Handler]) -> None:
    if handler is not None:
        logging.getLogger(LOGGER_NAME).removeHandler(handler)


def _level_of(level: str) -> int:
    resolved = getattr(logging, str(level).upper(), None)
    return resolved if isinstance(resolved, int) else logging.INFO
