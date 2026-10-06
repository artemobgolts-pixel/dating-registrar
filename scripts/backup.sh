#!/usr/bin/env bash
# Ежедневный quiesced DB+media bundle -> rclone; каждый retained bundle самодостаточен.
# Поддерживает управляемый и legacy/simple релиз. Короткая пауза traffic обязательна.
# RCLONE_REMOTE=backup:date4you KEEP_REMOTE=30 ./scripts/backup.sh
# Telegram DB-only копия остаётся явным opt-in через TG_BACKUP_CHAT_ID.
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python3}" scripts/backup_remote.py
