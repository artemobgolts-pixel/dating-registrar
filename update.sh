#!/usr/bin/env bash
# Серверный апдейт: подтянуть свежий код и пересобрать контейнер.
# Запуск на сервере из каталога проекта:  cd /opt/boris-site && ./update.sh
#
# Безопасно для данных: data/ и .env не в git, pull их не трогает.
set -euo pipefail

echo "→ Забираю свежий код…"
git pull --ff-only origin main

# --build нужен, если менялся Python-код или зависимости. Для чистой
# статики/шаблонов хватило бы 'restart', но --build надёжнее и не намного
# дольше при кэше слоёв Docker.
echo "→ Пересобираю и поднимаю…"
docker compose up -d --build

echo "→ Жду healthcheck…"
sleep 8
docker compose ps

echo
echo "✓ Готово. Логи: docker compose logs -f app"
echo "  Откат:  git log --oneline (взять прошлый хеш) → git checkout <хеш> && docker compose up -d --build"
