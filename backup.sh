#!/bin/bash
# Meal Planner backup — runs periodically, only backs up if DB changed
# Keeps last 30 backups (rotating)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${MEAL_PLANNER_DATA:-$SCRIPT_DIR/data}"
DB="$DATA_DIR/meal_planner.db"
UPLOADS="$DATA_DIR/uploads"
BACKUP_DIR="$DATA_DIR/backups"
STATE_FILE="$BACKUP_DIR/.last_backup_hash"
MAX_BACKUPS=30

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB" ]; then
    exit 0
fi

CURRENT_HASH=$(sha256sum "$DB" | cut -d' ' -f1)
LAST_HASH=""
if [ -f "$STATE_FILE" ]; then
    LAST_HASH=$(cat "$STATE_FILE")
fi

if [ "$CURRENT_HASH" = "$LAST_HASH" ]; then
    exit 0
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="meal_planner_${TIMESTAMP}"

python3 -c "
import sqlite3
src = sqlite3.connect('$DB')
dst = sqlite3.connect('$BACKUP_DIR/${BACKUP_NAME}.db')
src.backup(dst)
dst.close()
src.close()
"

if [ -d "$UPLOADS" ] && [ "$(ls -A $UPLOADS 2>/dev/null)" ]; then
    tar czf "$BACKUP_DIR/${BACKUP_NAME}_uploads.tar.gz" -C "$DATA_DIR" uploads
fi

echo "$CURRENT_HASH" > "$STATE_FILE"

cd "$BACKUP_DIR"
ls -t meal_planner_*.db 2>/dev/null | tail -n +$((MAX_BACKUPS + 1)) | xargs -r rm -f
ls -t meal_planner_*_uploads.tar.gz 2>/dev/null | tail -n +$((MAX_BACKUPS + 1)) | xargs -r rm -f

echo "$(date): Backup created: ${BACKUP_NAME}"
