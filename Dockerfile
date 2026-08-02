# Runnable image for sidecar mode (see README) — a one-shot container that
# fetches secrets and exits. For exec mode, most consumers instead ADD the
# raw init.py URL directly into their own image (see README); this image
# exists specifically so sidecar mode has something to `image:` reference
# without every consumer needing python3/pyyaml in their own app image.
FROM python:3.12-alpine
RUN pip install --no-cache-dir pyyaml
COPY init.py /init.py
ENTRYPOINT ["python3", "/init.py"]
