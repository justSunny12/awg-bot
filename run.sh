#!/usr/bin/env bash
#
# run.sh — форграунд-запуск бота БЕЗ systemd/демона (ручная проверка и отладка).
#
# Точка входа — пакет: python -m awgbot (обёртка awgbot/__main__.py). Конфиг и
# секреты config читает сам: боевые /etc/awg-bot/{conf,env}, если есть, иначе
# dev-раскладка в корне репо — ./conf/*.yaml и ./.env. Останов — Ctrl-C.
#
# Запуск:  ./run.sh
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"          # корень репозитория (рядом с awgbot/)

# venv, если собран установщиком/вручную; иначе системный python3.
PY="python3"
[[ -x venv/bin/python ]] && PY="venv/bin/python"

exec "$PY" -m awgbot
