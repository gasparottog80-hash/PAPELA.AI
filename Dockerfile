# syntax=docker/dockerfile:1
# Single image shared by api + worker. Fake OCR by default; add the `ocr`
# extra + system libs when wiring real PaddleOCR (see comment below).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# git + openssh-client: needed to resolve the private papela-fiscal-extractor
# Git dependency during `pip install .` below.
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

# For real PaddleOCR add: libgl1 libglib2.0-0 (OpenCV runtime) and install
# ".[ocr]". Kept out of the default image to stay slim.
COPY pyproject.toml README.md ./
COPY app ./app

# --mount=type=ssh forwards the host's SSH agent for this step only — the
# private key is never written to any layer or to the final image. Requires
# `docker build --ssh default` (see README).
RUN --mount=type=ssh pip install --upgrade pip && pip install .

# Non-root: the app never needs root, and the PDF volume is chown'd to it.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/pdfs && chown -R appuser:appuser /data /app
USER appuser

# Default command = API. Compose overrides `command` for the worker.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
