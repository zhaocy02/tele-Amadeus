import logging

from amadeus_bot.logging_config import _SensitiveURLRedactionFilter, configure_logging


def test_logging_filter_redacts_telegram_bot_token_from_formatted_url() -> None:
    token = "123456789:ABC_def-GHIjkl"
    record = logging.LogRecord(
        name="httpx",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: GET %s",
        args=(f"https://api.telegram.org/bot{token}/getUpdates",),
        exc_info=None,
    )

    assert _SensitiveURLRedactionFilter().filter(record) is True
    rendered = record.getMessage()

    assert token not in rendered
    assert "https://api.telegram.org/bot<redacted>/getUpdates" in rendered


def test_configure_logging_suppresses_http_request_info_logs() -> None:
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    previous_httpx = httpx_logger.level
    previous_httpcore = httpcore_logger.level
    try:
        httpx_logger.setLevel(logging.NOTSET)
        httpcore_logger.setLevel(logging.NOTSET)

        configure_logging("INFO")

        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
    finally:
        httpx_logger.setLevel(previous_httpx)
        httpcore_logger.setLevel(previous_httpcore)
