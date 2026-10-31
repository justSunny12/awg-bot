#!/bin/sh
#
# reserve-pair-setup.sh — линк пары серверов AWG (awgpair) на ВПС.
#
# Линк пары — AWG-туннель сервер ↔ сервер: внутри него канал пары (st, пакет
# переезда, переезд и возврат). Пересылки через него нет, кроме РФ-доступа
# через второй сервер: правила файервола и ip rule pref 151 ставит бот,
# AllowedIPs у просящего расширяет до 0.0.0.0/0 рантаймом (awg set), не в
# конфиге — скрипт этого не делает, а --reassert, смена ключей и --set-peer
# поднимают линк из конфига с узкими AllowedIPs (бот вернёт их на ближайшем
# такте). В конфиге AllowedIPs — только адрес другой стороны в /30, без
# Table = off (маршрут /30 ставит awg-quick, он и нужен; с 0.0.0.0/0 в конфиге
# awg-quick увёл бы в линк маршрут по умолчанию), keepalive с обеих сторон —
# по нему живость.
# Обфускация — как у линков шлюзов (Jc…H4 случайные, S2 ≠ S1 + 56).
# Адреса в /30: принявший — первый, пригласивший — второй (PAIR_PEER_ADDR /
# PAIR_SELF_ADDR переопределяют). Keepalive — client_config.keepalive_seconds
# из app.yaml (_APP_YAML; строка в двойных кавычках, иначе 25-35), или
# PAIR_KEEPALIVE.
#
# Режимы (окружение: PAIR_IF=awgpair, PAIR_CIDR=10.99.98.0/30, PAIR_PORT —
# свой ListenPort (reserve.pair_port), PAIR_ID — id приглашения; пути —
# CONF_DIR, UNIT, SELF_PATH; остальное — по режиму):
#   --stage      пригласивший: новые ключи обеих сторон и обфускация, конфиг со
#                штампом «# awg-bot: пара <id>» (признак сопряжения до кода;
#                правило файервола линка пары от штампа не зависит),
#                пир — сторона принявшего без PSK; линк поднят; на stdout —
#                PAIR_VPS_PRIV/PAIR_PEER_PRIV/PAIR_VPS_PUB/PAIR_PEER_PUB/JC…H4
#                строками key=value (их читает бот, в файл стороны принявшего
#                они не пишутся — уезжают в приглашении)
#   --join       принявший: конфиг из приглашения (PAIR_PRIV, PAIR_PEER_PUB,
#                PAIR_ENDPOINT host:port, JC…H4 из окружения), штамп, линк поднят
#   --rekey-stage   новые ключи и PSK (пригласивший): .next рядом, живые не тронуты;
#                   на stdout — ключи обеих сторон и PSK; принявший получает своё по каналу
#   --rekey-commit  .next → живой (прежний → .prev), штамп снят, линк переподнят
#   --rekey-revert  .next снять; был коммит — .prev назад
#   --rekey-drop    убрать .next и .prev
#   --set-peer      принявший после кода: конфиг с новыми ключами из окружения
#                   (PAIR_PRIV, PAIR_PEER_PUB, PAIR_PSK), без штампа, переподнять.
#                   Бот зовёт его только ПОСЛЕ того, как ответ pair_admin ушёл
#                   пригласившему: тот до этого слова живёт на ключах
#                   приглашения, и ответ по линку на новых ключах до него не
#                   дошёл бы
#   --rollback   снять линк и конфиг (отмена приглашения, срок, третий код,
#                удаление пары, пара без подтверждения сессией за 10 минут)
#   --adopt      конфиг есть (восстановление из бэкапа): копия скрипта, юнит,
#                линк переподнят; восстановление зовёт его для каждого
#                awgpair*.conf копии с PAIR_IF = имя файла
#   --down       опустить линк (ExecStop юнита)
#   --reassert   поднять после загрузки (ExecStart юнита)
#   --status     что стоит: конфиг, штамп, поднят ли
#
# Юнит — awg-pair.service (один линк пары на сервер); --stage, --join и
# --adopt ставят копию скрипта в /usr/local/sbin (её выполняет юнит) и юнит.
# Снимают линк, юнит и копию ещё awg-bot uninstall и переустановка со сносом
# данных (--down, затем файлы).
#
# Чего НЕ делает: файервол не трогает (таблицу перерисовывает бот: с линка
# пары — только канал пары, порт ListenPort открывается вместе с остальными
# конфигами awg), в базу не пишет, копию в /usr/local/sbin при --rollback не
# убирает.
set -u

