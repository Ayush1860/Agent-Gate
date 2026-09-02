# syntax=docker/dockerfile:1

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a source edit does not invalidate the install layer.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY agentgate/ ./agentgate/
COPY eval/ ./eval/
RUN pip install --no-deps -e .

# Traces are written here; mount a volume to keep them across restarts.
RUN mkdir -p /app/runs

# Default to the offline provider so `docker compose up` works with no key set.
ENV AGENTGATE_PROVIDER=mock \
    AGENTGATE_MODEL=mock-reviewer-v1 \
    AGENTGATE_TRACE_FILE=/app/runs/traces.jsonl \
    AGENTGATE_REVIEW_FILE=/app/runs/reviews.jsonl

# Run as a non-root user.
RUN useradd --create-home --uid 10001 agentgate && chown -R agentgate:agentgate /app
USER agentgate

EXPOSE 8000 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["agentgate", "serve", "--host", "0.0.0.0", "--port", "8000"]
