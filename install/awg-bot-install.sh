#!/usr/bin/env bash
#
# awg-bot-install.sh — внешний установщик-bootstrap хоста бота awg-bot.
#
# Единственная задача — greenfield-подготовка «в одно окно»: развернуть код и
# передать управление внутреннему инструменту, который всё настроит. Сам НЕ
# настраивает и НЕ дублирует логику: конфигурация/venv/юнит/валидация живут в
# awg-bot.sh (внутри архива → /opt/awg-bot/awg-bot.sh).
#
# Флоу: root → найти awg-bot.tgz рядом → mkdir FHS → распаковать код в
# /opt/awg-bot → симлинк awg-bot → exec awg-bot.sh reconfigure --first-run,
# передав пути установщика и архива на самоочистку.
#
# Использование:
#   sudo ./awg-bot-install.sh            (архив awg-bot.tgz рядом со скриптом)
#   sudo ./awg-bot-install.sh <path.tgz>
#
set -euo pipefail

INSTALL_DIR="/opt/awg-bot"
ETC_DIR="/etc/awg-bot"
DATA_DIR="/var/lib/awg-bot"
SELF_LINK="/usr/local/bin/awg-bot"

c_info=$'\033[0;36m'; c_err=$'\033[0;31m'; c_off=$'\033[0m'
log() { printf '%s[install]%s %s\n' "$c_info" "$c_off" "$*"; }
die() { printf '%s[install:ОШИБКА]%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }

[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root: sudo $0 $*"

SELF_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SELF_DIR="$(dirname "$SELF_PATH")"

# ── найти архив: аргумент → рядом со скриптом → CWD ──────────────────────────
TGZ=""
if [[ -n "${1:-}" ]]; then
    [[ -f "$1" ]] || die "архив не найден: $1"
    TGZ="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
else
    for c in "$SELF_DIR"/awg-bot.tgz "$SELF_DIR"/awg-bot-*.tgz ./awg-bot.tgz; do
        [[ -f "$c" ]] && { TGZ="$(cd "$(dirname "$c")" && pwd)/$(basename "$c")"; break; }
    done
    [[ -n "$TGZ" ]] || die "рядом нет awg-bot.tgz — положи архив рядом или укажи путь: $0 <path.tgz>"
fi
log "архив: $TGZ"

# ── greenfield: FHS-каталоги + распаковка кода + симлинк ──────────────────────
if [[ -x "$INSTALL_DIR/venv/bin/python" ]]; then
    # Рабочая установка уже есть — не тупикуем, а предлагаем действия. Весь
    # функционал уже в установленном awg-bot; мы лишь вызываем его с нужным verb.
    BOT="$INSTALL_DIR/awg-bot.sh"
    printf '\n%s[install]%s awg-bot уже установлен в %s. Что делаем?\n' "$c_info" "$c_off" "$INSTALL_DIR" >&2
    printf '  1) Обновить код из этой поставки (awg-bot update)\n' >&2
    printf '  2) Восстановить из резервной копии (awg-bot restore)\n' >&2
    printf '  3) Удалить бота полностью (awg-bot uninstall)\n' >&2
    printf '  4) Ничего, выйти\n' >&2
    read -r -p "Выбор [1-4]: " __ch
    case "${__ch:-4}" in
        1) [[ -n "$TGZ" ]] || die "рядом нет awg-bot.tgz для обновления — положи архив рядом и повтори, либо: sudo awg-bot update <путь>"
           log "→ обновление из $TGZ"; exec "$BOT" update "$TGZ" ;;
        2) log "→ восстановление из резервной копии"; exec "$BOT" restore ;;
        3) log "→ удаление"; exec "$BOT" uninstall ;;
        *) die "выход — ничего не изменено (для действий: sudo awg-bot update|restore|uninstall)" ;;
    esac
fi
# Полу-остаток (каталог есть, но рабочего venv нет) — прерванный uninstall/распаковка.
# Не тупикуем: предлагаем дочистить и продолжить с нуля.
if [[ -e "$INSTALL_DIR" ]]; then
    printf '[install:!] найден остаток прошлой установки в %s (без рабочего venv).\n' "$INSTALL_DIR" >&2
    read -r -p "Удалить его и установить с нуля? [Y/n]: " __a; __a="${__a:-y}"
    [[ "${__a,,}" == "y" ]] || die "прервано — уберите $INSTALL_DIR вручную и повторите"
    rm -rf "$INSTALL_DIR"; rm -f "$SELF_LINK"
fi
log "создаю каталоги ($INSTALL_DIR, $ETC_DIR, $DATA_DIR)…"
mkdir -p "$INSTALL_DIR" "$ETC_DIR" "$DATA_DIR"
chmod 700 "$DATA_DIR"

log "распаковываю код в $INSTALL_DIR…"
tar xzf "$TGZ" -C "$INSTALL_DIR"
# архив может содержать один верхний каталог — нормализуем
if [[ ! -f "$INSTALL_DIR/awgbot/__main__.py" ]]; then
    sub="$(ls -d "$INSTALL_DIR"/*/ 2>/dev/null | head -n1 || true)"
    [[ -n "$sub" && -f "$sub/awgbot/__main__.py" ]] || die "в архиве нет awgbot/ — не та поставка?"
    ( shopt -s dotglob; mv "$sub"* "$INSTALL_DIR"/ ) && rmdir "$sub" 2>/dev/null || true
fi
[[ -f "$INSTALL_DIR/awg-bot.sh" ]] || die "в архиве нет awg-bot.sh — не та поставка?"
chmod +x "$INSTALL_DIR/awg-bot.sh"

log "симлинк $SELF_LINK → $INSTALL_DIR/awg-bot.sh"
ln -sf "$INSTALL_DIR/awg-bot.sh" "$SELF_LINK"

# ── передать управление внутреннему инструменту (он настроит и подчистит нас) ─
log "запускаю мастер настройки…"
exec "$INSTALL_DIR/awg-bot.sh" reconfigure --first-run --cleanup "$SELF_PATH" "$TGZ"
