"""Structured operational logs; transport payload logging stays disabled."""
import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value = {
            'time': datetime.fromtimestamp(record.created, UTC).isoformat(),
            'level': record.levelname, 'logger': record.name, 'event': record.getMessage(),
        }
        for name in ('method', 'route', 'status', 'duration_ms'):
            if hasattr(record, name):
                value[name] = getattr(record, name)
        if record.exc_info:
            value['exception'] = record.exc_info[0].__name__
        return json.dumps(value)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler])
    logging.getLogger('skinder').setLevel(level)
    # These libraries can print credentials, request bodies, or source excerpts at DEBUG.
    for name in ('openai', 'httpx', 'httpcore', 'httpx2', 'authlib', 'sqlalchemy.engine'):
        logging.getLogger(name).setLevel(logging.WARNING)
