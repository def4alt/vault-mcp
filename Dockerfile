FROM ghcr.io/astral-sh/uv:0.12.24 AS uv

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client poppler-utils util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home vault
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev && chown -R vault /app
USER vault
# The vault is a bind-mounted git repo owned by another uid on some hosts.
RUN git config --global --add safe.directory /vault
ENV PATH=/app/.venv/bin:$PATH VAULT_ROOT=/vault FASTMCP_HOME=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000 9000
CMD ["vault-mcp"]
