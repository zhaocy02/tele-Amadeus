from __future__ import annotations

import logging
import re

_TELEGRAM_BOT_TOKEN_PATH = re.compile(r"(?P<prefix>/bot)[0-9]+:[A-Za-z0-9_-]+")
_NOISY_HTTP_LOGGERS = ("httpx", "httpcore")


class _SensitiveURLRedactionFilter(logging.Filter):
    """Redact credentials embedded in request URLs before handlers render them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        redacted = _redact_sensitive_log_text(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def _redact_sensitive_log_text(text: str) -> str:
    return _TELEGRAM_BOT_TOKEN_PATH.sub(r"\g<prefix><redacted>", text)


def configure_logging(level: str = "INFO") -> None:
    """Configure process logging without emitting credentials embedded in HTTP URLs."""

    normalized = level.upper()
    numeric_level = getattr(logging, normalized, None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"invalid log level: {level}")

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # httpx/httpcore INFO records contain full request URLs. Telegram Bot API credentials live in
    # the URL path, so request-line logging must not inherit the application INFO level.
    for logger_name in _NOISY_HTTP_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    # Keep a handler-level defense in depth for warnings/errors or future libraries that may still
    # render a Telegram Bot API URL. Handler filters cover propagated records from child loggers.
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(item, _SensitiveURLRedactionFilter) for item in handler.filters):
            handler.addFilter(_SensitiveURLRedactionFilter())
