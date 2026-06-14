#!/bin/sh
# Стартовый скрипт контейнера.
#
# Запускается от root, при необходимости чинит владельца /data (старые
# установки могли создать каталог от root — иначе SQLite падает с
# «attempt to write a readonly database») и понижает привилегии до appuser.
set -eu

mkdir -p /data /data/uploads /data/backups

# chown -R только если нашлось хоть что-то не от appuser:
# обычный старт мгновенный, «переехавшая» установка чинится один раз.
if find /data ! -user appuser -print -quit | grep -q .; then
    echo "[entrypoint] выравниваю права на /data под appuser"
    chown -R appuser:appuser /data || true
fi

# Fail-fast: лучше упасть сразу с понятной ошибкой, чем ловить в рантайме
# криптичный sqlite3.OperationalError в restart-цикле.
if find /data ! -user appuser -print -quit | grep -q .; then
    echo "[entrypoint] ОШИБКА: не удалось выдать appuser права на /data." >&2
    echo "[entrypoint] Выполни на хосте:  chown -R 10001:10001 ./data  — и перезапусти." >&2
    exit 1
fi

exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
