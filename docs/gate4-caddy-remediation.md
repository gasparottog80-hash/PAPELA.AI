# Gate 4 — Caddy remediation evidence

Scope: the Internet-facing reverse proxy only. This is a local/CI candidate,
not permission to deploy it or a claim that PAPELA.AI is ready for customers.
The proprietary extractor source and all private SSH keys remain outside this
repository and outside the Caddy build.

## Baseline and decision

On 2026-10-02, Trivy 0.74.0 (image
`aquasec/trivy@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969`)
scanned the locally built image `papela-caddy:gate4` ID
`sha256:968a332647b19715885aaec60b70869c8beebad6c240720e5521f09850330170`.
Its official base was
`caddy:2.11.4-alpine@sha256:6aeddd44c3078b0f9a35206472a11420648a79c184603ef95957d0a20044cb2b`.
The Trivy vulnerability DB reported `UpdatedAt=2026-10-02T01:05:41Z`,
`DownloadedAt=2026-10-02T02:16:27Z`. Result: 17 HIGH findings in
`/usr/bin/caddy`, 0 CRITICAL, 0 HIGH Alpine packages, 0 detected secrets.
There are 16 distinct advisory IDs because CVE-2026-46600 appears in two
component records. These are not discarded as false positives.

The latest stable official Caddy release checked was 2.11.4. The 17 findings
are embedded in that release's Go executable, not Alpine packages; changing
only the runtime base would leave that executable unchanged. The chosen
mitigation recompiles the unmodified official `v2.11.4` Go module with
patched dependencies and Go 1.26.8, then copies only the static executable,
CA trust bundle and Caddyfile into `scratch`. No plugins or extra runtime
tools are added. The Caddy source itself is not forked or patched.

Every item below was reported in the final-filesystem Caddy executable.
"Reachability" is a conservative configuration review, not proof that the
package is unreachable: the external TLS/HTTP boundary is treated as exposed.
All listed fixes were applied via the pinned Go build, irrespective of the
reachability assessment.

| Advisory | Component: old -> fixed build | Runtime reachability assessment |
| --- | --- | --- |
| CVE-2026-56854 | `x/crypto` 0.52.0 -> 0.55.0 (fix 0.55.0) | SSH server is not configured; module present in binary, path not proven unreachable. |
| CVE-2026-46600 | `x/net` 0.55.0 -> 0.58.0 (fix 0.56.0) | DNS parsing could be exercised by outbound name resolution; treat as potentially reachable. |
| CVE-2026-56852 | `x/text` 0.37.0 -> 0.41.0 (fix 0.39.0) | Untrusted text/host processing crosses the proxy; potentially reachable. |
| CVE-2026-84304 | gRPC 1.81.0 -> 1.83.2 (fix 1.83.1) | No gRPC service/OTLP exporter configured; binary includes module, so no waiver. |
| CVE-2026-84445 | gRPC 1.81.0 -> 1.83.2 (fix 1.83.2 on this branch) | No gRPC listener configured; malformed RPC path not demonstrated, but fixed. |
| GHSA-hrxh-6v49-42gf | gRPC 1.81.0 -> 1.83.2 (fix 1.82.1) | xDS RBAC is not configured; HTTP/2 exposure still warrants patch. |
| CVE-2026-27145 | Go stdlib 1.26.3 -> 1.26.8 (fix 1.26.4) | `crypto/x509` parses TLS certificates; potentially reachable. |
| CVE-2026-33818 | Go stdlib 1.26.3 -> 1.26.8 (fix 1.26.6) | ASN.1 may process certificates; potentially reachable. |
| CVE-2026-39821 | Go stdlib 1.26.3 -> 1.26.8 (fix 1.26.6) | HTTP/IDNA host handling is proxy-facing; potentially reachable. |
| CVE-2026-39822 | Go stdlib 1.26.3 -> 1.26.8 (fix 1.26.5) | `os.Root` traversal path not shown in this reverse-proxy config; fixed anyway. |
| CVE-2026-42504 | Go stdlib 1.26.3 -> 1.26.8 (fix 1.26.4) | MIME/header parsing may process untrusted requests; potentially reachable. |
| CVE-2026-46600 | Go stdlib 1.26.3 -> 1.26.8 (fix 1.26.6) | Duplicate scanner record for DNS parsing; potentially reachable. |
| CVE-2026-56853 | Go stdlib 1.26.3 -> 1.26.8 (fix 1.26.6) | h2c is not configured; TLS HTTP/2 remains exposed, so patch not waived. |
| CVE-2026-56858 | Go stdlib 1.26.3 -> 1.26.8 (fix 1.26.6) | `html/template` use is not shown in configured proxy route; fixed anyway. |
| CVE-2026-56859 | Go stdlib 1.26.3 -> 1.26.8 (fix 1.26.6) | XML parser use is not shown in configured proxy route; fixed anyway. |
| CVE-2026-56860 | Go stdlib 1.26.3 -> 1.26.8 (fix 1.26.6) | URL parsing is proxy-facing; potentially reachable. |
| CVE-2026-56862 | Go stdlib 1.26.3 -> 1.26.8 (fix 1.26.6) | TLS termination is directly exposed; potentially reachable. |

