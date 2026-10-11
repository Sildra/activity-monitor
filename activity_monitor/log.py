import logging
import sys
import time

from uvicorn.logging import AccessFormatter, DefaultFormatter


class _TimestampMixin:
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        if datefmt:
            return time.strftime(datefmt, self.converter(record.created))

        timestamp = time.strftime("%H:%M:%S", self.converter(record.created))

        return f"{timestamp}.{record.msecs:03.0f}"


class TimestampFormatter(_TimestampMixin, DefaultFormatter):
    pass


class TimestampAccessFormatter(_TimestampMixin, AccessFormatter):
    pass


def get_logger(name: str = "activity_monitor") -> logging.Logger:
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(TimestampFormatter("%(asctime)s %(levelprefix)s %(message)s", use_colors=None))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    return logger
