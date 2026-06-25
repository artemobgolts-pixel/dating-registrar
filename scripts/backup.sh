#!/usr/bin/env bash
# Бэкап базы НАРУЖУ (в облако). Запускается по cron на прод-хосте, напр. раз в сутки:
#
#   # /etc/cron.d/date4you-backup  (или crontab -e)
#   17 4 * * *  cd /opt/date4you && ./scripts/backup.sh >> /var/log/date4you-backup.log 2>&1
#
# Логика:
#   1) консистентный снимок SQLite ВНУТРИ контейнера (sqlite backup API, безопасно при WAL);
#   2) забираем самый свежий снимок из data/backups;
#   3) заливаем в облако через rclone (S3 / Я.Диск / R2 — что настроено в rclone-ремоуте);
#   4) храним последние KEEP_REMOTE копий снимка БД в облаке;
#   5) зеркалим медиа (data/uploads) в облако; удалённые файлы уходят в корзину
#      uploads-trash/ДАТА и чистятся через MEDIA_TRASH_DAYS дней;
#   6) шлём свежий снимок БД (gzip) документом в Telegram (TG_BACKUP_CHAT_ID).
#
# Зачем наружу: авто-снимок приложения лежит на ТОМ ЖЕ диске, что и боевая база.
# Это данные посторонних — потеря недопустима. Облачная копия переживает гибель диска.
#
# Требования на хосте: docker compose, rclone (`rclone config` → ремоут RCLONE_REMOTE).
# Пример для S3-совместимого хранилища (ключи живут ТОЛЬКО в ~/.config/rclone/rclone.conf
# с правами 600 — НИКОГДА не в гите и не в этом файле):
#   rclone config create backup s3 provider=Other \
#     endpoint=<s3-endpoint> access_key_id=<key> secret_access_key=<secret>
# и RCLONE_REMOTE=backup:<имя-бакета>.
set -euo pipefail

# --- настройки (переопредели через окружение или правкой здесь) ---
COMPOSE="${COMPOSE:-docker compose}"          # как звать compose на этом хосте
SERVICE="${SERVICE:-app}"                      # имя сервиса с приложением
DATA_DIR="${DATA_DIR:-./data}"                 # каталог data на хосте (volume контейнера)
RCLONE_REMOTE="${RCLONE_REMOTE:-backup:date4you}"  # rclone-ремоут:путь назначения
KEEP_REMOTE="${KEEP_REMOTE:-30}"               # сколько копий снимка БД держать в облаке
MEDIA_TRASH_DAYS="${MEDIA_TRASH_DAYS:-7}"      # сколько дней держать удалённые медиа в корзине
MEDIA_MAX_DELETE="${MEDIA_MAX_DELETE:-200}"    # стоп-кран: не зеркалить, если sync хочет снести больше N файлов

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

# 5) Медиа (фото/видео) НАРУЖУ: зеркалирование каталога uploads с «корзиной».
#    Снимок базы хранит только ИМЕНА файлов — без этого каталога восстановление
#    даст битые карточки. sync (а не copy) держит remote зеркалом живых файлов,
#    НО удаляемое не пропадает: --backup-dir уводит его в датированную корзину
#    uploads-trash/ГГГГ-ММ-ДД, откуда чистится через MEDIA_TRASH_DAYS дней. Так
#    файл в работе ВСЕГДА в бэкапе, удалённый ещё неделю можно достать, а бакет
#    не растёт бесконечно. Защита: пустой/исчезнувший uploads НЕ зеркалим (иначе
#    sync увёл бы весь remote в корзину); --max-delete отбивает аномальный снос.
UPLOADS_DIR="$DATA_DIR/uploads"
if [ -d "$UPLOADS_DIR" ] && [ -n "$(ls -A "$UPLOADS_DIR" 2>/dev/null)" ]; then
  TODAY="$(date +%F)"
  rclone sync "$UPLOADS_DIR" "$RCLONE_REMOTE/uploads" \
    --backup-dir "$RCLONE_REMOTE/uploads-trash/$TODAY" \
    --max-delete "$MEDIA_MAX_DELETE" --fast-list
  echo "Медиа синхронизированы в $RCLONE_REMOTE/uploads (удалённое → uploads-trash/$TODAY)"

  # Чистим корзины старше MEDIA_TRASH_DAYS дней. Имена ГГГГ-ММ-ДД сравнимы как
  # строки, поэтому достаточно сравнить с датой-отсечкой.
  CUTOFF="$(date -d "$MEDIA_TRASH_DAYS days ago" +%F)"
  while IFS= read -r d; do
    name="${d%/}"
    if [ -n "$name" ] && [ "$name" \< "$CUTOFF" ]; then
      rclone purge "$RCLONE_REMOTE/uploads-trash/$name" && echo "Корзина очищена: $name"
    fi
  done < <(rclone lsf "$RCLONE_REMOTE/uploads-trash/" --dirs-only 2>/dev/null || true)
else
  echo "ПРЕДУПРЕЖДЕНИЕ: $UPLOADS_DIR пуст или не найден — медиа НЕ синхронизированы "\
       "(зеркало не трогаем, чтобы не увести живой бэкап в корзину)" >&2
fi

# 6) Снимок базы в Telegram. Шлём ИЗ КОНТЕЙНЕРА (там TG_BOT_TOKEN и
#    TG_BACKUP_CHAT_ID из .env, и переиспользуем уже оттестированный
#    tasks.ship_backup_to_tg: gzip + проверка лимита 50 МБ + подпись). Если
#    TG_BACKUP_CHAT_ID не задан — функция тихо вернёт False. Сбой TG не валит
#    бэкап: облачная копия уже сделана выше, это лишь «карман под рукой».
$COMPOSE exec -T "$SERVICE" python -c "
import glob, tasks
from pathlib import Path
f = sorted(glob.glob('/data/backups/app-*.db'))
print('Telegram:', 'отправлено' if (f and tasks.ship_backup_to_tg(Path(f[-1]))) else 'пропущено')
" || echo "ПРЕДУПРЕЖДЕНИЕ: отправка снимка в Telegram не удалась (бэкап в облаке сделан)" >&2

echo "[$(date '+%F %T')] Бэкап: готово"