PAIR_IF="$(printf '%s' "${PAIR_IF:-awgpair}" | tr -cd 'A-Za-z0-9_.-' | cut -c1-15)"
PAIR_CIDR="${PAIR_CIDR:-10.99.98.0/30}"
PAIR_PORT="${PAIR_PORT:-47101}"
PAIR_ID="${PAIR_ID:-}"
# ключи и адрес второй стороны — из окружения (их кладёт бот; в argv — никогда)
PAIR_PRIV="${PAIR_PRIV:-}"; PAIR_PEER_PUB="${PAIR_PEER_PUB:-}"; PAIR_PSK="${PAIR_PSK:-}"; PAIR_ENDPOINT="${PAIR_ENDPOINT:-}"
_cidr_base="${PAIR_CIDR%/*}"; _cidr_last="${_cidr_base##*.}"; _cidr_head="${_cidr_base%.*}"
# пригласивший — второй адрес, принявший — первый (как ВПС/шлюз у линка слота — наоборот:
# приглашение несёт адрес принявшего, он первый)
PAIR_PEER_ADDR="${PAIR_PEER_ADDR:-$_cidr_head.$(( _cidr_last + 1 ))}"
PAIR_SELF_ADDR="${PAIR_SELF_ADDR:-$_cidr_head.$(( _cidr_last + 2 ))}"
_APP_YAML="${_APP_YAML:-/etc/awg-bot/conf/app.yaml}"
_cfg_keepalive="$(awk -F'"' '/^  keepalive_seconds:/{print $2; exit}' "$_APP_YAML" 2>/dev/null || true)"
PAIR_KEEPALIVE="${PAIR_KEEPALIVE:-${_cfg_keepalive:-25-35}}"
case "$PAIR_KEEPALIVE" in ''|*[!0-9-]*|-*|*-|*-*-*) PAIR_KEEPALIVE="25-35" ;; esac
CONF_DIR="${CONF_DIR:-/etc/amnezia/amneziawg}"
CONF="$CONF_DIR/$PAIR_IF.conf"
UNIT="${UNIT:-/etc/systemd/system/awg-pair.service}"
SELF_PATH="${SELF_PATH:-/usr/local/sbin/reserve-pair-setup.sh}"
STAMP_PREFIX="# awg-bot: пара "

say() { printf '%s\n' "$*"; }
die() { say "ОШИБКА: $*"; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "нужен root"; }
rnd() { _r="$(od -An -N4 -tu4 /dev/urandom | tr -d ' ')"; echo $(( _r % ($2 - $1 + 1) + $1 )); }
h_range() { _lo=$(rnd "$1" $(( $2 - 200000 ))); printf '%s-%s' "$_lo" "$(( _lo + $(rnd 50000 150000) ))"; }

gen_obf() {
    JC=$(rnd 3 10); JMIN=$(rnd 8 15); JMAX=$(rnd 40 70)
    S1=$(rnd 15 150); S2=$(rnd 15 150); S3=$(rnd 15 150); S4=$(rnd 15 150)
    while [ $((S1 + 56)) -eq "$S2" ]; do S2=$(rnd 15 150); done
    H1=$(h_range 5 500000000); H2=$(h_range 500000001 1000000000)
    H3=$(h_range 1000000001 1500000000); H4=$(h_range 1500000001 2000000000)
}

