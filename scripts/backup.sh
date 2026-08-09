#!/usr/bin/env bash
# Konsistentes SQLite-Backup des laufenden Containers (nutzt die
# sqlite3-Backup-API, daher auch bei laufenden Schreibzugriffen sicher).
# Aufruf per Cron, z. B. täglich 03:00:
#   0 3 * * * /pfad/zu/sportabo-manager/scripts/backup.sh
set -euo pipefail

BACKUP_DIR="$(cd "$(dirname "$0")/.." && pwd)/data/backups"
KEEP_DAYS=30
STAMP=$(date +%Y%m%d-%H%M%S)

mkdir -p "$BACKUP_DIR"

docker exec sportabo python -c "
import sqlite3
src = sqlite3.connect('/app/data/sportabo.db')
dst = sqlite3.connect('/app/data/backups/sportabo-$STAMP.db')
src.backup(dst)
dst.close(); src.close()
"

# Alte Backups aufräumen
find "$BACKUP_DIR" -name 'sportabo-*.db' -mtime "+$KEEP_DAYS" -delete

echo "Backup: $BACKUP_DIR/sportabo-$STAMP.db"
# Tipp: Off-Site-Kopie z. B. auf eine Hetzner Storage Box:
# rsync -a "$BACKUP_DIR/" u123456@u123456.your-storagebox.de:sportabo-backups/
