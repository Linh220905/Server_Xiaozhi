import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "server.log")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_configured = False


class _ConsoleColorFormatter(logging.Formatter):
    """Colorize only level name in console logs; keep message readable."""

    RESET = "\033[0m"
    COLORS = {
        logging.DEBUG: "\033[36m",     # cyan
        logging.INFO: "\033[92m",      # bright green
        logging.WARNING: "\033[93m",   # yellow
        logging.ERROR: "\033[91m",     # red
        logging.CRITICAL: "\033[95m",  # magenta
    }

    def format(self, record: logging.LogRecord) -> str:
        original_levelname = record.levelname
        color = self.COLORS.get(record.levelno)
        if color:
            record.levelname = f"{color}{original_levelname}{self.RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname


def setup_logging(level: int = logging.INFO) -> None:
    """Configure one shared logging pipeline for the whole server."""
    global _configured
    if _configured:
        return

    os.makedirs(LOG_DIR, exist_ok=True)

    file_formatter = logging.Formatter(LOG_FORMAT)
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)

    console_handler = logging.StreamHandler()
    if sys.stderr.isatty():
        console_handler.setFormatter(_ConsoleColorFormatter(LOG_FORMAT))
    else:
        console_handler.setFormatter(file_formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        external_logger = logging.getLogger(logger_name)
        external_logger.handlers.clear()
        external_logger.propagate = True
        external_logger.setLevel(level)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def info(msg: str, *args, **kwargs) -> None:
    get_logger("server").info(msg, *args, **kwargs)


def warning(msg: str, *args, **kwargs) -> None:
    get_logger("server").warning(msg, *args, **kwargs)


def error(msg: str, *args, **kwargs) -> None:
    get_logger("server").error(msg, *args, **kwargs)


def debug(msg: str, *args, **kwargs) -> None:
    get_logger("server").debug(msg, *args, **kwargs)


if __name__ == "__main__":
    logger = get_logger(__name__)
    logger.info("Test log: Server started successfully.")
    logger.warning("Test log: This is a warning.")
    logger.error("Test log: This is an error.")
    logger.debug("Test log: Debug message.")
    logger.info("Log test completed. Check logs/server.log.")
