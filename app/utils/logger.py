import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Every LogRecord has these. Anything else on the record was passed via `extra=`.
_BUILTIN_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "color_message",  # uvicorn adds an ANSI-coloured copy of the message to its records
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(_context(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """For local development. Same fields as the JSON output, as key=value pairs."""

    default_msec_format = "%s.%03d"

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        context = " ".join(f"{key}={_text_value(value)}" for key, value in _context(record).items())
        if not context:
            return line
        # keep the traceback (if any) below the fields
        first, sep, rest = line.partition("\n")
        return f"{first} {context}{sep}{rest}"


def _text_value(value: object) -> str:
    text = str(value)
    # Quote values with spaces (mostly error messages) so the pairs stay readable,
    # and with newlines so one record can't spill onto lines that look like others.
    needs_quotes = not text or '"' in text or any(ch.isspace() for ch in text)
    return json.dumps(text) if needs_quotes else text


def _context(record: logging.LogRecord) -> dict:
    context = {}
    for key, value in record.__dict__.items():
        if key not in _BUILTIN_ATTRS and not key.startswith("_"):
            context[key] = value
    # last, so the fields specific to this line come first
    request_id = request_id_var.get()
    if request_id:
        context["request_id"] = request_id
    return context


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Route uvicorn's own logs through our handler so everything comes out in one format.
    # The access log is replaced by middleware/request_context.py.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
