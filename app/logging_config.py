from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    """Minimal structured JSON log formatter (stdout, one line per record)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            # Only application event identifiers are allowed. Third-party logs
            # (including PDF parser warnings and DB retry messages) are suppressed.
            "msg": record.msg
            if isinstance(record.msg, str)
            and record.msg.replace(".", "").replace("_", "").isalnum()
            else "event",
        }
        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            payload["request_id"] = request_id
        # Sanitized error category (never the raw exception text — LGPD).
        error = getattr(record, "error", None)
        if error is not None:
            payload["error"] = error
        if record.exc_info and record.exc_info[1]:
            from .sanitize import sanitize_error

            payload["error"] = sanitize_error(record.exc_info[1])
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(lambda record: record.name.startswith("papela."))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