## Supply chain and final image

`Dockerfile.caddy` pins the official Go builder
`golang:1.26.8-alpine3.23@sha256:a8fa79c5bd40d880b52bd3b6d7669ecdcfd00e85facdd427d279efb5ddd79cb1`.
`ops/caddy/go.mod` pins Caddy 2.11.4, `x/crypto` 0.55.0, `x/net`
0.58.0, `x/text` 0.41.0 and gRPC 1.83.2. The committed `go.sum` includes
the official Caddy module checksum
`h1:XKxkMTgNSizEvKG6QHue6cAsFOteU2qA61w2tKkCWi0=`. `go mod verify`
passed during a no-cache build; `go build -mod=readonly` rejects required
manifest changes. `CustomVersion=v2.11.4` supplies the version label when
building from a separate pinned Go module; it is not the source-integrity
control. No private Git dependency, SSH forwarding or credential is used in
this Caddy build.

The locally rebuilt final image `papela-caddy:gate4` has ID
`sha256:81ff3a6c0b82e2ddce8411e6b63e57199914d4046837a01ebd13807b9f2d4095`.
It reports `v2.11.4`, runs as UID/GID 10001, and has CA certificates for
upstream TLS and ACME. Its exported filesystem has 22 paths (including
Docker-generated `/proc`, `/sys`, `/dev` and host config files). The actual
image payload contains `/usr/bin/caddy`, `/etc/caddy/Caddyfile`,
`/etc/ssl/certs/ca-certificates.crt`, plus writable `/data` and `/config`
mountpoints. There is no shell, compiler, Git, SSH client, curl or package
manager. A `/bin/sh` launch fails because the file does not exist. Full
`docker history` had 11 entries and zero matches for known private-key/token
markers; Trivy found zero secrets. This is a bounded inspection, not a
guarantee against unknown secret formats.

The final all-severity Trivy scan against the same DB found **0 CRITICAL,
0 HIGH, 5 MEDIUM, 4 LOW, 1 UNKNOWN, 0 secrets**. Remaining MEDIUM items:
`GHSA-gcjh-h69q-9w9g` (cel-go), `CVE-2026-81871` (OTLP log gRPC),
`CVE-2026-81872` (OTel SDK log), `CVE-2026-56855` and `CVE-2026-78662`
(`x/crypto`). LOW: four records for `CVE-2026-81870` across OTel components.
UNKNOWN: `GO-2026-5932` (`x/crypto`, no fixed version in this DB). These
remain tracked and must be reevaluated when updating Caddy or the scanner DB.

The Compose file uses a locally built image with `pull_policy: never`; a
portable, registry-backed image digest cannot be pinned in Compose before a
reviewed private registry publication, which this gate does not authorize.
The builder base is digest-pinned. For a future deployment, build on the
approved target platform, scan the **final** image, record its immutable ID,
publish privately under change control, then pin the registry manifest digest
in the deployment manifest. Never substitute `latest`, and never roll back to
the known-vulnerable official Caddy binary merely to restore availability.

## Functional evidence and current boundary

The `Caddyfile.prod` and production Compose topology are unchanged. In a
new localhost-only synthetic stack, Caddy configuration validated, the two
migration runs succeeded, all four services became healthy, and only Caddy
published `127.0.0.1:18080/18443`. HTTP returned 308 to HTTPS. A TLS client
verified the local CA and exercised `/health`, `/readiness`, document
extraction, cross-tenant isolation and job persistence before/after restarting
Postgres, API, worker and Caddy. A 23 MiB upload returned 413 against the
22 MiB proxy limit. `caddy_data` and `caddy_config` persisted across restart.
This tests local automatic TLS only; public ACME issuance/renewal, DNS and
firewall remain untested and require a separate operator decision.

Following separate user authorization, the application was updated to
`pypdf` 6.19.0 without changing the private extractor revision. Fresh
no-cache runtime builds of the application, Caddy, Postgres and migrator
each scanned at zero CRITICAL/HIGH and zero secrets. The local synthetic
smoke was repeated successfully on the fresh images, including restart
persistence. Gate 4 still requires green CI on the exact pushed commit;
local evidence alone is not sufficient. The application advisory details
and residual MEDIUM/LOW records are tracked in
[the Gate 4 production record](gate4-production.md).
