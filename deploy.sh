#!/usr/bin/env bash
# Проверка точного clean SHA. Commit/push выполняются явно и отдельно.
set -euo pipefail
cd "$(dirname "$0")"
if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Использование: ./deploy.sh <полный SHA>; Linux Python 3.12 + Chromium обязательны' >&2
  exit 2
fi
exec "${PYTHON:-python3}" scripts/release_gate.py --sha "$1" --mode release --output artifacts/release-gate
