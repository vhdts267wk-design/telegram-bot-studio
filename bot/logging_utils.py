"""Logging configuration that keeps service credentials out of log output."""

import logging
import os
import re


_SECRET_NAMES = (
    "BOT_TOKEN", "OPENAI_API_KEY", "DATABASE_URL", "PANEL_PASSWORD", "PANEL_SECRET_KEY",
    "MARKET_BRIDGE_KEY",
)
_TOKEN_RE = re.compile(r"(?:bot)?\d{6,12}:[A-Za-z0-9_-]{30,}")
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}")
_DATABASE_RE = re.compile(r"\bpostgres(?:ql)?://\S+", re.IGNORECASE)


class SecretRedactingFormatter(logging.Formatter):
    """Redact the final formatted message, including exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        output = super().format(record)
        values = {
            value
            for name in _SECRET_NAMES
            for value in (os.environ.get(name, ""), os.environ.get(name, "").strip())
            if value
        }
        for value in sorted(values, key=len, reverse=True):
            output = output.replace(value, "[REDACTED]")
        output = _TOKEN_RE.sub("[REDACTED_BOT_TOKEN]", output)
        output = _API_KEY_RE.sub("[REDACTED_API_KEY]", output)
        return _DATABASE_RE.sub("[REDACTED_DATABASE_URL]", output)


def configure_logging(level_name: str = "INFO") -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = SecretRedactingFormatter(
        "%(asctime)s %(name)s [%(levelname)s] %(message)s"
    )
    for handler in root.handlers:
        handler.setFormatter(formatter)
    # HTTP and SDK debug output can include private URLs or uploaded images.
    for name in ("httpx", "httpcore", "httpx2", "httpcore2", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