# Значения от второго сервера (ключи, обфускация, адрес) — строками в конфиг:
# перевод строки в значении стал бы своей строкой конфига (`PostUp = …` от
# root). Бот проверяет их до вызова; здесь — второй рубеж, до записи файла.
valid_key() { case "$1" in *[!A-Za-z0-9+/=]*|*=?*|*[!=]) return 1 ;; esac; [ "${#1}" -eq 44 ]; }   # 43 base64 и «=»
valid_num() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; [ "${#1}" -le 10 ]; }
valid_obf() { case "$1" in *-*) valid_num "${1%%-*}" && valid_num "${1#*-}" ;; *) valid_num "$1" ;; esac; }
valid_ep() { case "$1" in ''|*[!]A-Za-z0-9.:[-]*) return 1 ;; esac; }
valid_addr() { case "$1" in ''|*[!0-9.]*) return 1 ;; esac; }
check_values() { # check_values SELF_ADDR PRIV PEER_PUB PEER_ADDR [PSK] [ENDPOINT] — и JC…H4, порт, keepalive
    valid_addr "$1" && valid_addr "$4" || die "адрес линка пары не того вида"
    valid_key "$2" && valid_key "$3" || die "ключ линка пары не того вида"
    [ -z "${5:-}" ] || valid_key "$5" || die "PSK линка пары не того вида"
    [ -z "${6:-}" ] || valid_ep "$6" || die "адрес второго сервера не того вида"
    valid_num "$PAIR_PORT" || die "порт линка пары не того вида"
    for v in JC JMIN JMAX S1 S2 S3 S4 H1 H2 H3 H4; do
        eval "_x=\${$v:-}"
        valid_obf "$_x" || die "параметр обфускации $v не того вида"
    done
}

write_conf() {   # write_conf FILE SELF_ADDR PRIV PEER_PUB PEER_ADDR [PSK] [ENDPOINT] [STAMP]
    _f="$1"; _addr="$2"; _priv="$3"; _ppub="$4"; _paddr="$5"; _psk="${6:-}"; _ep="${7:-}"; _stamp="${8:-}"
    check_values "$_addr" "$_priv" "$_ppub" "$_paddr" "$_psk" "$_ep"
    case "$_stamp" in *[!A-Za-z0-9_.-]*) die "id приглашения не того вида" ;; esac
    _um="$(umask)"; umask 077                     # приватный ключ — файл 0600 с рождения, не после chmod
    {
        say "# Линк пары серверов AWG (резерв). Сгенерировано reserve-pair-setup.sh."
        [ -n "$_stamp" ] && say "${STAMP_PREFIX}${_stamp}"
        say "[Interface]"
        say "Address = $_addr/30"
        say "ListenPort = $PAIR_PORT"
        say "PrivateKey = $_priv"
        say "Jc = ${JC}"; say "Jmin = ${JMIN}"; say "Jmax = ${JMAX}"
        say "S1 = ${S1}"; say "S2 = ${S2}"; say "S3 = ${S3}"; say "S4 = ${S4}"
        say "H1 = ${H1}"; say "H2 = ${H2}"; say "H3 = ${H3}"; say "H4 = ${H4}"
        say ""
        say "[Peer]"
        say "PublicKey = $_ppub"
        [ -n "$_psk" ] && say "PresharedKey = $_psk"
        say "AllowedIPs = $_paddr/32"
        [ -n "$_ep" ] && say "Endpoint = $_ep"
        say "PersistentKeepalive = $PAIR_KEEPALIVE"
    } > "$_f"
    umask "$_um"
    chmod 600 "$_f"
}

obf_from_env() {   # обфускация из окружения (приглашение / канал)
    for v in JC JMIN JMAX S1 S2 S3 S4 H1 H2 H3 H4; do
        eval "_x=\${$v:-}"
        [ -n "$_x" ] || die "нет $v в окружении"
    done
}

