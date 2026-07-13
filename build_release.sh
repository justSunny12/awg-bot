#!/usr/bin/env bash
#
# build_release.sh — сборка поставок из репозитория. DEV-инструмент, в поставки
# НЕ входит.
#
# Формат — tar.gz (нативно для Linux, распаковка без доп. софта: tar+gzip есть
# на любом образе; unzip — нет). На продукт — ДВА артефакта: payload-архив +
# внешний bootstrap-установщик РЯДОМ с ним (сам установщик в архив не входит).
#
# Артефакты (в ./dist по умолчанию):
#   awg-bot.tgz          + awg-bot-install.sh     продукт (co-located хост awg+бот)
#   awg-bot-project.tgz  полный проект для разработки (с тестами)
#
# Запуск:  ./build_release.sh [OUT_DIR]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$ROOT/dist}"
mkdir -p "$OUT"
log() { printf '\033[0;36m[build]\033[0m %s\n' "$*"; }

_stage_copy() {  # _stage_copy SRC DEST_DIR — копия без питон-кэша
    local src="$1" dest="$2"; mkdir -p "$dest"; cp -r "$ROOT/$src" "$dest/"
    find "$dest" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$dest" -name '*.pyc' -delete 2>/dev/null || true
}
_targz() {  # _targz STAGE_DIR OUT_TGZ — собрать во временном, затем копировать
    local tmp; tmp="$(mktemp -u).tgz"
    ( cd "$1" && tar czf "$tmp" . ); cp -f "$tmp" "$2"; rm -f "$tmp"
}
_count() { tar tzf "$1" | grep -vc '/$'; }

# ── продукт: бот (payload awg-bot.tgz + bootstrap awg-bot-install.sh рядом) ───
build_bot() {
    local s; s="$(mktemp -d)"
    for p in awgbot tools conf; do _stage_copy "$p" "$s"; done
    mkdir -p "$s/install"
    cp "$ROOT/install/harden_firewall.sh" "$s/install/"
    cp "$ROOT/awg-bot.sh" "$s/"; chmod +x "$s/awg-bot.sh"           # единый инструмент — в корне
    cp "$ROOT/awg-bot.service" "$ROOT/run.sh" "$ROOT/requirements.txt" "$ROOT/.env.example" "$s/"
    cp "$ROOT/docs/README-bot.md" "$s/README.md"
    _targz "$s" "$OUT/awg-bot.tgz"; rm -rf "$s"
    install -m 0755 "$ROOT/install/awg-bot-install.sh" "$OUT/awg-bot-install.sh"   # bootstrap РЯДОМ
    log "awg-bot.tgz: $(_count "$OUT/awg-bot.tgz") файлов  (+ awg-bot-install.sh рядом)"
}

# ── полный проект (dev) ──────────────────────────────────────────────────────
build_project() {
    local tmp; tmp="$(mktemp -u).tgz"
    ( cd "$ROOT" && tar czf "$tmp" \
        --exclude='./.git' --exclude='*/__pycache__' --exclude='*.pyc' \
        --exclude='./data' --exclude='*.db' --exclude='./.pytest_cache' \
        --exclude='./venv' --exclude='./dist' . )
    cp -f "$tmp" "$OUT/awg-bot-project.tgz"; rm -f "$tmp"
    log "awg-bot-project.tgz: $(_count "$OUT/awg-bot-project.tgz") файлов"
}

build_bot
build_project
log "готово → $OUT"
