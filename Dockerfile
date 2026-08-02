# Runnable image for sidecar mode (see README) — a long-lived container that
# keeps refreshing secrets in the background. For exec mode, most consumers
# instead ADD the raw init.py URL directly into their own image (see README);
# this image exists specifically so sidecar mode has something to `image:`
# reference without every consumer needing python3/pyyaml in their own app
# image.
FROM python:3.12-alpine
RUN pip install --no-cache-dir pyyaml
COPY init.py /init.py

# Sidecar mode never exits on its own — depends_on must use
# condition: service_healthy, not service_completed_successfully. The
# readiness file is created only after the first successful fetch, matching
# the old "app waits for secrets to exist" bootstrap guarantee.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD test -f "${READY_FILE:-/tmp/container-init-ready}" || exit 1

ENTRYPOINT ["python3", "/init.py"]
