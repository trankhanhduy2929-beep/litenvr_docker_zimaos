FROM python:3.11-slim-bookworm

ARG TARGETARCH=amd64
ARG GO2RTC_VERSION=v1.9.14

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tzdata \
    curl \
    && curl -fsSL -o /usr/local/bin/go2rtc \
       "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_linux_${TARGETARCH}" \
    && chmod +x /usr/local/bin/go2rtc \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.143.3 \
    uvicorn==0.54.0 \
    pydantic==2.14.1 \
    websocket-client==1.9.2 \
    smbprotocol==1.17.0 \
    pyyaml==6.0.3 \
    paho-mqtt==2.1.0

COPY app/ /app/

ARG LITENVR_VERSION=1.9.22
ENV LITENVR_VERSION=${LITENVR_VERSION}
ENV MALLOC_ARENA_MAX=2

EXPOSE 8080 8556/tcp 8556/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
