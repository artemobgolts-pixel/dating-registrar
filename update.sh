#!/usr/bin/env bash
# Никаких pull/main/rebuild: заранее подготовленный artifact полного SHA.
set -euo pipefail
cd "$(dirname "$0")"
if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Использование: ./update.sh <полный подготовленный SHA>; см. docs/release.md' >&2
  exit 2
fi
exec "${PYTHON:-python3}" scripts/release.py deploy --sha "$1"
