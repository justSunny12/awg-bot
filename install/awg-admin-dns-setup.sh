#!/usr/bin/env bash
#
# awg-admin-dns-setup.sh — резолвер админа: второй экземпляр dnsmasq на
# собственном адресе (10.255.53.53/32 на dummy-интерфейсе), отдельно от
# резолвера клиентов (awg-resolver-setup.sh).
#
# ЗАЧЕМ. «SMB в туннеле»: SMB-серверы локальных сетей шлюзов видны по именам на
# устройствах админа — Finder (macOS, «Сеть» → awg.internal) и \\имя.awg.internal
# (Windows). Записи DNS-SD (RFC 6763) под awg.internal кладёт бот файлом
# записей; всё остальное этот резолвер отдаёт основному (server=<адрес
# резолвера клиентов>), и его защиты — кэш, блок DoH-эндпоинтов, rebind —
# наследуются. Отвечает он только устройствам админа: :53 его адреса файервол
# пускает с набора admin4 (infra/nftguard), остальным — drop. Отдельный
# экземпляр, а не записи в основном: имена локальных сетей не должны видеть
# прочие клиенты.
#
# УСТРОЙСТВО. Юнит awgbot-admin-dns.service: dummy-интерфейс с адресом /32,
# затем `dnsmasq -k --conf-file=<свой конфиг>` — свой конфиг целиком, без
# /etc/dnsmasq.conf и /etc/dnsmasq.d: однократные ключи основного экземпляра
# ему не мешают. Перезапуск без лимита попыток — как у основного резолвера.
# Интерфейс с адресом при остановке юнита НЕ снимается: пока демон лежит,
# запрос устройства админа получает отказ, а не уходит маршрутом по умолчанию к
# провайдеру (в forward файервол бота тоже режет этот адрес); снимает его
# только remove. install и records идут по одному (flock на LOCK): бот зовёт их
# с тика, канала и тумблера.
#
# РЕЖИМЫ:
#   install UPSTREAM   конфиг (server=UPSTREAM — адрес резолвера клиентов),
#                      юнит, запуск; идемпотентно, рестарт — только при
#                      изменении конфига или юнита. Не прошёл dnsmasq --test
#                      или не поднялся: при первой установке снимается всё
#                      (иначе бот счёл бы резолвер поставленным), иначе —
#                      прежний конфиг назад
#   plan UPSTREAM      что сделал бы install, ничего не меняя (без root)
#   records FILE|-     поставить файл записей (FILE) или снять его (-):
#                      dnsmasq --test, рестарт экземпляра, при отказе — прежний
#                      файл назад; строки файла проверяет бот (gwservices.lines_ok)
#   status             ADDR=… ACTIVE=0|1 UPSTREAM=… RECORDS=0|1; код 0 — жив
#   remove             остановить, снять юнит, конфиг и интерфейс
#
# КТО ЗОВЁТ. Установщик (awg-bot.sh ensure_resolver) — install сразу после
# резолвера клиентов; бот (infra/admindns.py) — install на каждом старте (живые
# хосты; сверка конфига и юнита со своими) и в финале переезда (новый адрес
# резолвера клиентов), records — по тумблеру, каналу и тику монитора;
# человек — `awg-bot resolver admin status|plan|install|remove`. Снимает его и
# `awg-bot uninstall` вместе с обвязкой РФ-доступа (без этого скрипта).
#
# ОКРУЖЕНИЕ: ADMIN_ADDR (10.255.53.53; бот и awg-bot.sh передают
# app.client_config.admin_dns), ADMIN_DIR (/etc/awgbot-admin-dns), ADMIN_IF
# (awgadmdns), ADMIN_UNIT (awgbot-admin-dns), UNIT_FILE
# (/etc/systemd/system/<ADMIN_UNIT>.service), LOCK (/run/<ADMIN_UNIT>.lock).
# Коды: 0 — сделано, 1 — ошибка, 2 — неверный вызов.

set -euo pipefail

ADMIN_ADDR="${ADMIN_ADDR:-10.255.53.53}"
ADMIN_DIR="${ADMIN_DIR:-/etc/awgbot-admin-dns}"
ADMIN_IF="${ADMIN_IF:-awgadmdns}"
ADMIN_UNIT="${ADMIN_UNIT:-awgbot-admin-dns}"
CONF="$ADMIN_DIR/dnsmasq.conf"
RECORDS_DIR="$ADMIN_DIR/records.d"
RECORDS="$RECORDS_DIR/awg-smb.conf"
UNIT_FILE="${UNIT_FILE:-/etc/systemd/system/$ADMIN_UNIT.service}"

