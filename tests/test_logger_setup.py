"""Logger setup tests."""

import logging

from polymarket_arb.logger_setup import setup_logging


def test_setup_logging_downgrades_httpx_logger_to_warning():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    httpx_logger = logging.getLogger("httpx")
    urllib3_logger = logging.getLogger("urllib3")
    requests_logger = logging.getLogger("requests")
    original_httpx_level = httpx_logger.level
    original_urllib3_level = urllib3_logger.level
    original_requests_level = requests_logger.level

    for handler in list(root.handlers):
        root.removeHandler(handler)

    try:
        setup_logging(level="INFO", log_file="")

        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("urllib3").level == logging.WARNING
        assert logging.getLogger("requests").level == logging.WARNING
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)
        httpx_logger.setLevel(original_httpx_level)
        urllib3_logger.setLevel(original_urllib3_level)
        requests_logger.setLevel(original_requests_level)
