FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9

# The dev stack's inference cap: at most two in-flight requests to the production engines.
# Standard library only; nothing is installed. Configuration (CAP_UPSTREAMS and friends) comes from
# the environment; see the module docstring. SIGTERM drains in-flight requests for up to 30 s, so
# give the container a stop grace period above that.

ENV PYTHONUNBUFFERED=1

COPY inference_cap.py /app/inference_cap.py

USER 65532:65532

EXPOSE 9100

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python3", "-c", "import sys, urllib.request; opener = urllib.request.build_opener(urllib.request.ProxyHandler({})); sys.exit(0 if opener.open('http://127.0.0.1:9100/_cap/health', timeout=4).status == 200 else 1)"]

CMD ["python3", "/app/inference_cap.py"]
