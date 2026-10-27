#!/bin/sh
# Custom-format dump, restrictive permissions, atomic publication, no deletion.
set -eu
umask 077
: "${PGHOST:?set PGHOST}"
: "${PGDATABASE:?set PGDATABASE}"
: "${PGUSER:?set PGUSER}"
backup_dir=${BACKUP_DIR:-/backups}
mkdir -p "$backup_dir"
backup_file="$backup_dir/opstrail-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
trap 'echo "backup interrupted; incomplete .partial file is not a usable backup" >&2' HUP INT TERM
pg_dump --no-owner --no-acl --format=custom --file="$backup_file.partial"
pg_restore --list "$backup_file.partial" > /dev/null
mv "$backup_file.partial" "$backup_file"
echo "backup completed: $backup_file"
