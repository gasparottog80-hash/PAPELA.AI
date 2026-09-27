# Gate 2 — actual CI execution evidence

Date: 2026-09-27. Scope: **secure CI foundation, PASS**. No production deployment.

## Verified source and run

- CI implementation commit: `a4ff9dc9801d195933fc9466b7f2c51ea7797507`.
- Normal push to `origin/master`, no force push; remote SHA independently checked.
- Workflow: `Reproducible baseline`, event `push`, branch `master`.
- Run: https://github.com/gasparottog80-hash/PAPELA.AI/actions/runs/36351642415
- Job: https://github.com/gasparottog80-hash/PAPELA.AI/actions/runs/36351642415/job/108711334804
- GitHub API and browser both confirmed `completed / success`; every step,
  including credential removal, synthetic-data cleanup and post steps, succeeded.
- Job duration shown in the GitHub UI: 47 seconds.

## Objective results

| Check | Observed result |
| --- | --- |
| Exclusive v2 agent / host identity | Step succeeded; both SHA256 assertions mandatory |
| `uv lock --check`, locked sync | Success; no lock/manifest mutation |
| Runner extractor provenance | PASS, version 0.1.0, installed commit equals lock |
| Docker build | Success, `--no-cache`, required BuildKit SSH forwarding |
| Runtime extractor provenance | PASS, version 0.1.0, installed commit equals lock |
| `ruff check .`, `mypy app` | Lint/typecheck step succeeded |
| Compose configs / isolated DB | Success; mandatory connection preflight |
| Pytest | `19 passed, 2 warnings in 1.64s` |
| JUnit completeness | `19 tests, zero skips/errors/failures` |
| `uv build` | Distribution build step succeeded |
| Fresh-image async E2E | Smoke PASS: health, auth, upload, worker, extractor, PDF purge |
| Cleanup | Agent removal and isolated Compose cleanup succeeded |
| Uploaded artifacts | GitHub API reports zero |

Runner, Docker builder, Docker runtime and `uv.lock` all report the identical
extractor commit: `ddb485ff76627f2e995b11d2b4d11325fc5628c9`. The version is
`0.1.0`. Both provenance outputs were inspected in the actual GitHub job logs.

## Credential and IP boundaries

The compromised original key was revoked; GitHub confirmed successful deletion
and an empty Deploy Key list before replacement. The v2 public key is registered
as `Read-only`, fingerprint:
`SHA256:SwxgjtF6Mzjf2/Bh7/5EfJmMR5Xiu8XlwYSRqnaUx7U`.
The user supplied its private half only to `PAPELA_EXTRACTOR_CI_SSH_KEY` and
confirmed clipboard cleanup. The workflow checks the agent's fingerprint and
never falls back to a personal identity. The private value was not read back.

The commit contains only public workflow/configuration/documentation and the
synthetic HTTP smoke script. No extractor code or private credential was added.
No image, distribution, private source cache or test artifact was published.
The Dockerfile's builder-only agent forwarding / tmpfs source cache are unchanged
from the audited Gate 1 implementation. Runtime images contain the proprietary
installed dependency and must never be published publicly.

No private-key block / credential-token patterns were found in the inspected
visible log portions. This was a targeted UI inspection, not an exhaustive raw
archive scan: archive export timed out and the raw-log URL was blocked by the
browser. Those limitations did not prevent confirmation of the run, every step,
provenance outputs, test count, smoke output or zero uploaded artifacts.

## Limits of this PASS

The `pull_request` trigger is configured; a separate real PR run was not performed.
Fork / Dependabot events fail before credentials and do not get private source.
Same-repository contributors must be trusted. The E2E uses synthetic PDF / fake
OCR with the real fiscal extractor; it does not validate production PaddleOCR,
multi-tenant authorization, deployment, backups, LGPD compliance or go-live.
Two existing dependency deprecation warnings remain non-failing.

This evidence documents the successful implementation commit above. Publishing
this evidence will trigger another push run; confirm the final remote HEAD and
that run's conclusion before final handoff. No workflow behavior is changed by
this documentation-only follow-up.
