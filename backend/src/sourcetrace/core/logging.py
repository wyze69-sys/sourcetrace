"""Logging configuration helpers."""

import logging


def configure_logging(level: str = "INFO") -> None:
    """Configure concise process-wide logging."""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
