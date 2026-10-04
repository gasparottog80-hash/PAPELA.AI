from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from uuid import UUID

from .error_codes import SAFE_ERROR_CODES

SAFE_EVENTS = frozenset(
    {
        "startup", "shutdown", "db.ready", "http.request", "request.failed",
        "upload.rejected", "upload.accepted", "worker.started", "worker.stopping",
        "worker.stopped", "worker.heartbeat", "job.claimed", "job.started",
        "job.done", "job.failed", "job.retry", "job.timeout",
        "job.cleanup_failed", "jobs.reaped", "reaper.failed",
        "retention.purged", "retention.sweep_failed", "retention.sweep",
        "pdf.delete_failed", "pdf.deleted", "pdf.delete_noop",
        "data_deleted", "data_exported", "tenant_deleted",
        "api_key_rotated", "api_key_revoked", "erasure.reconciled",
        "retention.results_purged", "retention.results_failed",
        "privacy.not_ready", "job.erased_in_flight",
    }
)
SAFE_ERROR = re.compile(r"^[a-z_]+: [A-Za-z][A-Za-z0-9_]{0,60}$")


def _safe_uuid(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == value else None


class JsonFormatter(logging.Formatter):
    """Minimal structured JSON log formatter (stdout, one line per record)."""

    def __init__(self, service: str = "unknown") -> None:
        super().__init__()
        if service not in {"api", "worker", "unknown"}:
            raise ValueError("unknown log service")
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "event": record.msg
            if isinstance(record.msg, str) and record.msg in SAFE_EVENTS
            else "event",
        }
        for key in ("request_id", "job_id"):
            value = _safe_uuid(getattr(record, key, None))
            if value is not None:
                payload[key] = value
        subject_ref = getattr(record, "subject_ref", None)
        if isinstance(subject_ref, str) and re.fullmatch(r"[0-9a-f]{16}", subject_ref):
            payload["subject_ref"] = subject_ref
        duration = getattr(record, "duration_ms", None)
        if isinstance(duration, (int, float)) and 0 <= duration <= 3600000:
            payload["duration_ms"] = round(duration, 2)
        attempt = getattr(record, "attempt", None)
        if isinstance(attempt, int) and 0 <= attempt <= 10:
            payload["attempt"] = attempt
        count = getattr(record, "count", None)
        if isinstance(count, int) and 0 <= count <= 1000000:
            payload["count"] = count
        code = getattr(record, "error_code", None)
        if isinstance(code, str) and code in SAFE_ERROR_CODES:
            payload["error_code"] = code
        status_code = getattr(record, "status_code", None)
        if isinstance(status_code, int) and 100 <= status_code <= 599:
            payload["status_code"] = status_code
        route = getattr(record, "route", None)
        if route in {"health", "readiness", "upload", "job", "job_audit", "other"}:
            payload["route"] = route
        cleanup_reason = getattr(record, "cleanup_reason", None)
        if cleanup_reason in {
            "rejected-upload", "create-failed", "purge-after-done",
            "terminal-failure", "tenant-delete", "retention-expired",
            "result-retention", "tenant-offboard", "restore-erasure",
        }:
            payload["cleanup_reason"] = cleanup_reason
        # Only sanitized category and class name; never raw exception text.
        error = getattr(record, "error", None)
        if isinstance(error, str) and SAFE_ERROR.fullmatch(error):
            payload["error"] = error
        if record.exc_info and record.exc_info[1]:
            from .sanitize import sanitize_error

            payload["error"] = sanitize_error(record.exc_info[1])
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str, service: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    handler.addFilter(lambda record: record.name.startswith("papela."))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
