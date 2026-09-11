# Single image shared by api + worker. Fake OCR by default; add the `ocr`
# extra + system libs when wiring real PaddleOCR (see comment below).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# For real PaddleOCR add: libgl1 libglib2.0-0 (OpenCV runtime) and install
# ".[ocr]". Kept out of the default image to stay slim.
COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --upgrade pip && pip install .

# Non-root: the app never needs root, and the PDF volume is chown'd to it.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/pdfs && chown -R appuser:appuser /data /app
USER appuser

# Default command = API. Compose overrides `command` for the worker.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
