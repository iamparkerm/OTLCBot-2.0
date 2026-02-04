#!/usr/bin/env bash
set -euo pipefail

# --- Config (adjust if you ever change these) ---
REPO_DIR="$HOME/OTLCBot-2.0"
ENV_FILE="$REPO_DIR/.env"
MOUNT_POINT="/mnt/usb"
DEVICE="/dev/sda1"          # your USB partition
FSTYPE="vfat"               # confirmed working for you
BACKUP_SUBDIR="otlc_backups"

# Log file (lives on the Pi)
LOG_FILE="$HOME/otlc_backup.log"

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE" >/dev/null
}

# Load env vars (DB_PATH) from .env
if [[ ! -f "$ENV_FILE" ]]; then
  log "ERROR: .env not found at $ENV_FILE"
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

DB_PATH="${DB_PATH:-}"
if [[ -z "$DB_PATH" ]]; then
  log "ERROR: DB_PATH not set in .env"
  exit 1
fi

if [[ ! -f "$DB_PATH" ]]; then
  log "ERROR: DB not found at $DB_PATH"
  exit 1
fi

# If USB device isn't present, exit gracefully
if [[ ! -b "$DEVICE" ]]; then
  log "USB not detected at $DEVICE (is it plugged in?). Exiting."
  exit 0
fi

# Make mount point
sudo mkdir -p "$MOUNT_POINT"

# Make a temp backup file first (safe SQLite backup)
TMP_BACKUP="/tmp/otlc_db_$(date +%F_%H%M%S).db"
log "Creating SQLite safe backup to $TMP_BACKUP ..."
sqlite3 "$DB_PATH" ".backup '$TMP_BACKUP'"

# Mount USB (explicit FS type avoids auto-detect issues)
log "Mounting $DEVICE to $MOUNT_POINT ..."
sudo mount -t "$FSTYPE" "$DEVICE" "$MOUNT_POINT"

# Ensure backup directory exists on USB
USB_DIR="$MOUNT_POINT/$BACKUP_SUBDIR"
sudo mkdir -p "$USB_DIR"

# Copy with a timestamped filename
DEST="$USB_DIR/data_$(date +%F_%H%M%S).db"
log "Copying backup to $DEST ..."
sudo cp "$TMP_BACKUP" "$DEST"

# Optional: keep a stable "latest.db" too
log "Updating latest.db ..."
sudo cp "$TMP_BACKUP" "$USB_DIR/latest.db"

# Flush writes and unmount cleanly
log "Sync + unmount ..."
sync
sudo umount "$MOUNT_POINT"

# Cleanup temp file
rm -f "$TMP_BACKUP"

log "Backup completed successfully."
