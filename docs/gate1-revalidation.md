# Gate 1 — revalidation

Status: PASS (2026-09-27). Validates fake OCR with the real private extractor;
does not certify real PaddleOCR models or production readiness. Gate 2 has not
started. No commit or push has been performed.

## Extractor identity

| Source | Installed/resolved commit | Version |
| --- | --- | --- |
| Local environment, direct_url.json | `ddb485ff76627f2e995b11d2b4d11325fc5628c9` | `0.1.0` |
| Fresh Docker image, direct_url.json | `ddb485ff76627f2e995b11d2b4d11325fc5628c9` | `0.1.0` |
| uv.lock Git source | `ddb485ff76627f2e995b11d2b4d11325fc5628c9` | `0.1.0` |

All three commits are identical. The lockfile SHA-256 is identical locally and
inside the image, and unchanged throughout this revalidation:
`dbcdb5bb6bb15dcc7d0df4a53eacf9851131e501831229256cf6b98451b09283`.

A read-only remote query of the private repository returned:

```text
2746d39c395d2c09849f938a4cee0fc4d8208358 refs/tags/v0.1.0
ddb485ff76627f2e995b11d2b4d11325fc5628c9 refs/tags/v0.1.0^{}
```

The local private repository independently confirmed `cat-file -t 2746d39...`
is `tag` and `rev-parse 2746d39...^{commit}` is `ddb485ff...`.
This is an annotated tag: its object SHA and peeled commit describe the same
release. The previous divergence diagnosis confused two Git object types.
These checks provide no evidence of a moved tag; they do not establish a full
historical audit of tag changes. No private source, refs or history were changed.

## Installation change

Docker copies uv.lock and runs `uv sync --locked --no-dev --no-editable`.
Unlike `--frozen` alone, `--locked` checks manifest consistency and rejects
required lockfile updates. The application is installed in `/app/.venv`,
which supplies runtime Python and uvicorn. Python and uv images are pinned by
digest; uv is version 0.10.12.

BuildKit SSH forwarding is required only in the builder. SSH verification uses
the existing pinned GitHub host keys with `StrictHostKeyChecking=yes`. The Git
checkout and uv cache use a temporary mount. The final stage receives only the
installed environment, lockfile and provenance checker from the builder.
It runs as appuser (UID 10001).

Other Gate 1 changes add lint/typecheck tools and mechanical Python style/type
corrections. The lockfile delta from HEAD adds six quality-tool packages; no
existing dependency version or extractor Git source changed.

## Executed checks

| Check | Evidence |
| --- | --- |
| uv lock --check | PASS; 93 packages; lockfile unchanged |
| uv sync --locked --extra dev | PASS |
| uv run --locked --extra dev ruff check . | PASS |
| uv run --locked --extra dev mypy app | PASS; 13 files, zero issues |
| uv run --locked --extra dev pytest -ra | PASS; 19 passed, zero skips, two existing deprecation warnings |
| uv build | PASS; wheel and sdist |
| docker compose config --quiet | PASS |
| docker compose -f compose.gate1.yml config --quiet | PASS |
| docker build --no-cache --pull --ssh default --progress plain -t papelaai-gate1-revalidated . | PASS; final Dockerfile rebuilt without application-layer cache |
| docker run --rm --network none papelaai-gate1-revalidated python scripts/verify_extractor.py | PASS; installed version/commit match lock |
| Inconsistent-manifest negative test | PASS; temporary project version changed to 0.1.1; uv sync --locked --no-dev --no-editable --dry-run exited 1, explicitly requiring a lockfile update; lock hash unchanged |
| git diff --check | PASS |

Final image ID:
`sha256:c3cbbaa3546ff8134db0d51abd5b70593236a3d11557fc8b7756e310b5a77bce`.
Created: `2026-09-27T20:11:47.713192002Z`.
API and worker both used this image ID for the E2E.

## Isolated smoke/E2E

compose.gate1.yml uses a separate project, disposable Postgres on tmpfs,
loopback-only ports 55433/18077 and a dedicated PDF volume. Its credentials are
explicit synthetic test values. Existing application containers/databases were
not used as evidence and were not modified.

Postgres was started alone while pytest ran with
`PAPELA_DATABASE_URL=postgresql://gate1:gate1-test-only@127.0.0.1:55433/gate1`.
This confines the existing tests' TRUNCATE jobs to the disposable database.
The worker was started after pytest completed to avoid queue races.

The smoke test sent a generated one-page blank PDF through the newly built API
and independently running worker:

- /health: HTTP 200, status ok.
- Protected request without key: HTTP 401.
- Synthetic upload: HTTP 202.
- Polling job: done, one page, schema version 3, fake OCR.
- Real private extractor invoked; existing synthetic fixture produced two items.
- Raw PDF removed; audit reports file_exists=false and a purge timestamp.
- Job response contains x-request-id.

## Secrets and proprietary-source inspection

- docker history --no-trunc: zero matches for private-key markers, developer
  key filenames, API-key injection or GitHub-token patterns.
- Image configuration: zero credential-related environment entries.
- Exported final image: 9 layers, 6,925 regular-file entries inspected;
  zero private-key blocks, GitHub-token patterns, .ssh, .env, developer key
  filenames, .git or uv checkout-cache paths found.
- Runtime filesystem: SSH/Git binaries and uv cache absent;
  Python executable is /app/.venv/bin/python.
- Public wheel and sdist do not vendor the private extractor package. The
  runtime image necessarily installs the private dependency and must remain
  private; it was not pushed or uploaded.
- Diff review found no private source, private fiscal rules, real API keys or
  token/private-key patterns. Public SSH host keys and synthetic test credentials
  are not secrets.

These are bounded inspections, not a guarantee for arbitrary unknown secret
formats. The private key itself was never read or copied; local authentication
used the existing SSH agent through temporary forwarding.

## Pending checkpoint

Review the staged diff before committing. Proposed message:
`chore: establish reproducible quality baseline`.
This checkpoint stops before creating the commit; Gate 2 has not started.
