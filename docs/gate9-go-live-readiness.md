# Gate 9 — go-live readiness (decision record)

Date: 2026-10-04. Decision: **BLOCKED / NO-GO for customer data**. See
`docs/production-enablement.md` for subsequent local-only preparations. This
is an audit and a future deployment plan, not permission to provision, publish,
deploy, process customer documents, or claim legal compliance. Gates 1–8
passed their stated local/CI scopes; that is not evidence of a live service.
The reviewed baseline is `0feaf1cbcf7255d00a1beecc46e459d9f1637a46`,
whose [GitHub Actions run](https://github.com/gasparottog80-hash/PAPELA.AI/actions/runs/37244972044)
completed successfully. No production host, DNS, TLS certificate, registry
manifest, offsite backup, real alert receiver or customer tenant was verified.

Priority for decisions: security > recoverability > operation > evidence >
convenience. `BLOCKER` prevents a controlled pilot. `HIGH` requires an owner,
mitigation and explicit acceptance before a pilot; `MEDIUM`/`LOW` require
tracking. `ACCEPTED RISK` below is only a *proposed* bounded acceptance, not
the customer's or legal team's approval. An unproven control is not PASS.

## Baseline and inherited gates

| Gate | Evidence | Production-relevant residual | Pilot blocker? |
| --- | --- | --- | --- |
| 1 reproducibility | Locked extractor, build, tests and synthetic E2E in [Gate 1](gate1-revalidation.md) | Fake OCR evidence; production accepts selectable-text PDFs only | No, if the input contract states this limit |
| 2 secure CI | Exclusive read-only extractor identity and real green Actions in [Gate 2](gate2-validation.md) | CI does not publish a production artifact | Yes, until a controlled release path exists |
| 3 security | Tenant-scoped job access, bounded input and zero known CRITICAL/HIGH runtime scan in [Gate 3](gate3-security.md) | Manual provisioning; native OCR excluded; scans are point-in-time | No, with a verified tenant procedure and text-PDF contract |
| 4 topology | Local TLS, health, restart, tenant and upload smoke in [Gate 4](gate4-production.md) | No real VPS, DNS, ACME, firewall or public-path test | Yes |
| 5 observability | Safe logs, correlation and process-local metrics in [Gate 5](gate5-observability.md) | No durable metrics, receiver or delivered alert | Yes |
| 6 backup | Restricted local archive, checksum and isolated restore in [Gate 6](gate6-backup-restore.md) | No encrypted offsite copy or offsite restore | Yes |
| 7 release | Local A→B→A rollback and CI check in [Gate 7](gate7-deploy-rollback.md) | Image ID is not a registry manifest digest; no host drill | Yes |
| 8 erasure | Journal, reconciliation and synthetic compaction in [Gate 8](gate8-data-lifecycle.md) | Independent journal backup/offsite proof and legal retention unresolved; production compaction disabled | Yes until recovery and policy are set |

The baseline's CI run covered locked install, no-cache Docker build, scans,
lint/typecheck, tests, package build, secret scan, synthetic E2E and Gate
4/6/7/8 regressions. It did not perform the real-host checks in this record.

## Final GO/NO-GO matrix

`Owner` is the accountable role to assign to a named person before execution.
No row asserts that a provider or person has been selected. The pilot column
assumes **one controlled, contractually scoped customer**; broad sale has a
higher support/commercial threshold.

| Item | Status / evidence now | Severity | Owner | Action and acceptance evidence | Blocks pilot? | Blocks broad sale? |
| --- | --- | --- | --- | --- | --- | --- |
| Supported VPS, disk and load | None provisioned or benchmarked | BLOCKER | Infra | Select supported Linux, capacity-test text-PDF workload, record sizing and failure-domain risk | Yes | Yes |
| Host hardening/patches | Checklist only | BLOCKER | Security/Infra | Verify SSH, firewall, time sync, patch cadence and port audit on host | Yes | Yes |
| DNS, real TLS/ACME | Local CA smoke only | BLOCKER | Infra | Verify public DNS, certificate issuance/renewal, redirect, TLS expiry alert | Yes | Yes |
| Production secrets and escrow | Synthetic files only | BLOCKER | Security | Create independent real values outside Git, test file modes, recovery and revocation | Yes | Yes |
| Private registry artifact | Only local image ID; `registry_digest=null` | BLOCKER | Release | Private GHCR package, SHA tag, scan-before-push, capture real digest, deploy/pin by digest | Yes | Yes |
| Digest-aware release controller | `ops/release.py` verifies local tag/image ID, not a pulled manifest digest | BLOCKER | Engineering | Implement/test remote digest recording, pull and exact running-image verification; retain local drill | Yes | Yes |
| Production scans | CI scans candidate, not a published production digest | BLOCKER | Security/Release | Re-scan exact published artifact and all runtime images before promotion; 0 CRITICAL/HIGH/secrets | Yes | Yes |
| Offsite DB backup + restore | Local/CI drill only | BLOCKER | Infra | Encrypted independent copy, checksum/receipt, independent credential/key recovery and isolated restore | Yes | Yes |
| Independent journal backup | Journal is excluded from Postgres dumps; no live copy/recovery | BLOCKER | Infra/Privacy | Capture complete journal/index separately with matching horizon; restore before DB reconciliation | Yes | Yes |
| Journal/inventory topology | Journal bind exists; API/worker lack backup-root read mount | BLOCKER | Engineering/Infra | Design least-privilege consistent inventory view; independently back up journal and enforce restore floor | Yes | Yes |
| Real backup inventory | Synthetic/checksummed, not authenticated; no provider receipt | BLOCKER | Infra/Privacy | Account for all local/offsite copies, holds and deletion receipts; reconcile before readiness | Yes | Yes |
| Production journal compaction | Mutating command intentionally test-only | ACCEPTED RISK proposed | Privacy/Infra | Keep disabled; retain all markers, monitor size, back up independently; enable only after audited offsite coverage | No, if unlimited retention/capacity accepted | Yes without long-term policy |
| Legal retention/deletion schedule | Technical defaults, not legal decisions | BLOCKER | Controller/Legal | Approve per-class periods, holds, deletion evidence and restore exception process | Yes | Yes |
| Age-based log retention | Docker `json-file` has size/count limits only | BLOCKER | Privacy/Infra | Approve days, incident hold and safe collector/journald deletion; test expiry without touching Docker-owned files | Yes | Yes |
| Active alerting | Instruments exist; no receiver or on-call | BLOCKER | Operations | Install independent uptime path and tested delivery/escalation; run failure drills | Yes | Yes |
| Durable metrics/queue visibility | In-process metrics reset; no collector | HIGH | Operations | Restricted scrape/aggregation, bounded retention; alert on stuck jobs and DB/host pressure | Yes | Yes |
| Disk and Postgres monitoring | Local health checks only; no live pressure/history alerts | BLOCKER | Operations | Alert on disk/inodes, backup space, DB availability/connections and queue age | Yes | Yes |
| Backup/restore freshness | No scheduler/offsite drill schedule | BLOCKER | Operations | Daily verified copies, missed-run alarms and periodic independent restore proof | Yes | Yes |
| Real host deploy/rollback | Only localhost A→B→A | BLOCKER | Release/Infra | Execute approved host drill below, including recovery from failed migration | Yes | Yes |
| Cross-schema rollback | Compatible schema policy; older pre-Gate-8 schema not drilled | BLOCKER | Engineering/DBA | Separate reviewed migration/rollback plan and isolated older-schema restore drill before such upgrade | Yes for a schema-changing rollout | Yes |
| Single VPS | Topology has one host/failure domain | ACCEPTED RISK proposed | Business/Operations | Publish bounded pilot availability/RPO/RTO; require working offsite restore; revisit HA before SLA | No if approved | Yes for unqualified HA claims |
| Text-only PDF input | Runtime rejects image-only/scanned PDFs | ACCEPTED RISK proposed | Product/Customer | Make limitation explicit in pilot contract/API docs and test representative customer samples | No if approved | Yes for unrestricted OCR claims |
| Native OCR extra | Known advisories; excluded from runtime | HIGH | Engineering/Security | Do not enable without patched isolated design and separate accuracy/security gate | No for text-only pilot | Yes for OCR product claims |
| Tenant provisioning/offboarding | Operator CLI and secret-file procedure; no live rehearsal | BLOCKER | Operations/Security | Dual-control UUID/key issue, safe delivery, revocation, offboarding/export and recovery rehearsal | Yes | Yes |
| Incident response | No named responder, rota or exercise | BLOCKER | Security/Operations/Legal | Define triage, containment, evidence, communication and restore decision; tabletop test | Yes | Yes |
| Secret rotation/revocation | Procedures exist only for synthetic tenant keys | BLOCKER | Security/Operations | Rehearse DB, tenant, deploy and offsite credential replacement with old-value rejection | Yes | Yes |
| Privacy roles/notices/contracts | No approved controller/operator allocation, notice or DPA | BLOCKER | Legal/Business | Determine roles, lawful basis, notice, subprocessors, hosting, DPA/terms and titular process | Yes | Yes |
| Commercial operations | Pricing, billing, support and SLA not evidenced | HIGH | Business | Decide support channel, limits, pricing, contract, invoice and service promise | No for approved noncommercial pilot | Yes |
| Residual MEDIUM/LOW CVEs | Point-in-time Gate 8 scans; no known CRITICAL/HIGH | MEDIUM | Security | Re-scan at publish/deploy; track fixes and exposure; no waiver for new severe CVE | No if reviewed | No if reviewed |

No actual risk acceptance is recorded here. A named owner must sign each
proposed acceptance with scope, expiry, monitoring and rollback trigger.
Public source, CI logs or artifacts must never contain the proprietary
extractor source, an SSH private key or a production image made public.

## Minimum real infrastructure (not provisioned)

One supported 64-bit Linux VPS (initial candidate: Ubuntu 24.04 LTS),
maintained Docker Engine plus Compose plugin, 4 vCPU, 8 GiB RAM and 160 GiB
encrypted SSD is a **planning floor**, not measured capacity. The Compose
service memory limits sum to about 4.75 GiB before host, page cache, Docker,
migrator and backup overhead. Reserve at least 25% free RAM/disk in steady
state and measure with representative text PDFs, worst-case concurrency and
backup/restore. A 2–4 GiB encrypted swapfile may absorb transients; swapping
must not be used to claim sufficient processing capacity. Set independent
disk alerts at 70/80/90%; deny uploads safely before the private PDF quota
or filesystem is exhausted.

Disk budgeting is explicit: OS/images + Postgres growth + `PAPELA_MAX_STORAGE_BYTES`
+ at least two verified local DB archives + independent journal copies +
25% headroom. If this exceeds 160 GiB, resize before deployment. Backups on
the same VPS are only a fast local copy, never the offsite recovery copy.
No multi-host HA or zero-downtime claim is made.

Topology: public TCP 80/443 → Caddy → private API; worker and API → internal
Postgres; PDF volume private; separate restricted journal and backup mounts;
offsite encrypted storage in another failure domain. Keep TCP 5432, 8000 and
metrics 9100 unexposed. SSH should be key-only from approved management
sources with an out-of-band console recovery path. Verify DNS A/AAAA and
ACME from the public Internet, not just a local CA.

Mandatory host checklist before any real secret is placed:

1. Non-root operator with sudo audit; SSH key-only, `PasswordAuthentication no`,
   `PermitRootLogin no` after a second verified session and provider-console
   recovery test. Restrict SSH source addresses if feasible; no Docker socket
   access for untrusted accounts (Docker group is effectively root).
2. Default-deny inbound firewall; allow only approved SSH and TCP 80/443;
   validate IPv4/IPv6 and `docker compose ps`/host port audit. Postgres/API/
   worker/metrics stay private. Do not enable a remote firewall blindly.
3. Supported OS security updates, scheduled patch window/reboot plan,
   time-sync, disk/inode alerts and least-privilege host files. Consider
   fail2ban only if SSH exposure and alert data justify it; it is not a
   substitute for key-only SSH and source restrictions.
4. External secret and release-state dirs `0700`; files `0400`/`0600` with
   the UID required by file-backed Compose mounts. Verify backups, journal,
   inventory and ACME key permissions, encryption and independent escrow.
5. Record Docker/Compose/OS versions, firewall rules, patch owner, disk
   baseline, open-port scan, backup schedule and alert delivery evidence.

## Private registry and immutable release

Preferred candidate: **private** `ghcr.io/gasparottog80-hash/papela-ai`
(exact package name/access to be approved). GitHub currently describes
Container registry image storage/bandwidth as free, subject to future policy
change; confirm account billing/visibility before first push. The public
PAPELA.AI repository does **not** make a private runtime image safe to
publish publicly: its installed dependency contains proprietary code.
Restrict package read to deploy identity and designated maintainers.

Future release sequence, requiring separate authorization: CI succeeds on
exact `master` SHA; build once from locked sources with the CI-only read-only
extractor key; verify extractor commit; scan **all** final images and secrets;
then a manually approved publish job with only `packages:write` sends the
private image tagged `sha-<40 hex commit>`. Record the **registry manifest**
digest returned by the push, verify repository/tag/digest and package
visibility, and sign/attest provenance if adopted. Production pulls only
`ghcr.io/gasparottog80-hash/papela-ai@sha256:<manifest digest>` using a
read-only pull identity. Tags are discovery aids, not deployment identities.
Keep active/previous digests. A Docker image ID/config digest is not the
registry manifest digest. The later Production Enablement changes add
digest-aware release validation and a manual-only publication workflow, but
neither a real private package nor a pushed manifest has been verified;
manually setting `registry_digest` would still be false evidence. Do not make
an automatic publish trigger from a public PR or expose the runtime image as a workflow
artifact. [GitHub's registry permissions](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
and [billing policy](https://docs.github.com/en/billing/concepts/product-billing/github-packages)
must be rechecked at authorization time.

## Production secret inventory (none generated here)

| Secret | Origin / storage | Consumer | Rotation, revocation and recovery |
| --- | --- | --- | --- |
| PostgreSQL owner password | Independently generated; restricted external file and separate encrypted escrow | Postgres, migrator, backup | Rotate in maintenance window, update all consumers, revoke old value; restore escrow tested |
| PostgreSQL runtime password + URL | Separate value; UID-65532 file, no shell argv or Git | API/worker | Coordinate DB role rotation and atomic file replacement/recreate; test old value rejected |
| Tenant UUID→API-key map | Unique high-entropy keys; restricted external file | API only | Dual-control issue/secure delivery; rotate/revoke with Gate 8 journal actions; recover map separately |
| Synthetic canary key | Separate restricted file, dedicated tenant | Release smoke | Rotate/remove with tenant map; never use a customer key |
| GHCR pull credential | Scoped read-only deploy identity or equivalent, outside Git | Approved host | Revoke on compromise, rotate and re-pull known digest; no package write on host |
| CI extractor Deploy Key | Existing exclusive read-only v2 GitHub secret | CI build only | Revoke key/secret together, create replacement; never put on VPS/image |
| Offsite backup credential | Dedicated prefix-scoped service identity | Backup/sync operator | Rotate independently, deny/delete only if policy requires, test recovery; never reuse host SSH identity |
| Offsite encryption recovery key | Independent recipient/KMS identity in separate escrow | Restore operators | Two-person recovery exercise; key loss makes copies unrecoverable |
| Host SSH identity | Approved operator key and authorized_keys inventory | SSH | Remove departed users promptly, test emergency console path |
| ACME account/certificate keys | Caddy private volume/backup or reissuance procedure | Caddy | Restrict, renew automatically, test replacement and expiry alarm |

There is **no** implemented journal/inventory MAC/signing secret. Current
SHA-256 detects accidental corruption, not malicious rewriting. Do not
invent a value or claim authenticity. Host root and backup operators are
trusted; if that threat model is unacceptable, add authenticated inventory
and independent key management before intake.

## Offsite backup, erasure and restore design

Before traffic, select a private offsite provider and failure domain. On a
schedule: create a Gate 6 custom archive; verify SHA-256 and `pg_restore`
TOC; take a consistency-bound copy of the **independent** journal/index and
inventory (not merely a Postgres volume snapshot); encrypt client-side or
with recoverable KMS keys; upload archive plus metadata to a private prefix;
verify remote checksum and object listing/receipt; atomically record the
remote copy in an operator-controlled inventory. Use least-privilege upload
credentials and separate read/recovery identity. Record creation time,
privacy generation/horizon, expiry, hold and physical deletion receipt.
Back up the journal/inventory at least as durably and for longer than every
restorable DB archive. Never delete an older journal needed for replay.

Daily local/offsite backup and a weekly isolated offsite restore are starting
drill frequencies, not approved RPO/RTO or a contractual SLA. Host loss has
no proven RPO/RTO until an independently downloaded offsite archive and
journal are restored into an isolated host/DB, reconciled, and a synthetic
job passes. Alert if daily success exceeds 26 hours or the weekly restore
misses its approved window; also alert on failed verification/upload,
unlisted copy and low storage. Practice key recovery.
Do not silently mark copies deleted on calendar expiry; offsite receipts and
legal holds must be independently accounted for.

At present API/worker see the journal bind but **not** the backup root. The
Gate 8 inventory validator rehashes local `present` archives; simply
publishing the inventory in production would make readiness 503. Design a
read-only, least-privilege inventory/backup view or a separately attested
coverage protocol, then test it with the real storage layout. On restore,
stop writers, restore the complete independent journal first, restore DB to
a *new* target, enforce `floor_generation`, reconcile all retained markers,
then allow readiness. Missing/corrupt/unlisted/offsite-uncertain inventory
fails closed. Production **mutating compaction stays disabled** for the pilot;
that is conditionally acceptable only if the journal is backed up, monitored
for capacity and retained indefinitely until a separately approved legal
and offsite-aware deletion design is proven. Never switch `PAPELA_ENV=test`
on a production host to bypass the guard.

## Observability and log lifecycle

Before traffic, assign an on-call owner and an independent external HTTPS
probe so host loss is observable. Minimum delivered alerts: public uptime,
`/readiness`, worker heartbeat, 5xx rate, oldest pending/processing job,
disk/inodes, Postgres health, backup age/failure, offsite receipt age,
restore-drill age, TLS expiry and host CPU/memory pressure. A receiver must
acknowledge synthetic failure tests. Process-local metrics reset on restart;
a restricted collector with bounded retention is required for trends and
alerting. Keep tenant/job/request IDs, documents and credentials out of
metric labels and third-party sinks. A same-host dashboard alone cannot
detect whole-host outage.

Docker `json-file` currently rotates by `max-size`/`max-file`, **not days**.
Do not `find -delete` or truncate Docker-owned active log files; Docker warns
that external access can interfere with the daemon. Obtain a legal period
for each log class, maximum disk budget, access list and incident-hold
procedure. A later local-only journald overlay and host-policy renderer are
documented in `docs/production-enablement.md`; the actual Linux host policy,
driver behavior and verified expiry drill remain untested. Until deployed,
age retention is a pilot blocker. Never forward raw container logs to an
external service without canary-based redaction review.
[Docker's json-file guidance](https://docs.docker.com/engine/logging/drivers/json-file/)
documents only the size/count rotation and warns against external edits.

## Controlled real-host drill — future approval only

Stop at every failed check; preserve old image, DB, journal and backups.
Never `down --volumes`, overwrite the production DB or force a rollback.

1. Select/provision supported host; establish out-of-band recovery.
2. Harden and verify SSH, firewall, patches, disk and open ports.
3. Point approved DNS; verify A/AAAA and public reachability.
4. Install pinned/maintained Docker Engine and Compose; record versions.
5. Create external restricted secrets and tested independent escrow.
6. Mount journal and restricted inventory/backup view separately; validate
   permissions, generation and fail-closed behavior.
7. Configure encrypted offsite destination, independent credentials and
   verified journal/archive copy path.
8. Pull the scanned private release **by manifest digest**; verify provenance
   and local image ID mapping, with active/previous digests retained.
9. Take and verify a baseline backup; on a fresh empty installation record
   the initial state before migration.
10. Run the reviewed one-shot migration; require explicit schema policy.
11. Start Postgres, API, worker and Caddy without exposing internal ports.
12. Verify internal and public `/health`/`readiness`, then real ACME/TLS,
    redirect, certificate chain and renewal path.
13. Run synthetic API extraction and negative auth/tenant-isolation tests.
14. Exercise bounded synthetic upload, results, source-PDF purge and restart
    persistence. Never use a real customer document as the canary.
15. Create a fresh backup, verify and upload it; confirm remote checksum,
    inventory coverage and alert receipt.
16. Download the offsite archive/journal with recovery credentials into an
    isolated host/DB; restore and reconcile; confirm deleted synthetic job
    does not reappear and `restore_floor` rejects older snapshots.
17. Execute same-schema A→B→A rollback with real digest pins; for any schema
    change use the separately approved backward-compatibility or full
    restore drill. Verify active SHA/digest, API/worker image and schema.
18. Trigger and acknowledge every critical alert; inspect safe logs, disk
    retention and certificate-expiry monitoring.
19. Record evidence, named owners and explicit GO/NO-GO; only a separate
    authorization may admit a controlled customer pilot.

## Product, legal and commercial boundaries

**Internal production-ready** requires all technical blockers above, a
successful host/offsite/alert drill with synthetic data, measured capacity,
named responders and a maintained patch/recovery process. **One controlled
pilot** additionally requires approved processing roles, lawful basis,
notice/terms/DPA as applicable, contractual retention and exceptions,
subprocessor/hosting disclosure, a tested tenant issue/offboard flow,
secure customer-key delivery, text-only input agreement and incident contact.
**Broad sale** additionally requires repeatable onboarding/offboarding,
support coverage, published API/limits, pricing/billing, enforceable SLA/SLO
and capacity/HA claims backed by evidence. No new product feature is
authorized by this record.

The pilot tenant runbook must be rehearsed with synthetic identities before
use: assign a never-reused UUID; generate and deliver a unique key through a
separate approved channel; verify only that tenant's scoped API access and
limits; record the issue/rotation owner without logging the key; for offboard,
revoke first, remove the external mapping, recreate API, reconcile the
journal, prove the old key is 401 and all tenant jobs are absent while another
tenant remains unaffected. A legal hold or export request needs separate
approval and controlled handling; do not repurpose the automated deletion
path as a universal legal answer.

The controller/business owner and qualified counsel must decide controller
versus processor role, lawful basis, final retention/holds, data-subject
request workflow, privacy notice, terms, subprocessors and hosting location,
incident notification duties, contract clauses and any DPA. These cannot be
settled by code. The [LGPD](https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709.htm)
and [ANPD role guidance](https://www.gov.br/anpd/pt-br/centrais-de-conteudo/materiais-educativos-e-publicacoes/guia-orientativo-para-definicoes-dos-agentes-de-tratamento-de-dados-pessoais-e-do-encarregado)
are review inputs, not a legal opinion. The [ANPD incident communication
guidance](https://www.gov.br/anpd/pt-br/canais_atendimento/agente-de-tratamento/comunicado-de-incidente-de-seguranca-cis)
must be reflected in the approved incident plan. Without these decisions,
**pilot and broad sale are NO-GO**.

### Decision

1. **Infrastructure production: NO-GO.** No host, real TLS, digest-pinned
   artifact, offsite recovery or active alerts have been verified.
2. **One customer pilot: NO-GO.** Technical and legal/operational blockers
   remain; do not process a customer's document yet.
3. **Broad commercial sale: NO-GO.** Pilot prerequisites plus support,
   contract, billing, capacity and service claims are not ready.

Gate 9 is **BLOCKED**, not FAIL: the existing local/CI evidence is intact,
but required real-world controls and three software design gaps are not
closed. Next authorized work should first implement/test registry-digest
release support, a least-privilege backup-inventory view and an age-aware
log plan locally, then select offsite/host/owners and request explicit
approval for any external provisioning or publication.
