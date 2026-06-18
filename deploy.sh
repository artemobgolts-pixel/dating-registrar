#!/usr/bin/env bash
# Локальный деплой: прогнать тесты → закоммитить → запушить в GitHub.
# Запуск из корня date4you:  ./deploy.sh "что изменил"
#
# После пуша зайди на сервер (по паролю / через панель хостера) и выполни:
#     cd /opt/date4you && ./update.sh
# — он подтянет этот коммит и пересоберёт контейнер.
set -euo pipefail

MSG="${1:-}"
if [ -z "$MSG" ]; then
  echo "Использование: ./deploy.sh \"описание изменений\"" >&2
  exit 1
fi

# 1. Тесты — на прод не уходит красное. Windows-консоль требует UTF-8.
echo "→ Тесты…"
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/test_smoke.py

# 2. Коммит (только если есть изменения).
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -m "$MSG"
else
  echo "→ Нет изменений для коммита — пушу то, что есть."
fi

# 3. Пуш в main.
git push origin main
echo
echo "✓ Запушено. Теперь на сервере:  cd /opt/date4you && ./update.sh"
