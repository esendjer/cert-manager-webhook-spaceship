FROM docker.io/library/python:3.15-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM docker.io/library/python:3.15-slim

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --system --uid 65532 --no-create-home --shell /usr/sbin/nologin webhook

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY main.py ./

USER 65532:65532
EXPOSE 8443

# TLS is mandatory: the kube-apiserver only talks to aggregated APIs over HTTPS.
# TLS_CERT_FILE / TLS_KEY_FILE are expected to be mounted by the deployment.
ENV TLS_CERT_FILE=/tls/tls.crt \
    TLS_KEY_FILE=/tls/tls.key

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port 8443 --ssl-certfile \"$TLS_CERT_FILE\" --ssl-keyfile \"$TLS_KEY_FILE\""]
