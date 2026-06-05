ARG PYTHON_VERSION=3.12
ARG PYTHON_VARIANT=slim
ARG PORT=8080
ARG APP_USER=app
ARG APP_UID=1000

FROM python:${PYTHON_VERSION}-${PYTHON_VARIANT} AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:${PYTHON_VERSION}-${PYTHON_VARIANT}
ARG PORT
ARG APP_USER
ARG APP_UID
ENV PYTHONUNBUFFERED=1 PORT=${PORT}
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq-dev curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u ${APP_UID} ${APP_USER}
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache /wheels/*
COPY --chown=${APP_USER}:${APP_USER} app/ ./app/
USER ${APP_USER}
EXPOSE ${PORT}
HEALTHCHECK --interval=30s --timeout=10s \
  CMD curl -f http://localhost:${PORT}/health || exit 1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]