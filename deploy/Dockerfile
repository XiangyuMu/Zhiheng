ARG REGISTRY=docker.m.daocloud.io
FROM ${REGISTRY}/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends restic sqlite3 openssl ca-certificates
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY migrations ./migrations
COPY scripts ./scripts
COPY alembic.ini ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && zhiheng-api"]
