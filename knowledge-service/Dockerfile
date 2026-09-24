FROM python:3.12-slim

# curl is only here for the compose healthcheck, matching how the
# orchestrator's health probe works.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py /app/app.py

# graphrag is MOUNTED, not copied. Building it in would mean a build context
# at the repository root -- 403 MB, and it would pull the metadata mirror with
# its live credentials into an image layer. Mounting also means a code change
# is a restart rather than a rebuild, and it matches how brain/packs is
# already served to the orchestrator.
ENV PYTHONPATH=/app/vendor \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8006
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8006"]
