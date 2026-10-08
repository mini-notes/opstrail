#!/bin/sh
# Run migrations before serving. Alembic is idempotent, so this is safe on
# every start; with several replicas Postgres serialises the version table.
set -e
echo "opstrail: running migrations..."
alembic upgrade head
echo "opstrail: starting api..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