link_up() { ip link show "$PAIR_IF" >/dev/null 2>&1 && { awg-quick down "$PAIR_IF" >/dev/null 2>&1 || true; }; awg-quick up "$PAIR_IF"; }
link_down() { ip link show "$PAIR_IF" >/dev/null 2>&1 && awg-quick down "$PAIR_IF"; return 0; }

install_self() {
    [ -f "$SELF_PATH" ] && cmp -s "$0" "$SELF_PATH" && return 0
    install -m 0755 "$0" "$SELF_PATH" 2>/dev/null || return 1
}

write_unit() {
    cat > "$UNIT" <<UNITEOF
[Unit]
Description=awg-bot: линк пары серверов AWG (резерв)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=PAIR_IF=$PAIR_IF
ExecStart=$SELF_PATH --reassert
ExecStop=$SELF_PATH --down

[Install]
WantedBy=multi-user.target
UNITEOF
    systemctl daemon-reload
}

ensure_unit() {
    install_self || die "скрипт линка пары не встал в /usr/local/sbin"
    [ -f "$UNIT" ] && grep -q -- '--reassert' "$UNIT" || write_unit
    systemctl enable "awg-pair.service" >/dev/null 2>&1 || true
}

print_keys() {
    say "PAIR_VPS_PRIV=$VPS_PRIV"; say "PAIR_VPS_PUB=$VPS_PUB"
    say "PAIR_PEER_PRIV=$PEER_PRIV"; say "PAIR_PEER_PUB=$PEER_PUB"
    [ -n "${PSK:-}" ] && say "PAIR_PSK=$PSK"
    say "JC=$JC"; say "JMIN=$JMIN"; say "JMAX=$JMAX"
    say "S1=$S1"; say "S2=$S2"; say "S3=$S3"; say "S4=$S4"
    say "H1=$H1"; say "H2=$H2"; say "H3=$H3"; say "H4=$H4"
}

