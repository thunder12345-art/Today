FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

RUN sed -i \
        -e 's|deb.debian.org|mirrors.aliyun.com|g' \
        -e 's|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true \
    && apt-get -o Acquire::Retries=2 update \
    && apt-get install -y --no-install-recommends ca-certificates git tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY auto/ /app/auto/

EXPOSE 8080
HEALTHCHECK --interval=300s --timeout=15s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/status', timeout=10)"

CMD ["python", "/app/auto/server.py"]