MODE="${1:-}"; ARG="${2:-}"
LOCK="${LOCK:-/run/$ADMIN_UNIT.lock}"
log()  { printf '[admin-dns] %s\n' "$*"; }
die()  { printf '[admin-dns:ОШИБКА] %s\n' "$*" >&2; exit 1; }
PLAN=0
valid_addr() { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; }

render_conf() {  # render_conf UPSTREAM — конфиг экземпляра в stdout
    cat <<EOF
# Сгенерировано awg-admin-dns-setup.sh — резолвер админа awg-bot (SMB в туннеле).
# Руками не править: переписывается ботом.
listen-address=$ADMIN_ADDR
bind-interfaces
port=53
no-resolv
no-hosts
server=$1
pid-file=/run/$ADMIN_UNIT.pid
cache-size=1000
conf-dir=$RECORDS_DIR,*.conf
EOF
}

render_unit() {
    local ip dnsmasq
    ip="$(command -v ip || echo /usr/sbin/ip)"
    dnsmasq="$(command -v dnsmasq || echo /usr/sbin/dnsmasq)"
    cat <<EOF
# Поставлено awg-admin-dns-setup.sh. Резолвер админа: имена SMB локальных сетей
# шлюзов для устройств админа в туннеле. Упал — у устройств админа нет DNS,
# поэтому поднимаем сам и без лимита попыток (адрес резолвера клиентов, куда
# он отдаёт остальное, появляется позже).
[Unit]
Description=awg-bot: резолвер админа (имена SMB локальных сетей шлюзов)
After=network-online.target dnsmasq.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStartPre=-$ip link add $ADMIN_IF type dummy
ExecStartPre=$ip addr replace $ADMIN_ADDR/32 dev $ADMIN_IF
ExecStartPre=$ip link set $ADMIN_IF up
ExecStart=$dnsmasq -k --conf-file=$CONF
# Интерфейс с адресом при остановке НЕ снимается: пока демон лежит, адрес
# остаётся локальным, и запрос устройства админа получает отказ, а не уходит
# маршрутом по умолчанию к провайдеру. Снимает его только «remove».
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

put_if_changed() {  # put_if_changed SRC DST MODE — 0: изменился, 1: тот же
    if [[ -f "$2" ]] && cmp -s "$1" "$2"; then rm -f "$1"; return 1; fi
    if [[ "$PLAN" -eq 1 ]]; then printf '  would: записать %s\n' "$2"; rm -f "$1"; return 0; fi
    mkdir -p "$(dirname "$2")"
    install -m "$3" "$1" "$2"; rm -f "$1"
    return 0
}

conf_test() {  # 0 — конфиг экземпляра принят dnsmasq (или проверить нечем)
    command -v dnsmasq >/dev/null 2>&1 || return 0
    dnsmasq --test --conf-file="$CONF" >/dev/null 2>&1
}

restart_unit() {
    systemctl daemon-reload
    systemctl enable "$ADMIN_UNIT" >/dev/null 2>&1 || true
    systemctl restart "$ADMIN_UNIT" && systemctl is-active --quiet "$ADMIN_UNIT"
}

take_lock() {  # install и records — по одному: бот зовёт их с тика, с канала и с тумблера
    command -v flock >/dev/null 2>&1 || return 0
    exec 9>"$LOCK"
    flock -w 60 9 || die "резолвер админа уже настраивается другим прогоном"
}

purge() {  # снять всё своё: юнит, интерфейс, конфиг
    systemctl disable --now "$ADMIN_UNIT" >/dev/null 2>&1 || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload || true
    ip link del "$ADMIN_IF" >/dev/null 2>&1 || true
    rm -rf "$ADMIN_DIR"
}

case "$MODE" in
    install|plan)
        [[ "$MODE" == "plan" ]] && PLAN=1
        valid_addr "$ARG" || { echo "нужен адрес резолвера клиентов: $0 $MODE 10.8.1.1" >&2; exit 2; }
        valid_addr "$ADMIN_ADDR" || { echo "ADMIN_ADDR не адрес: $ADMIN_ADDR" >&2; exit 2; }
        [[ "$PLAN" -eq 1 || "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root"
        if [[ "$PLAN" -eq 0 ]] && ! command -v dnsmasq >/dev/null 2>&1; then
            die "нет dnsmasq — сначала свой резолвер клиентов (awg-resolver-setup.sh install)"
        fi
        [[ "$PLAN" -eq 1 ]] || take_lock
        first=0; [[ -f "$CONF" ]] || first=1
        changed=0
        tmp="$(mktemp)"; render_conf "$ARG" > "$tmp"
        prev=""
        if [[ -f "$CONF" && "$PLAN" -eq 0 ]]; then prev="$(mktemp)"; cp "$CONF" "$prev"; fi
        put_if_changed "$tmp" "$CONF" 0644 && changed=1
        [[ "$PLAN" -eq 1 ]] || mkdir -p "$RECORDS_DIR"
        tmp="$(mktemp)"; render_unit > "$tmp"
        put_if_changed "$tmp" "$UNIT_FILE" 0644 && changed=1
        if [[ "$PLAN" -eq 1 ]]; then
            log "would: резолвер админа $ADMIN_ADDR → $ARG (юнит $ADMIN_UNIT)"
            exit 0
        fi
        if [[ "$changed" -eq 0 ]] && systemctl is-active --quiet "$ADMIN_UNIT"; then
            [[ -n "$prev" ]] && rm -f "$prev"
            log "без изменений: $ADMIN_ADDR → $ARG"
            exit 0
        fi
        if ! conf_test; then
            # конфиг не принят — работающий демон не трогаем
            if [[ -n "$prev" ]]; then cp "$prev" "$CONF"; rm -f "$prev"; fi
            [[ "$first" -eq 1 ]] && purge
            die "конфиг резолвера админа отклонён dnsmasq — dnsmasq --test --conf-file=$CONF"
        fi
        if ! restart_unit; then
            if [[ "$first" -eq 1 ]]; then
                # первая установка не поднялась — снять всё: иначе «поставлен» по
                # наличию конфига, и конфиги устройств админа ушли бы с мёртвым DNS
                purge
            elif [[ -n "$prev" ]]; then
                cp "$prev" "$CONF"; rm -f "$prev"; restart_unit || true
            fi
            die "резолвер админа не поднялся — journalctl -u $ADMIN_UNIT -o short-iso -e"
        fi
        [[ -n "$prev" ]] && rm -f "$prev"
        log "резолвер админа слушает $ADMIN_ADDR, остальное — к $ARG"
        ;;
    records)
        [[ -n "$ARG" ]] || { echo "нужен файл записей или «-»: $0 records <файл>|-" >&2; exit 2; }
        [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root"
        [[ -f "$CONF" ]] || die "резолвера админа нет — сначала install"
        take_lock
        mkdir -p "$RECORDS_DIR"
        prev=""
        if [[ -f "$RECORDS" ]]; then prev="$(mktemp)"; cp "$RECORDS" "$prev"; fi
        if [[ "$ARG" == "-" ]]; then
            [[ -f "$RECORDS" ]] || { log "записей и так нет"; exit 0; }
            rm -f "$RECORDS"
        else
            [[ -f "$ARG" ]] || { echo "нет файла $ARG" >&2; exit 2; }
            if [[ -f "$RECORDS" ]] && cmp -s "$ARG" "$RECORDS"; then
                rm -f "$prev"; log "записи не изменились"; exit 0
            fi
            install -m 0644 "$ARG" "$RECORDS"
        fi
        if ! conf_test; then
            # записи не приняты проверкой — работающий демон не трогаем
            if [[ -n "$prev" ]]; then cp "$prev" "$RECORDS"; else rm -f "$RECORDS"; fi
            rm -f "$prev"
            die "записи не приняты dnsmasq --test — прежние на месте"
        fi
        if ! restart_unit; then
            if [[ -n "$prev" ]]; then cp "$prev" "$RECORDS"; else rm -f "$RECORDS"; fi
            rm -f "$prev"
            restart_unit || true
            die "резолвер админа не поднялся с новыми записями — прежние на месте; journalctl -u $ADMIN_UNIT -o short-iso -e"
        fi
        rm -f "$prev"
        log "записи резолвера админа обновлены"
        ;;
    status)
        active=0; systemctl is-active --quiet "$ADMIN_UNIT" 2>/dev/null && active=1
        up=""; [[ -f "$CONF" ]] && up="$(sed -nE 's/^server=([0-9.]+)$/\1/p' "$CONF" | head -n 1)"
        rec=0; [[ -s "$RECORDS" ]] && rec=1
        printf 'ADDR=%s\nACTIVE=%s\nUPSTREAM=%s\nRECORDS=%s\n' "$ADMIN_ADDR" "$active" "$up" "$rec"
        [[ -f "$CONF" && "$active" -eq 1 ]]
        ;;
    remove)
        [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root"
        purge
        log "резолвер админа снят"
        ;;
    *)
        awk 'NR>1 && !/^#/{exit} NR>1' "$0"; exit 2 ;;
esac
