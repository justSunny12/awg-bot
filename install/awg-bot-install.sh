#!/usr/bin/env bash
#
# awg-bot-install.sh — внешний установщик-bootstrap хоста бота awg-bot.
#
# Единственная задача — greenfield-подготовка «в одно окно»: развернуть код и
# передать управление внутреннему инструменту, который всё настроит. Сам НЕ
# настраивает и НЕ дублирует логику: конфигурация/venv/юнит/валидация живут в
# awg-bot.sh (внутри архива → /opt/awg-bot/awg-bot.sh).
#
# Флоу: root → взять код (распакованная поставка вокруг себя ИЛИ awg-bot.tgz
# рядом) → mkdir FHS → разложить код в /opt/awg-bot → симлинк awg-bot →
# exec awg-bot.sh reconfigure --first-run, передав на самоочистку всё
# временное: архив и каталог распаковки.
#
# ОСНОВНОЙ СПОСОБ — одной командой, поставка едет целиком одним архивом:
#   cd "$(mktemp -d)" \
#     && curl -fsSLO https://github.com/<repo>/releases/latest/download/awg-bot.tgz \
#     && tar xzf awg-bot.tgz && sudo bash install/awg-bot-install.sh
#
# Использование:
#   sudo bash install/awg-bot-install.sh   (из распакованной поставки)
#   sudo ./awg-bot-install.sh              (архив awg-bot.tgz рядом со скриптом)
#   sudo ./awg-bot-install.sh <path.tgz>
#   sudo ./awg-bot-install.sh --role gateway [<path.tgz>]   # агент на шлюзе
#   sudo ./awg-bot-install.sh --port 51820 [--subnet 10.8.1]  # порт/подсеть создаваемого
#                                                            # awg-сервера (иначе порт случайный)
#   sudo ./awg-bot-install.sh --advanced      # спросить всё, как в прежних версиях
#   sudo ./awg-bot-install.sh --skip-verify   # своя сборка: не сверять sha256 с релизом
#
set -euo pipefail

INSTALL_DIR="/opt/awg-bot"
ETC_DIR="/etc/awg-bot"
DATA_DIR="/var/lib/awg-bot"
SELF_LINK="/usr/local/bin/awg-bot"

