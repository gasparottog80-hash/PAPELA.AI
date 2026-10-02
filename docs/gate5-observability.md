# Gate 5 — observability (local/CI candidate)

Scope: structured diagnostics, bounded metrics and a minimal alert plan for
the existing single-VPS topology. This does not deploy a collector, dashboard,
VPS or public endpoint. It does not authorize customer traffic. The Gate 3
tenant boundary and Gate 4 topology remain unchanged.

## Baseline map and changes

| Component | Before | Gate 5 instrumentation |
| --- | --- | --- |
| API | JSON events and `X-Request-ID`, but no service field or metrics; one upload event mislabeled a job ID as request ID | Allowlisted JSON schema, stable error codes, normalized route/status/duration, request and upload/job counters; correct request-to-job link |
| Worker | Events for claim/done/failure; file heartbeat used by Docker | Claim/start/done/retry/failure/timeout/cleanup events with job ID, attempt and duration; process-local counters/histogram and heartbeat-age gauge |
| Postgres | Native container stdout/stderr and API readiness check | Native logs with Docker rotation and password/data-safe statement settings; API/worker count bounded DB errors |
| Caddy | Runtime log filtering removed request headers; access log absent | JSON access logs with headers, URI/query, host, IP and response headers removed; runtime filter uses the same redaction |
| Migrator | One-shot `psql` output and notices | JSON start/completion/failure events; raw `psql` output is suppressed so future SQL errors cannot echo a password or document field |
| CI | Quality/security and synthetic E2E gates | Exercises private metrics endpoints and verifies a synthetic header/query canary never appears in Caddy logs |

## Structured logs and correlation

API and worker write one JSON object per event to stdout. The allowlisted
fields are `ts` (UTC ISO 8601), `level`, `service`, `event`, canonical
`request_id`/`job_id` when relevant, bounded `duration_ms`, `status_code`,
normalized `route`, `attempt`, `count`, stable `error_code` and a sanitized
error category/class. Unknown event names and fields are dropped by the
formatter. No tenant UUID is logged: route and result metrics do not need it.
Raw exception messages, stack traces, URLs, filenames, headers and PDF/OCR
content are not rendered. The worker may log a sanitized exception class but
never the original message. Do not forward unreviewed container logs to an
external service.

`X-Request-ID` is accepted only as a single canonical UUIDv4; absent,
duplicate or malformed values are replaced. The validated ID is returned on
every HTTP response, including admission rejections. The upload acceptance
event contains both this ID and the server-generated `job_id`. Worker events
carry the same `job_id`, providing a durable correlation path without storing
client-supplied IDs in Postgres or changing the job schema. Job IDs are
operational pseudonyms, not public metrics labels.

Stable response `code` values include `AUTH_INVALID`, `INVALID_REQUEST`,
`INVALID_PDF`, `UPLOAD_TOO_LARGE`, `RATE_LIMITED`, `JOB_NOT_FOUND`,
`JOB_NOT_TERMINAL`, `STORAGE_UNAVAILABLE`, `DB_UNAVAILABLE`,
`PROCESSING_TIMEOUT` and `INTERNAL_ERROR`. The `error` message remains
generic/safe. A processing exception uses a stable code internally and
retains only the existing sanitized category/class in job status. Clients
never receive raw stack traces or document fragments. Cross-tenant and
unknown job IDs retain identical 404 behavior.

## Metrics and access boundary

Each API/worker process serves Prometheus text at `127.0.0.1:9100/metrics`
**inside its own container only**. Port 9100 is neither published by Compose
nor proxied by Caddy. The API's public `/metrics` route returns 404. Operators
can inspect a container locally with `docker exec <api-or-worker> python -c`
and `urllib.request.urlopen('http://127.0.0.1:9100/metrics')`; a future
collector requires a separately reviewed namespace/access design. No
Prometheus/Grafana service or third-party account is installed at this gate.

| Metric | Scope / labels |
| --- | --- |
| `requests_total`, `request_duration_seconds` | API; fixed route names and status class (duration: route only) |
| `uploads_rejected_total` | API; fixed rejection reason |
| `jobs_created_total` | API; no labels |
| `jobs_completed_total`, `jobs_failed_total`, `jobs_retried_total`, `jobs_reaped_total` | Worker; no labels |
| `job_processing_duration_seconds` | Worker; fixed outcome |
| `worker_heartbeat_age_seconds` | Worker; no labels, `-1` before first heartbeat |
| `db_errors_total` | Process-local; fixed operation name |

All counters are process-local and reset on restart. This is intentional:
they are operational rates, not financial/audit totals. Histogram buckets
are bounded and labels reject arbitrary values. No tenant, job, request,
filename, URL, exception text, customer IP or document data can become a
metric label. Docker's existing healthchecks remain the availability source
when a process cannot serve metrics.

