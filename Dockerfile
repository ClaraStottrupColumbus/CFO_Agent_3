# CFO budgeting agent — Azure App Service for Containers image.
# Build for amd64 even on Apple Silicon; Azure runs amd64:
#   docker build --platform linux/amd64 -t <acr>.azurecr.io/cfo-budget-agent:v1 .
# See AZURE_DEPLOYMENT.md for the full deploy runbook.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copied explicitly rather than `COPY . .` so .env, .venv/ and tests/ cannot
# reach an image layer even if .dockerignore is wrong.
COPY app/ ./app/
COPY static/ ./static/

# data/ ships with the image on purpose: every path constant in the app is
# derived from the source location and none is env-configurable, so the
# container must be writable here. Shipping a populated snapshot means
# profile.setup_complete() is true on boot (no setup gate) and the lifespan
# hook skips generate_data.generate(). App Service disks are ephemeral, so a
# restart resets the demo to this known-good state.
COPY data/ ./data/

# Must match WEBSITES_PORT on the web app.
EXPOSE 8000

# One worker, deliberately: scheduler.py is an in-process daemon thread and
# every store writes local JSON under a threading.Lock, so a second worker
# would give two schedulers racing on the same files.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
