#!/bin/sh
# Development convenience. Production overrides command and runs the migrate
# one-shot explicitly before starting API/worker containers.
set -e
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
