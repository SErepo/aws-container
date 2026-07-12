# syntax=docker/dockerfile:1

###############################################################################
# Stage 1 — builder
# We use uv (an ultra-fast Python package manager) to install dependencies into
# an isolated virtual environment. Doing this in a separate stage keeps build
# tools out of the final image, making it smaller and more secure.
###############################################################################
FROM python:3.11-slim AS builder

# uv reads these to build a self-contained venv we can copy to the final stage.
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Install uv (single static binary) from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Build tools needed to compile psycopg2 if a wheel isn't available.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create the virtual environment and install requirements.
COPY requirements.txt .
RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache -r requirements.txt


###############################################################################
# Stage 2 — runtime
# Slim final image that only contains the Python runtime, the pre-built venv
# and the application code. No build tools => smaller attack surface.
###############################################################################
FROM python:3.11-slim AS runtime

# libpq5 is the runtime library psycopg2 links against.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user for security.
RUN useradd --create-home --uid 1000 appuser

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=5000

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 5000

# Container-level health check (mirrors the ALB health check in AWS).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:5000/health || exit 1

# gunicorn = production WSGI server. 2 workers x 4 threads is a sane default for
# a small Fargate task (0.5 vCPU). Tune via CPU/memory in the ECS task def.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "60", "app:app"]
