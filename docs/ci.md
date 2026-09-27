# Gate 2 — secure CI foundation

Status at CI publication: **awaiting real GitHub Actions execution**. The v2
read-only Deploy Key is configured, and the repository secret's existence was
verified in GitHub. A YAML file or local validation is not Gate 2 PASS.

## Credential rotation — 2026-09-27

The user reported `papela_extractor_ci_ed25519` compromised. Never use it again.
Its Deploy Key fingerprint is
`SHA256:TswXackN4pzDVLvuE8upx4DdbQXfMk68YyfKcKKtOYk`; revocation was
confirmed in GitHub by the successful-deletion message and an empty key list.
No run or build may use that credential. A fresh, separate local key pair was
generated as `papela_extractor_ci_ed25519_v2`, fingerprint
`SHA256:SwxgjtF6Mzjf2/Bh7/5EfJmMR5Xiu8XlwYSRqnaUx7U`, without printing its
private contents. The replacement was then registered as
`PAPELA.AI GitHub Actions (read-only, v2)`; GitHub shows its matching fingerprint
and `Read-only`. The user manually registered the v2 private key as repository
secret `PAPELA_EXTRACTOR_CI_SSH_KEY` and cleared the clipboard. The successful
secret-add message and secret name were verified without reading the value.
Browser filesystem/clipboard isolation prevented safe automated transfer, so
no exposing workaround was used. The workflow verifies the v2 agent fingerprint
before accessing the extractor. Neither key belongs in Git, artifacts or logs.

## Trust boundary

`.github/workflows/ci.yml` runs on pushes to `master` and `pull_request`.
The GitHub token has only `contents: read`; checkout does not persist it.
Actions are pinned to full upstream commit hashes. Python 3.11 and uv 0.10.12
match Gate 1; installation uses `uv sync --locked --extra dev`. Docker uses the
committed lockfile, never a new pip resolution.

Only a **dedicated read-only Deploy Key** for `papela-fiscal-extractor` may be
stored as `PAPELA_EXTRACTOR_CI_SSH_KEY` in PAPELA.AI repository Actions secrets.
There is no fallback to a personal identity. The private key is supplied to
`ssh-add` over stdin with a 30-minute lifetime, never a file, Docker argument,
layer or log. BuildKit forwards
the agent socket; the agent is cleared/killed before tests. GitHub's public
Ed25519 host key is in `.github/ssh_known_hosts`; its official SHA256 fingerprint
is checked before use. Strict host checking is mandatory. The Dockerfile
independently pins GitHub's official host keys.

Fork and Dependabot PRs deliberately **fail** before credential loading. They
must not receive private extractor source or silently report a passed baseline.
Do not switch to `pull_request_target` to bypass this restriction. Repository
writers / same-repository PR authors must be trusted: modifying workflow/build
code can expose the secret and proprietary dependency. Read-only prevents
modification of the extractor, **not disclosure**. Review collaborator access
and workflow/Dockerfile changes. A maintainer must review an untrusted
contribution before adopting it into a trusted branch.

No private source cache, test artifact, distribution or Docker image is uploaded.
The runtime necessarily contains the installed proprietary package: never publish
it to a public registry. No production deployment is implemented.

## Checks

1. `uv lock --check`, locked dependency install and local extractor provenance.
2. Fresh Docker build (`--no-cache`, required BuildKit SSH mount) and independent
   runtime extractor commit/version check against the same `uv.lock`.
3. `ruff check .`, `mypy app` and both Compose configurations.
4. Isolated PostgreSQL in tmpfs; mandatory DB preflight, pytest and JUnit checks
   rejecting any skip, error, failure or empty test run.
5. `uv build` and smoke E2E on the **new image**, using synthetic PDF / fake OCR
   and the real extractor: health, auth, upload, asynchronous worker, extracted
   items and PDF deletion audit.
6. No manifest/lock mutation, diff check and unconditional cleanup of the isolated
   Compose stack / synthetic volume (never the regular development/production DB).

Compose passwords/API keys used here are synthetic fixtures, not real secrets.
Heavy OCR/PaddleOCR and production readiness remain separate gates.

## Local evidence — 2026-09-27

The four CI foundation files were validated locally before publication.
Actionlint 1.7.12 validated `ci.yml` successfully (shellcheck/pyflakes disabled;
this is workflow validation, not execution). Its official Windows release archive
was verified against upstream SHA256:
`6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9`.
Lock check, ruff, mypy (13 files), uv build and Compose config passed locally.
Pytest passed 19 tests with zero skips/errors/failures and two existing dependency
deprecation warnings. The new smoke script passed against the freshly revalidated
Gate 1 image, with a newly created isolated DB/volume; this was **not** a new CI
build nor a validation of the automation key. Synthetic containers/volume were
removed afterward. No personal key was used by this workflow or generated for CI.
Remote `master` was independently confirmed at Gate 1 commit
`c43ef1db4d3c044466a1bc0b32603882a6be9e37`.

## Future manual rotation / setup reference

Only generate a replacement with user authorization. This example refuses to
overwrite the currently configured v2 pair:

```powershell
$ciKeyPath = 'C:\Users\Gabriel\.ssh\papela_extractor_ci_ed25519_v2'
if ((Test-Path -LiteralPath $ciKeyPath) -or (Test-Path -LiteralPath "$ciKeyPath.pub")) {
    throw 'CI key path already exists; do not overwrite it.'
}
ssh-keygen -t ed25519 -C 'papela-ai-actions-readonly-v2' -f $ciKeyPath
```

For this unattended key, leave the passphrase empty (Enter twice).
The replacement pair already exists locally; do not run key generation again or
overwrite it. `papela_extractor_ci_ed25519_v2.pub` is **public**; register its line
in the private extractor repository → Settings → Deploy keys → Add deploy key.
Suggested title: `PAPELA.AI GitHub Actions (read-only, v2)`.
**Leave “Allow write access” unchecked.** Do not register it as a personal SSH
key on your account; never reuse `id_ed25519`.

`papela_extractor_ci_ed25519_v2` (without `.pub`) is **private**. Copy its complete
contents, including OpenSSH begin/end markers, directly to PAPELA.AI → Settings →
Secrets and variables → Actions → New repository secret:
`PAPELA_EXTRACTOR_CI_SSH_KEY`. Never paste it into chat, source files or logs.
You may use `Get-Content -Raw $ciKeyPath | Set-Clipboard` locally to avoid printing
it; clear the clipboard afterward. Protect the local private file and revoke/
rotate the Deploy Key if exposed; deploy keys do not automatically expire.

Host pin (public; no extra secret needed):

```text
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
```

Official references:

- https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys
- https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
- https://docs.github.com/en/actions/reference/security/secure-use

## Real execution acceptance

Inspect an actual `push` run on `master`. Record its URL, source SHA, all checks
and both extractor
commit outputs. Diagnose failures without disabling checks/changing the extractor
reference. Verify secret/IP hygiene in logs/artifacts and test a trusted
same-repository PR run. Gate 2 is PASS only with actual successful execution;
otherwise report FAIL/BLOCKED and its cause.