c_info=$'\033[0;36m'; c_err=$'\033[0;31m'; c_off=$'\033[0m'
log() { printf '%s[install]%s %s\n' "$c_info" "$c_off" "$*"; }
die() { printf '%s[install:ОШИБКА]%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }

verify_archive() {  # verify_archive TGZ SRC_ROOT — сверить sha256 с релизом GitHub
    # Не обязательный шаг: у GitHub есть лимит на анонимные запросы, а сети
    # может не быть вовсе. Ответил — сверяем и на расхождении отказываемся
    # ставить; не ответил — говорим об этом одной строкой и продолжаем, потому
    # что целостность загрузки и так держит TLS.
    local tgz="$1" root="$2" ver repo want have
    [[ "$SKIP_VERIFY" != "1" ]] || { log "целостность: сверка отключена (--skip-verify)"; return 0; }
    [[ -f "$tgz" ]] || return 0
    command -v sha256sum >/dev/null 2>&1 || return 0
    ver="$(sed -nE 's/^__version__ *= *"([^"]+)".*/\1/p' "$root/awgbot/__version__.py" 2>/dev/null | head -n1)"
    repo="$(sed -nE 's/^ *repo: *"?([^"#]+)"?.*/\1/p' "$root/conf/updates.yaml" 2>/dev/null | head -n1 | tr -d ' ')"
    [[ -n "$ver" && -n "$repo" ]] || return 0
    want="$(curl -fsS --max-time 10 "https://api.github.com/repos/$repo/releases/tags/v$ver" 2>/dev/null \
            | sed -nE 's/.*"digest" *: *"sha256:([0-9a-f]{64})".*/\1/p' | head -n1)"
    if [[ -z "$want" ]]; then
        log "целостность: GitHub не ответил — ставлю без сверки (загрузку прикрывает TLS)"
        return 0
    fi
    have="$(sha256sum "$tgz" | awk '{print $1}')"
    [[ "$have" == "$want" ]] || die "sha256 архива не совпал с релизом v$ver — ставить не буду. Скачай архив заново; если это своя сборка — повтори с --skip-verify"
    log "целостность: sha256 совпал с релизом v$ver"
}

[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root: sudo $0 $*"

SELF_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SELF_DIR="$(dirname "$SELF_PATH")"

# ── ключи: --role gateway (или AWG_BOT_ROLE=gateway), --port N, --subnet X.Y.Z ──
ROLE="${AWG_BOT_ROLE:-client}"
SKIP_VERIFY=0                  # своя сборка: sha256 с релизом сверять не с чем
EXTRA=()                       # что уходит дальше в reconfigure --first-run
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --role)   ROLE="${2:-client}"; shift 2 ;;
        --port)   [[ "${2:-}" =~ ^[0-9]+$ ]] || die "--port: число"; EXTRA+=(--port "$2"); shift 2 ;;
        --subnet) [[ "${2:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--subnet: три октета вида 10.8.1"; EXTRA+=(--subnet "$2"); shift 2 ;;
        --advanced) EXTRA+=(--advanced); shift ;;
        --skip-verify) SKIP_VERIFY=1; shift ;;
        *) die "неизвестный ключ: $1" ;;
    esac
done

# ── откуда брать код ─────────────────────────────────────────────────────────
# Поставка теперь ОДИН архив, и установщик едет внутри него: обычный путь —
# «распаковали, запустили отсюда». Тогда код лежит вокруг нас, и распаковывать
# нечего. Архив рядом остаётся для случая, когда скрипт вытащили отдельно.
TGZ=""
SRC_ROOT=""
UNPACK_ROOT="$(cd "$SELF_DIR/.." && pwd)"
if [[ -z "${1:-}" && -f "$UNPACK_ROOT/awgbot/__main__.py" && -f "$UNPACK_ROOT/awg-bot.sh" ]]; then
    SRC_ROOT="$UNPACK_ROOT"
    log "поставка распакована здесь: $SRC_ROOT"
    # Архив, если он остался рядом (скачали файлом, а не потоком) — уберём в конце
    for c in "$SRC_ROOT"/awg-bot.tgz "$SRC_ROOT"/../awg-bot.tgz; do
        [[ -f "$c" ]] && { TGZ="$(cd "$(dirname "$c")" && pwd)/$(basename "$c")"; break; }
    done
else
    if [[ -n "${1:-}" ]]; then
        [[ -f "$1" ]] || die "архив не найден: $1"
        TGZ="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
    else
        for c in "$SELF_DIR"/awg-bot.tgz "$SELF_DIR"/awg-bot-*.tgz ./awg-bot.tgz; do
            [[ -f "$c" ]] && { TGZ="$(cd "$(dirname "$c")" && pwd)/$(basename "$c")"; break; }
        done
        [[ -n "$TGZ" ]] || die "нет ни распакованной поставки рядом, ни awg-bot.tgz — скачай архив и распакуй его: tar xzf awg-bot.tgz && sudo bash install/awg-bot-install.sh"
    fi
    log "архив: $TGZ"
fi

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

if [[ -n "$SRC_ROOT" ]]; then
    verify_archive "$TGZ" "$SRC_ROOT"
    log "раскладываю код в ${INSTALL_DIR}…"
    ( shopt -s dotglob; cp -a "$SRC_ROOT"/. "$INSTALL_DIR"/ )
    # Установщик в /opt не нужен: там живёт awg-bot.sh, который делает всё
    # дальнейшее (update, restore, uninstall).
    rm -f "$INSTALL_DIR/install/awg-bot-install.sh"
else
    log "распаковываю код в ${INSTALL_DIR}…"
    tar xzf "$TGZ" -C "$INSTALL_DIR"
    # архив может содержать один верхний каталог — нормализуем
    if [[ ! -f "$INSTALL_DIR/awgbot/__main__.py" ]]; then
        sub="$(ls -d "$INSTALL_DIR"/*/ 2>/dev/null | head -n1 || true)"
        [[ -n "$sub" && -f "$sub/awgbot/__main__.py" ]] || die "в архиве нет awgbot/ — не та поставка?"
        ( shopt -s dotglob; mv "$sub"* "$INSTALL_DIR"/ ) && rmdir "$sub" 2>/dev/null || true
    fi
fi
[[ -f "$INSTALL_DIR/awg-bot.sh" ]] || die "в архиве нет awg-bot.sh — не та поставка?"
chmod +x "$INSTALL_DIR/awg-bot.sh"

log "симлинк $SELF_LINK → $INSTALL_DIR/awg-bot.sh"
ln -sf "$INSTALL_DIR/awg-bot.sh" "$SELF_LINK"

# ── передать управление внутреннему инструменту (он настроит и подчистит нас) ─
log "запускаю мастер настройки…"
# Убрать за собой: архив и каталог распаковки. Каталог сносим ТОЛЬКО если он
# временный (/tmp, /var/tmp) — человек мог распаковать поставку и к себе в
# домашний каталог, и удалять её там мы не вправе.
CLEANUP_DIR=""
case "${SRC_ROOT:-}" in
    /tmp/*|/var/tmp/*|/private/tmp/*) CLEANUP_DIR="$SRC_ROOT" ;;
esac
exec "$INSTALL_DIR/awg-bot.sh" reconfigure --first-run --role "$ROLE" ${EXTRA[@]+"${EXTRA[@]}"} \
     --cleanup "${CLEANUP_DIR:-$SELF_PATH}" "$TGZ"
