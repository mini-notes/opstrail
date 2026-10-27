#!/bin/sh
set -eu
while true; do
  /bin/sh /scripts/backup.sh
  sleep "${BACKUP_INTERVAL_SECONDS:-86400}" &
  wait "$!"
done
