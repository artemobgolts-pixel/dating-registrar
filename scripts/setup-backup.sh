#!/usr/bin/env bash
# Разовая НАСТРОЙКА бэкапа на ПРОД-СЕРВЕРЕ (выполнять на сервере, не локально).
#
# Что делает:
#   1) ставит rclone, если его нет;
#   2) создаёт rclone-ремоут "backup" (S3) из ПЕРЕМЕННЫХ ОКРУЖЕНИЯ — ключи
#      пишутся в ~/.config/rclone/rclone.conf (chmod 600), НИКОГДА не в git;
#   3) проверяет доступ к бакету (rclone lsd);
#   4) дописывает TG_BACKUP_CHAT_ID в .env (если ещё не задан) и перезапускает app;
#   5) делает контрольный прогон scripts/backup.sh;
#   6) подсказывает строку cron (сам crontab не трогает — добавишь осознанно).
#
# СЕКРЕТЫ В ЭТОТ ФАЙЛ НЕ ВПИСЫВАТЬ. Передаются переменными при запуске:
#
#   S3_ENDPOINT='https://s3.itecocloud.online' \
#   S3_ACCESS_KEY='<access-key>' \
#   S3_SECRET_KEY='<secret-key>' \
#   S3_BUCKET='date4you-d3df2b40' \
#   PROJECT_DIR='/opt/boris-site' \
#   bash scripts/setup-backup.sh
#
# После настройки СМЕНИ скомпрометированные секреты: ключ S3 в панели itecocloud
# и пароль сервера — они засветились при передаче.
set -euo pipefail

# --- входные параметры (через окружение) ---
S3_ENDPOINT="${S3_ENDPOINT:-}"
S3_ACCESS_KEY="${S3_ACCESS_KEY:-}"
S3_SECRET_KEY="${S3_SECRET_KEY:-}"
S3_BUCKET="${S3_BUCKET:-date4you-d3df2b40}"
S3_REGION="${S3_REGION:-}"                       # многие S3 не требуют; оставь пустым
TG_BACKUP_CHAT_ID="${TG_BACKUP_CHAT_ID:--5251173115}"
REMOTE_NAME="${REMOTE_NAME:-backup}"

# Каталог проекта: пробуем оба известных пути, иначе задай PROJECT_DIR= явно.
if [ -z "${PROJECT_DIR:-}" ]; then
  for d in /opt/boris-site /opt/date4you; do
    [ -f "$d/docker-compose.yml" ] && PROJECT_DIR="$d" && break
  done
fi
: "${PROJECT_DIR:?Не нашёл каталог проекта. Задай PROJECT_DIR=/путь/к/проекту}"

miss=()
[ -z "$S3_ENDPOINT" ]   && miss+=(S3_ENDPOINT)
[ -z "$S3_ACCESS_KEY" ] && miss+=(S3_ACCESS_KEY)
[ -z "$S3_SECRET_KEY" ] && miss+=(S3_SECRET_KEY)
if [ "${#miss[@]}" -gt 0 ]; then
  echo "ОШИБКА: не заданы переменные: ${miss[*]}" >&2
  echo "См. пример запуска в шапке этого файла." >&2
  exit 1
fi

echo "→ Проект: $PROJECT_DIR ; бакет: $S3_BUCKET ; ремоут: $REMOTE_NAME"

# 1) rclone
if ! command -v rclone >/dev/null 2>&1; then
  echo "→ Ставлю rclone…"
  curl -fsSL https://rclone.org/install.sh | sudo bash
fi
# Без пайпа в head: под set -euo pipefail head закрывает пайп раньше, rclone
# ловит SIGPIPE и роняет скрипт. Печатаем версию целиком — это безопасно.
rclone version || true

# 2) ремоут S3 (idempotent: пере-создаём с актуальными ключами)
echo "→ Настраиваю rclone-ремоут '$REMOTE_NAME'…"
rclone config delete "$REMOTE_NAME" 2>/dev/null || true
rclone config create "$REMOTE_NAME" s3 \
  provider Other \
  endpoint "$S3_ENDPOINT" \
  access_key_id "$S3_ACCESS_KEY" \
  secret_access_key "$S3_SECRET_KEY" \
  ${S3_REGION:+region "$S3_REGION"} \
  --non-interactive >/dev/null
# права на конфиг с ключами
chmod 600 "$(rclone config file | tail -1)" 2>/dev/null || true

# 3) проверка доступа
echo "→ Проверяю доступ к бакету…"
if ! rclone lsd "$REMOTE_NAME:$S3_BUCKET" >/dev/null 2>&1; then
  echo "  Бакет недоступен или не существует — пробую создать…"
  rclone mkdir "$REMOTE_NAME:$S3_BUCKET"
fi
rclone lsd "$REMOTE_NAME:" && echo "  ✓ S3 отвечает"

# 4) TG_BACKUP_CHAT_ID в .env
ENV_FILE="$PROJECT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "ОШИБКА: нет $ENV_FILE" >&2; exit 1
fi
if grep -q '^TG_BACKUP_CHAT_ID=' "$ENV_FILE"; then
  sed -i "s|^TG_BACKUP_CHAT_ID=.*|TG_BACKUP_CHAT_ID=$TG_BACKUP_CHAT_ID|" "$ENV_FILE"
  echo "→ TG_BACKUP_CHAT_ID обновлён в .env"
else
  printf '\nTG_BACKUP_CHAT_ID=%s\n' "$TG_BACKUP_CHAT_ID" >> "$ENV_FILE"
  echo "→ TG_BACKUP_CHAT_ID добавлен в .env"
fi
echo "→ Перезапускаю app, чтобы подхватить .env…"
( cd "$PROJECT_DIR" && docker compose up -d app )

# 5) контрольный прогон (база + uploads наружу, бэкап базы в TG)
echo "→ Контрольный прогон scripts/backup.sh…"
RCLONE_REMOTE="$REMOTE_NAME:$S3_BUCKET" DATA_DIR="$PROJECT_DIR/data" \
  bash "$PROJECT_DIR/scripts/backup.sh"

# 6) cron-подсказка
CRON_LINE="17 4 * * * cd $PROJECT_DIR && RCLONE_REMOTE=$REMOTE_NAME:$S3_BUCKET DATA_DIR=$PROJECT_DIR/data ./scripts/backup.sh >> /var/log/date4you-backup.log 2>&1"
echo
echo "✓ Настройка завершена."
echo "  Чтобы бэкап шёл ежедневно в 04:17 — добавь в crontab (crontab -e) строку:"
echo
echo "    $CRON_LINE"
echo
echo "  ⚠ СМЕНИ скомпрометированные секреты: ключ S3 (панель itecocloud) и пароль сервера."