mode="${1:-}"
case "$mode" in
    --stage)
        need_root; [ -n "$PAIR_ID" ] || die "нет PAIR_ID"
        command -v awg >/dev/null 2>&1 || die "нет amneziawg-tools"
        VPS_PRIV="$(awg genkey)"; VPS_PUB="$(printf '%s' "$VPS_PRIV" | awg pubkey)"
        PEER_PRIV="$(awg genkey)"; PEER_PUB="$(printf '%s' "$PEER_PRIV" | awg pubkey)"
        gen_obf
        write_conf "$CONF" "$PAIR_SELF_ADDR" "$VPS_PRIV" "$PEER_PUB" "$PAIR_PEER_ADDR" "" "" "$PAIR_ID"
        link_up || die "линк пары не поднялся"
        ensure_unit
        print_keys
        say "stage: ok"
        ;;
    --join)
        need_root; [ -n "$PAIR_ID" ] || die "нет PAIR_ID"
        [ -n "${PAIR_PRIV:-}" ] && [ -n "${PAIR_PEER_PUB:-}" ] && [ -n "${PAIR_ENDPOINT:-}" ] || die "нет ключей или адреса в окружении"
        obf_from_env
        # принявший — первый адрес, пригласивший — второй
        write_conf "$CONF" "$PAIR_PEER_ADDR" "$PAIR_PRIV" "$PAIR_PEER_PUB" "$PAIR_SELF_ADDR" "" "$PAIR_ENDPOINT" "$PAIR_ID"
        link_up || die "линк пары не поднялся"
        ensure_unit
        say "join: ok"
        ;;
    --rekey-stage)
        need_root; [ -f "$CONF" ] || die "конфига линка пары нет"
        VPS_PRIV="$(awg genkey)"; VPS_PUB="$(printf '%s' "$VPS_PRIV" | awg pubkey)"
        PEER_PRIV="$(awg genkey)"; PEER_PUB="$(printf '%s' "$PEER_PRIV" | awg pubkey)"
        PSK="$(awg genpsk)"
        gen_obf
        _addr="$(awk -F'[ /]+' '/^Address/{print $3; exit}' "$CONF")"
        _paddr="$(awk -F'[ /]+' '/^AllowedIPs/{print $3; exit}' "$CONF")"
        _ep="$(awk '/^Endpoint/{print $3; exit}' "$CONF")"
        write_conf "$CONF.next" "$_addr" "$VPS_PRIV" "$PEER_PUB" "$_paddr" "$PSK" "$_ep" ""
        print_keys
        say "rekey: staged"
        ;;
    --rekey-commit)
        need_root; [ -f "$CONF.next" ] || die "нет $CONF.next"
        [ -f "$CONF" ] && cp -p "$CONF" "$CONF.prev"
        mv "$CONF.next" "$CONF"
        link_up || { [ -f "$CONF.prev" ] && mv "$CONF.prev" "$CONF" && link_up; die "линк на новых ключах не поднялся — прежние возвращены"; }
        say "rekey: committed"
        ;;
    --rekey-revert)
        need_root
        rm -f "$CONF.next"
        if [ -f "$CONF.prev" ]; then mv "$CONF.prev" "$CONF"; link_up || die "линк на прежних ключах не поднялся"; fi
        say "rekey: reverted"
        ;;
    --rekey-drop)
        need_root; rm -f "$CONF.next" "$CONF.prev"; say "rekey: dropped" ;;
    --set-peer)
        need_root; [ -f "$CONF" ] || die "конфига линка пары нет"
        [ -n "${PAIR_PRIV:-}" ] && [ -n "${PAIR_PEER_PUB:-}" ] || die "нет ключей в окружении"
        obf_from_env
        _addr="$(awk -F'[ /]+' '/^Address/{print $3; exit}' "$CONF")"
        _paddr="$(awk -F'[ /]+' '/^AllowedIPs/{print $3; exit}' "$CONF")"
        _ep="$(awk '/^Endpoint/{print $3; exit}' "$CONF")"
        check_values "$_addr" "$PAIR_PRIV" "$PAIR_PEER_PUB" "$_paddr" "${PAIR_PSK:-}" "$_ep"   # до .prev: отказ — файлы как были
        cp -p "$CONF" "$CONF.prev"
        write_conf "$CONF" "$_addr" "$PAIR_PRIV" "$PAIR_PEER_PUB" "$_paddr" "${PAIR_PSK:-}" "$_ep" ""
        link_up || { mv "$CONF.prev" "$CONF"; link_up; die "линк на новых ключах не поднялся — прежние возвращены"; }
        rm -f "$CONF.prev"
        say "peer: set"
        ;;
    --rollback)
        need_root
        systemctl disable --now awg-pair.service >/dev/null 2>&1 || true
        link_down
        rm -f "$CONF" "$CONF.next" "$CONF.prev" "$UNIT"
        systemctl daemon-reload
        say "rollback: ok"
        ;;
    --adopt)
        need_root; [ -f "$CONF" ] || die "конфига линка пары нет — принимать нечего"
        ensure_unit; link_up || die "линк пары не поднялся"; say "adopt: ok" ;;
    --reassert)
        [ -f "$CONF" ] || { say "линк пары не настроен — нечего поднимать"; exit 0; }
        ip link show "$PAIR_IF" >/dev/null 2>&1 || awg-quick up "$PAIR_IF"
        ;;
    --down) link_down ;;
    --status)
        if [ -f "$CONF" ]; then say "conf=present"; grep -q "^${STAMP_PREFIX}" "$CONF" && say "stamp=$(sed -n "s/^${STAMP_PREFIX}//p" "$CONF" | head -n1)" || say "stamp="
        else say "conf=absent"; fi
        ip link show "$PAIR_IF" >/dev/null 2>&1 && say "link=up" || say "link=down"
        ;;
    *) say "использование: $0 --stage|--join|--rekey-stage|--rekey-commit|--rekey-revert|--rekey-drop|--set-peer|--rollback|--adopt|--reassert|--down|--status"; exit 2 ;;
esac