Postgres explicitly uses `log_min_error_statement=PANIC`,
`log_min_messages=FATAL`,
`log_parameter_max_length_on_error=0`, `log_statement=none` and
`log_error_verbosity=terse`. Ordinary SQL error messages may themselves
contain input values, so they are suppressed from server logs; API/worker
emit bounded DB error counts and the migrator emits only a safe failure code.
This also reduces SQL-level diagnosis. The [PostgreSQL 16 logging
documentation](https://www.postgresql.org/docs/16/runtime-config-logging.html)
describes the default risk. Operators should reproduce migration failures on
a restored synthetic database instead of enabling statement logging against
customer data.

The Caddy access log is JSON to stdout with method, protocol, status,
duration and size but deliberately **without** URI/query, IP, host or any
request/response headers. API logs provide a normalized route for diagnosis.
This trades per-URL proxy detail for protection against credentials or
personal data supplied in URLs. The Caddy runtime logger also removes these
fields from embedded request objects. A synthetic canary in `Authorization`
and query string is checked against Caddy logs in CI.

`/health` reports only process liveness; `/readiness` reports only
`ready`/`not_ready` after database and storage checks. Detailed failure
categories stay in internal metrics/logs. The worker file heartbeat and
Docker healthcheck remain, augmented by a loop-age gauge. A 200 health
response alone does not certify job processing; the E2E smoke still does.

## Minimal alert plan (not activated)

No external alert recipient or service is configured. Before real deploy,
an operator must choose a local collector/notifier, test delivery and assign
an on-call owner. Suggested starting rules, to calibrate with measured load:

| Signal | Initial trigger | First response |
| --- | --- | --- |
| API unavailable | Docker API unhealthy or `/health` unavailable for 2 minutes | Check process/container restart and Caddy upstream |
| Readiness failure | `/readiness` 503 for 1 minute | Check Postgres, volume free space, DB pool |
| Worker stalled | Docker worker unhealthy or heartbeat age >180 seconds | Check loop, DB, PDF parser timeout/reaper |
| Elevated errors | 5xx rate >5% over 5 minutes with at least 20 requests | Use request IDs, error codes and normalized routes |
| Pending/processing jobs stuck | Oldest pending >5 minutes or processing > configured stall timeout | Aggregate-only read-only SQL over `jobs`; inspect retries/reaper, never dump results/PDFs |
| Disk/quota pressure | Host/volume >80% or remaining private PDF space <2 upload limits | Stop admission safely; expand capacity under change control |
| Postgres unavailable | DB container unhealthy, readiness 503 or rising `db_errors_total` | Check DB logs, storage and role connectivity |

Example aggregate query for the queue (operator-only, no row payload):

```sql
SELECT status, count(*),
       coalesce(extract(epoch FROM now() - min(updated_at)), 0) AS oldest_age_s
FROM jobs WHERE status IN ('pending', 'processing') GROUP BY status;
```

## Privacy, retention and operational limits

Production Compose rotates each container's Docker `json-file` logs with
`PAPELA_LOG_MAX_SIZE` (default `10m`) and `PAPELA_LOG_MAX_FILES` (default
`3`). This is a **size limit**, not a calendar retention guarantee. The
operator must set a jurisdiction-appropriate time policy, storage access
controls and deletion procedure before customer intake. Logs must not be
backed up or exported as a second personal-data repository by default.
Review sample logs for secrets/PII after each logging change. Docker
administrators can read raw container logs, so host access remains sensitive.

Migration failure emits only `MIGRATION_FAILED` or
`MIGRATION_CONFIG_INVALID` and a process exit status; inspect a restored
synthetic database under change control for SQL diagnostics. Its transaction
and advisory lock behavior are unchanged.

Metrics lack durable storage and no dashboard/notification is active; this
is an instrumentation foundation, not complete 24/7 operations. One API
process and one worker are assumed by this gate. Native Postgres logs are
not transformed to JSON and require separate operator-side parsing if a
collector is later approved. Gate 6 backup/restore, Gate 8 retention policy,
public DNS/ACME and production deployment remain separate.

## Local revalidation on the Gate 5 candidate

The locked package build and dependency check passed; Ruff and mypy passed
for all 18 application source files. The 87-test suite passed (two upstream
deprecation warnings only). Both Compose configurations and the Caddyfile
validated. A no-cache application image build installed the extractor at the
unchanged lock commit. The synthetic TLS smoke passed before and after
restarting Postgres, API, worker and Caddy, including extraction, tenant
isolation, health/readiness and persisted job retrieval. A synthetic header,
query and failed SQL canary did not appear in Caddy/Postgres logs; the public
`/metrics` route returned 404 and port 9100 was unreachable from the edge
network.

Trivy 0.74.0 scans of the locally built runtime images reported no CRITICAL,
HIGH or secrets findings. Residuals: application 16 MEDIUM/8 LOW; Caddy 5
MEDIUM/4 LOW/1 UNKNOWN; Postgres and migrator zero. These counts are a
point-in-time local result, not a waiver for future advisories. Gate 5 remains
conditional on the GitHub Actions run for the final commit succeeding.
