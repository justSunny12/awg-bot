#!/usr/bin/env bash
#
# harden_firewall.sh — обёртка. Файервол хоста теперь ЕДИНСТВЕННАЯ точка:
# nft-таблица awg_bot_guard, которую ведёт бот (awgbot/infra/nftguard.py), а
# настраивает `awg-bot firewall setup` (tools/firewall.py). Скрипт оставлен для
# установщика и привычки — он лишь запускает мастер.
#
# Что делает мастер: спрашивает ВАШИ адреса для SSH (IP/CIDR/имя), прочие порты
# хоста, показывает таблицу, применяет её с ТАЙМЕРОМ ОТКАТА и просит проверить
# вход НОВЫМ подключением; `awg-bot firewall confirm` снимает таймер и, по
# желанию, выключает ufw (второй владелец правил там больше не нужен).
#
# ⚠️  Не сводите SSH-вайтлист к адресу VPN-туннеля: туннель живёт на этом же
#     сервере, и упавший awg оставил бы вас без входа. Внешние белые IP
#     обязательны (нет статики — DynamicDNS: имя в вайтлисте резолвится ботом).

# «sh script.sh» на Ubuntu означает dash — перезапускаем себя bash'ем
[ -z "${BASH_VERSION:-}" ] && exec bash "$0" "$@"
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY=""
for cand in "$ROOT/venv/bin/python" "$ROOT/.venv/bin/python" "/opt/awg-bot/venv/bin/python"; do
    [[ -x "$cand" ]] && { PY="$cand"; break; }
done
[[ -n "$PY" ]] || { echo "[firewall:ОШИБКА] не найден python окружения бота (venv)" >&2; exit 1; }
[[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "[firewall:ОШИБКА] нужен root (sudo $0)" >&2; exit 1; }

export AWG_BOT_CONF_DIR="${AWG_BOT_CONF_DIR:-/etc/awg-bot/conf}"
export AWG_BOT_DATA_DIR="${AWG_BOT_DATA_DIR:-/var/lib/awg-bot}"
export AWG_BOT_ENV="${AWG_BOT_ENV:-/etc/awg-bot/env}"
cd "$ROOT" && exec "$PY" -m tools.firewall setup "$@"
