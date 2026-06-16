FROM cryptochrome/dovi_convert:8.2.0@sha256:6592be70d2114c4c6812a93d0359e5cdfc3122d2ff581dab3f9555a833ae10b2

ARG APP_VERSION=dev
ARG APP_REVISION=unknown
ARG APP_BUILD_DATE=unknown

LABEL org.opencontainers.image.title="dovi-manager" \
      org.opencontainers.image.description="Server-rendered web UI for dovi_convert" \
      org.opencontainers.image.base.name="docker.io/cryptochrome/dovi_convert:8.2.0" \
      org.opencontainers.image.source="https://github.com/dovi-manager/dovi-manager" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${APP_REVISION}" \
      org.opencontainers.image.created="${APP_BUILD_DATE}"

USER root

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEDIA_ROOT=/media2/movies \
    TEMP_DIR=/cache \
    CONFIG_DIR=/config \
    DB_PATH=/config/dovi-manager.db \
    DOVI_CONVERT_PATH=/usr/local/bin/dovi_convert \
    DOVI_MANAGER_VERSION="${APP_VERSION}" \
    DOVI_MANAGER_REVISION="${APP_REVISION}" \
    DOVI_MANAGER_BUILD_DATE="${APP_BUILD_DATE}" \
    NO_COLOR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/dovi-manager

COPY requirements.txt .
RUN python3 -m venv /opt/dovi-manager/.venv \
    && /opt/dovi-manager/.venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY THIRD_PARTY_NOTICES.md .
COPY docker-entrypoint.sh /usr/local/bin/dovi-manager-entrypoint

RUN chmod 0755 /usr/local/bin/dovi-manager-entrypoint \
    && mkdir -p /media2/movies /cache /config

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/opt/dovi-manager/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3).read()"]

ENTRYPOINT ["/usr/local/bin/dovi-manager-entrypoint"]
