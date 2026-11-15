# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, so code-only changes reuse this cached layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra groq --no-install-project

COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra groq --no-editable


FROM python:3.12-slim
RUN useradd --create-home --uid 10001 agent
WORKDIR /app
COPY --from=build --chown=agent:agent /app/.venv /app/.venv
COPY --chown=agent:agent scripts ./scripts

ARG GIT_SHA=dev
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 GIT_SHA=$GIT_SHA HOST=0.0.0.0
USER agent
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/ping', timeout=2)"
CMD ["python", "-m", "risk_monitor.agentcore_app"]
