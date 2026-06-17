#!/usr/bin/env bash
# Бэкап базы НАРУЖУ (в облако). Запускается по cron на прод-хосте, напр. раз в сутки:
#
#   # /etc/cron.d/date4you-backup  (или crontab -e)
#   17 4 * * *  cd /opt/boris-site && ./scripts/backup.sh >> /var/log/date4you-backup.log 2>&1
#
# Логика:
#   1) консистентный снимок SQLite ВНУТРИ контейнера (sqlite backup API, безопасно при WAL);
#   2) забираем самый свежий снимок из data/backups;
#   3) заливаем в облако через rclone (S3 / Я.Диск / R2 — что настроено в rclone-ремоуте);
#   4) храним последние KEEP_REMOTE копий в облаке.
#
# Зачем наружу: авто-снимок приложения лежит на ТОМ ЖЕ диске, что и боевая база.
# Это данные посторонних — потеря недопустима. Облачная копия переживает гибель диска.
#
# Требования на хосте: docker compose, rclone (`rclone config` → ремоут RCLONE_REMOTE).
set -euo pipefail

# --- настройки (переопредели через окружение или правкой здесь) ---
COMPOSE="${COMPOSE:-docker compose}"          # как звать compose на этом хосте
SERVICE="${SERVICE:-app}"                      # имя сервиса с приложением
DATA_DIR="${DATA_DIR:-./data}"                 # каталог data на хосте (volume контейнера)
RCLONE_REMOTE="${RCLONE_REMOTE:-backup:date4you}"  # rclone-ремоут:путь назначения
KEEP_REMOTE="${KEEP_REMOTE:-30}"               # сколько копий держать в облаке

BACKUP_DIR="$DATA_DIR/backups"

echo "[$(date '+%F %T')] Бэкап: старт"

# 1) Снимок внутри контейнера (пишет в /data/backups, он же $BACKUP_DIR на хосте).
$COMPOSE exec -T "$SERVICE" python backup.py

# 2) Самый свежий снимок.
LATEST="$(ls -1t "$BACKUP_DIR"/app-*.db 2>/dev/null | head -n1 || true)"
if [ -z "$LATEST" ]; then
  echo "ОШИБКА: не нашёл свежий снимок в $BACKUP_DIR" >&2
  exit 1
fi
echo "Снимок: $LATEST"

# 3) Заливка в облако.
rclone copy "$LATEST" "$RCLONE_REMOTE/" --no-traverse
echo "Залито в $RCLONE_REMOTE/$(basename "$LATEST")"

# 4) Ротация в облаке: оставляем KEEP_REMOTE самых свежих app-*.db.
mapfile -t REMOTE < <(rclone lsf "$RCLONE_REMOTE/" --include 'app-*.db' | sort)
EXTRA=$(( ${#REMOTE[@]} - KEEP_REMOTE ))
if [ "$EXTRA" -gt 0 ]; then
  for f in "${REMOTE[@]:0:$EXTRA}"; do
    rclone deletefile "$RCLONE_REMOTE/$f" && echo "Удалил старую копию: $f"
  done
fi

echo "[$(date '+%F %T')] Бэкап: готово"
