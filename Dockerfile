# syntax=docker/dockerfile:1
# Single minimal image shared by api + worker. Native PaddleOCR is excluded.
FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e AS base

FROM base AS builder

# Match the local validation tool; dependency versions come from uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# git + openssh-client: needed to resolve the private papela-fiscal-extractor
# Git dependency during the locked sync below (builder only).
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

# GitHub's official SSH host keys — public data, not a secret (published at
# https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).
# Pins host identity so the SSH-forwarded clone below is verified against a
# known trust anchor, not accepted blindly on first use. Verified against the
# official SHA256 fingerprints:
#   ED25519 SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
#   ECDSA   SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM
#   RSA     SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s
RUN mkdir -p /etc/ssh && cat >> /etc/ssh/ssh_known_hosts <<'EOF'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
EOF

# Do not install the `ocr` extra in this image. It has unmitigated advisories.
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY scripts/verify_extractor.py ./scripts/verify_extractor.py

# --mount=type=ssh forwards the host's SSH agent for this step only — the
# private key is never written to any layer or to the final image. Requires
# `docker build --ssh default` (see README).
# --locked validates manifest consistency and refuses lockfile updates.
# The temporary cache (including the Git checkout) never becomes a layer.
RUN --mount=type=ssh,required=true \
    --mount=type=tmpfs,target=/root/.cache/uv \
    GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/ssh/ssh_known_hosts -o GlobalKnownHostsFile=/dev/null" \
    UV_PYTHON_DOWNLOADS=never uv sync --locked --no-dev --no-editable --link-mode=copy \
    && .venv/bin/python scripts/verify_extractor.py

# Retain only Debian runtime libraries needed by Python/psycopg. OpenSSL is
# upgraded from the Debian security repository, and the actual dpkg metadata
# is copied with the binaries so image scanners see the installed version.
FROM base AS patched-libs
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade \
        libssl3t64=3.5.7-1~deb13u3 \
    && mkdir -p /pkgmeta \
    && for package in libssl3t64 libffi8 libgcc-s1; do \
        dpkg-query -s "$package" > "/pkgmeta/$package"; \
    done \
    && rm -rf /var/lib/apt/lists/*

# Build/install tooling is not needed to execute the pre-built virtualenv.
# Remove global pip/setuptools/wheel and their vendored runtime CVE surface.
FROM builder AS stripped-python
RUN /usr/local/bin/python -m pip uninstall --yes pip setuptools wheel \
    && mkdir -p /runtime-data/pdfs \
    && chmod 700 /runtime-data/pdfs

FROM gcr.io/distroless/base-debian13@sha256:0896741ba5bafd3ac87ea025a5f578952f2d238ddc3614cb368acc983a687aa2 AS runtime

# Build provenance is supplied by the release job; it never contains secrets.
ARG RELEASE_COMMIT=unspecified
LABEL org.opencontainers.image.revision="${RELEASE_COMMIT}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LD_LIBRARY_PATH="/usr/local/lib" \
    PATH="/app/.venv/bin:/usr/local/bin:/usr/bin:/bin"

WORKDIR /app
COPY --from=stripped-python /usr/local/bin/python3 /usr/local/bin/python3
COPY --from=stripped-python /usr/local/bin/python3.11 /usr/local/bin/python3.11
COPY --from=stripped-python /usr/local/lib/libpython3.11.so.1.0 /usr/local/lib/libpython3.11.so.1.0
COPY --from=stripped-python /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=patched-libs /usr/lib/x86_64-linux-gnu/libffi.so.8 /usr/lib/x86_64-linux-gnu/libffi.so.8
COPY --from=patched-libs /usr/lib/x86_64-linux-gnu/libgcc_s.so.1 /usr/lib/x86_64-linux-gnu/libgcc_s.so.1
COPY --from=patched-libs /usr/lib/x86_64-linux-gnu/libssl.so.3 /usr/lib/x86_64-linux-gnu/libssl.so.3
COPY --from=patched-libs /usr/lib/x86_64-linux-gnu/libcrypto.so.3 /usr/lib/x86_64-linux-gnu/libcrypto.so.3
COPY --from=patched-libs /pkgmeta/ /var/lib/dpkg/status.d/
COPY --from=stripped-python --chown=65532:65532 /app/.venv /app/.venv
COPY --from=builder /app/uv.lock /app/uv.lock
COPY --from=builder /app/scripts/verify_extractor.py /app/scripts/verify_extractor.py
COPY --from=stripped-python --chown=65532:65532 --chmod=0700 /runtime-data/pdfs /data/pdfs

# Distroless has no shell/package manager. Keep the same non-root runtime.
USER 65532:65532

# Default command = API. Compose overrides `command` for the worker.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log", "--limit-concurrency", "32", "--timeout-keep-alive", "5"]
