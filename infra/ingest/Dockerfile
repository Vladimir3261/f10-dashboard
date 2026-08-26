# The ingest server is pure stdlib Python, so a slim base with no pip
# install is all it needs. Build context is the repo root so the shared
# infra/common module is available.
FROM python:3.12-slim

WORKDIR /app

# Only the pieces the server needs: the shared wire format, the server
# itself, and the channel map. No third-party dependencies.
COPY infra/common /app/infra/common
COPY infra/ingest /app/infra/ingest

ENV PYTHONUNBUFFERED=1
EXPOSE 8090

CMD ["python3", "/app/infra/ingest/server.py"]
