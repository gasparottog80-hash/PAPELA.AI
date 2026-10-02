"""Bounded, process-local Prometheus metrics on a loopback-only HTTP port.

The API and worker have separate container network namespaces, so both may
listen on 127.0.0.1:9100. No metric label accepts a tenant, job, filename,
request ID, URL or arbitrary exception text. Counters reset on process restart.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Literal

ROUTES = frozenset(
    {"health", "readiness", "upload", "job", "job_audit", "other"}
)
STATUS_CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx"})
REJECTION_REASONS = frozenset(
    {"auth", "rate_limit", "too_large", "invalid_pdf", "storage", "timeout"}
)
JOB_OUTCOMES = frozenset({"done", "retry", "failed", "cleanup_failed"})
DB_OPERATIONS = frozenset({"startup", "readiness", "request", "worker"})
BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0, 120.0)


@dataclass(frozen=True)
class MetricSpec:
    help: str
    kind: Literal["counter", "histogram"]
    labels: tuple[str, ...] = ()
    allowed: tuple[frozenset[str], ...] = ()


SPECS = {
    "requests_total": MetricSpec(
        "HTTP requests since process start", "counter",
        ("route", "status_class"), (ROUTES, STATUS_CLASSES),
    ),
    "request_duration_seconds": MetricSpec(
        "HTTP request duration", "histogram", ("route",), (ROUTES,),
    ),
    "uploads_rejected_total": MetricSpec(
        "Rejected upload requests", "counter", ("reason",),
        (REJECTION_REASONS,),
    ),
    "jobs_created_total": MetricSpec("Jobs accepted by the API", "counter"),
    "jobs_completed_total": MetricSpec("Jobs completed by this worker", "counter"),
    "jobs_failed_total": MetricSpec("Terminal job failures", "counter"),
    "jobs_retried_total": MetricSpec("Jobs returned to pending", "counter"),
    "jobs_reaped_total": MetricSpec("Stalled jobs reaped", "counter"),
    "job_processing_duration_seconds": MetricSpec(
        "Worker job processing duration", "histogram", ("outcome",),
        (JOB_OUTCOMES,),
    ),
    "db_errors_total": MetricSpec(
        "Database operation failures", "counter", ("operation",),
        (DB_OPERATIONS,),
    ),
}


@dataclass
class HistogramState:
    buckets: list[int] = field(default_factory=lambda: [0] * len(BUCKETS))
    count: int = 0
    total: float = 0.0


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[str, ...]], int] = {}
        self._histograms: dict[tuple[str, tuple[str, ...]], HistogramState] = {}
        self._heartbeat_at: float | None = None

    @staticmethod
    def _check(name: str, labels: tuple[str, ...], kind: str) -> MetricSpec:
        spec = SPECS[name]
        if spec.kind != kind or len(labels) != len(spec.labels):
            raise ValueError("invalid metric labels")
        if any(
            value not in allowed
            for value, allowed in zip(labels, spec.allowed, strict=True)
        ):
            raise ValueError("unbounded metric label")
        return spec

    def inc(self, name: str, *labels: str, amount: int = 1) -> None:
        self._check(name, labels, "counter")
        if amount < 0:
            raise ValueError("counter cannot decrease")
        key = (name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def observe(self, name: str, seconds: float, *labels: str) -> None:
        self._check(name, labels, "histogram")
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid duration")
        key = (name, labels)
        with self._lock:
            state = self._histograms.setdefault(key, HistogramState())
            state.count += 1
            state.total += seconds
            for index, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    state.buckets[index] += 1

    def heartbeat(self) -> None:
        with self._lock:
            self._heartbeat_at = time.monotonic()

    def counter_value(self, name: str, *labels: str) -> int:
        self._check(name, labels, "counter")
        with self._lock:
            return self._counters.get((name, labels), 0)

    def render(self, *, worker: bool = False) -> str:
        with self._lock:
            counters = dict(self._counters)
            histograms = {
                key: HistogramState(list(state.buckets), state.count, state.total)
                for key, state in self._histograms.items()
            }
            heartbeat_at = self._heartbeat_at
        lines: list[str] = []
        for name, spec in SPECS.items():
            lines.extend((f"# HELP {name} {spec.help}", f"# TYPE {name} {spec.kind}"))
            if spec.kind == "counter":
                for (metric, labels), value in sorted(counters.items()):
                    if metric == name:
                        lines.append(f"{name}{_labels(spec.labels, labels)} {value}")
            else:
                for (metric, labels), state in sorted(histograms.items()):
                    if metric != name:
                        continue
                    for bound, count in zip(BUCKETS, state.buckets, strict=True):
                        names = (*spec.labels, "le")
                        values = (*labels, str(bound))
                        lines.append(f"{name}_bucket{_labels(names, values)} {count}")
                    infinity = _labels((*spec.labels, "le"), (*labels, "+Inf"))
                    series = _labels(spec.labels, labels)
                    lines.append(f"{name}_bucket{infinity} {state.count}")
                    lines.append(f"{name}_sum{series} {state.total}")
                    lines.append(f"{name}_count{series} {state.count}")
        if worker:
            age = max(0.0, time.monotonic() - heartbeat_at) if heartbeat_at else -1
            lines.extend(
                (
                    "# HELP worker_heartbeat_age_seconds "
                    "Seconds since the worker loop advanced",
                    "# TYPE worker_heartbeat_age_seconds gauge",
                    f"worker_heartbeat_age_seconds {age}",
                )
            )
        return "\n".join(lines) + "\n"


def _labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    entries = (f'{name}="{value}"' for name, value in zip(names, values, strict=True))
    return "{" + ",".join(entries) + "}"


class PrivateMetricsServer(HTTPServer):
    allow_reuse_address = True


def start_metrics_server(
    metrics: Metrics, *, worker: bool = False
) -> PrivateMetricsServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/metrics":
                self.send_error(404)
                return
            body = metrics.render(worker=worker).encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            # Metrics queries are not routed through the API or access logger.
            return

    server = PrivateMetricsServer(("127.0.0.1", 9100), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


METRICS = Metrics()
