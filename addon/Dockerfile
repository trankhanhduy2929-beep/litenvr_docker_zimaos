ARG BASE_IMAGE=ghcr.io/trankhanhduy2929-beep/litenvr_docker_zimaos:latest
FROM ${BASE_IMAGE}

ARG BUILD_VERSION=1.9.20
ARG BUILD_ARCH=amd64

LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="addon" \
      io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.title="LiteNVR" \
      org.opencontainers.image.source="https://github.com/trankhanhduy2929-beep/litenvr_docker_zimaos"

COPY addon/run.py /run.py
RUN chmod a+x /run.py

EXPOSE 3333/tcp 8556/tcp 8556/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3333/health', timeout=4).read()" || exit 1

CMD ["python3", "/run.py"]
