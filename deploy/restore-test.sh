#!/bin/sh
# Restore only into a fresh disposable database, never an existing production DB.
set -eu
: "${PGDATABASE:?set disposable target PGDATABASE}"
case "$PGDATABASE" in
  *_restore_test) ;;
  *) echo 'PGDATABASE must end in _restore_test' >&2; exit 2 ;;
esac
if [ "$#" -ne 1 ]; then
  echo 'usage: restore-test.sh /path/to/backup.dump' >&2
  exit 2
fi
# Fails if the target already exists. No --clean and no DROP DATABASE.
createdb "$PGDATABASE"
pg_restore --exit-on-error --no-owner --no-acl --dbname="$PGDATABASE" "$1"
psql --no-psqlrc --set ON_ERROR_STOP=1 --command='SELECT version_num FROM alembic_version; SELECT count(*) AS alerts FROM alerts; SELECT count(*) AS notifications FROM notifications;'
