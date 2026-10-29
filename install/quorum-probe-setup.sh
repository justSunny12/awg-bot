#!/bin/sh
#
# quorum-probe-setup.sh — пространство awg-quo и интерфейсы зондов кворума
# резерва (концепт «кворум» §3.1); обе роли: сервер AWG и шлюз.
#
# Зонд — AWG-интерфейс голосующего с одним пиром (ответчик на интерфейсе AWG
# основы линии или на её клоне в зоне второго сервера). Интерфейс создаётся у
# хоста (UDP-сокет остаётся у хоста: шифрованный пакет уходит маршрутом хоста
# — у шлюза через провайдера локальной сети, как телефон пользователя), затем
# переносится в пространство awg-quo и там поднимается. В пространстве: адрес
# зонда /32, маршрут /32 до ответчика, таблица inet awg_quo — вход только
# ответы, выход только udp на порт ответчика, пересылки нет, маршрута по
# умолчанию нет: выхода в интернет у зонда нет по построению. Прежний код
# (агент без кворума, скрипт обвязки) интерфейсов в пространстве не видит.
#
# Конфиг зонда — [Interface] (Address, PrivateKey, обфускация Jc…H4, I1) и
# один [Peer] (PublicKey, PresharedKey, AllowedIPs — /32 ответчика, Endpoint
# — IP сервера:порт); PersistentKeepalive не нужен — сессию держат пробы.
# awg setconf не знает Address/MTU/DNS/Table — они снимаются перед ним.
#
# Режимы (root, кроме --status):
#   --up <conf>     QUO_IF=<имя> (иначе имя файла без .conf): конфиг → $QUO_CONF_DIR/<имя>.conf
#                   (0600), пространство, интерфейс, адрес, маршрут, таблица. Тот же конфиг при
#                   стоящем интерфейсе — «up: same»; другой — awg syncconf (сессия не рвётся);
#                   не вышло — пересоздание
#   --down <имя>    интерфейс и конфиг прочь; пространство остаётся
#   --wipe          все интерфейсы, конфиги и пространство прочь
#   --status        key=value: ns=present|absent, ifaces=<через пробел>, hs_<имя>=<секунд с
#                   рукопожатия|never>, conf_<имя>=present
#   --selftest      «сокет у хоста» на модуле ядра этой машины: интерфейс, созданный у хоста и
#                   перенесённый в пространство, принимает пакеты через сокет хоста; код 0 — да
# Окружение: QUO_NS (awg-quo), QUO_CONF_DIR (/var/lib/awg-bot/quorum), QUO_PORT (8789),
# QUO_IF (имя интерфейса для --up), QUO_SELFTEST_PORT (45998).
# Имена интерфейсов — awgq<линия><цель>: awgqah — зонд к основе линии a, awgqar — к её клону.
set -u

NS="${QUO_NS:-awg-quo}"
CONF_DIR="${QUO_CONF_DIR:-/var/lib/awg-bot/quorum}"
PORT="${QUO_PORT:-8789}"
NSP="/run/netns/$NS"

say() { printf '%s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "нужен root"; }
nsx() { ip netns exec "$NS" "$@"; }

conf_val() {   # conf_val FILE KEY — первое значение ключа без комментария
    sed -n "s/^[[:space:]]*$2[[:space:]]*=[[:space:]]*//p" "$1" | head -n1 | sed 's/[[:space:]]*#.*$//'
}

conf_check() {   # только известные ключи, один Peer, AllowedIPs — один /32
    [ -f "$1" ] || return 1
    grep -q '^[[:space:]]*PrivateKey' "$1" || return 1
    [ "$(grep -c '^\[Peer\]' "$1")" = "1" ] || return 1
    grep -q '^[[:space:]]*Endpoint' "$1" || return 1
    ! grep -Eqi '^[[:space:]]*(PostUp|PostDown|PreUp|PreDown|SaveConfig|DNS|Table)' "$1" || return 1
    _a="$(conf_val "$1" AllowedIPs)"
    case "$_a" in *,*|"") return 1 ;; esac
    case "$_a" in */32) ;; *) return 1 ;; esac
    _addr="$(conf_val "$1" Address)"
    case "$_addr" in */32) ;; *) return 1 ;; esac
    return 0
}

ns_ensure() {
    [ -e "$NSP" ] || ip netns add "$NS" || die "пространство $NS не создалось"
    nsx ip link set lo up
    nsx sysctl -qw net.ipv4.ip_forward=0 2>/dev/null || true
}

resp_set() {   # /32 ответчиков всех стоящих конфигов — набор resp4
    for f in "$CONF_DIR"/*.conf; do [ -f "$f" ] || continue; conf_val "$f" AllowedIPs; done | sed '/^$/d' | sort -u | tr '\n' ' '
}

table_write() {
    _r="$(resp_set)"
    _el=""; for x in $_r; do _el="$_el${_el:+, }$x"; done
    nsx nft -f - <<NFT || die "nft отверг таблицу $NS"
table inet awg_quo
delete table inet awg_quo
table inet awg_quo {
    set resp4 { type ipv4_addr; flags interval; $( [ -n "$_el" ] && printf 'elements = { %s }' "$_el" ) }
    chain input {
        type filter hook input priority filter; policy drop;
        iifname "lo" accept
        ct state established,related accept
        ct state invalid drop
    }
    chain output {
        type filter hook output priority filter; policy drop;
        oifname "lo" accept
        ct state established,related accept
        ip daddr @resp4 udp dport $PORT accept
    }
    chain forward {
        type filter hook forward priority filter; policy drop;
    }
}
NFT
}

setconf_of() {   # setconf_of CONF OUT — без расширений awg-quick
    grep -v -E '^[[:space:]]*(Address|MTU|DNS|Table)[[:space:]]*=' "$1" > "$2"; chmod 0600 "$2"
}

iface_up() {   # iface_up NAME CONF
    _name="$1"; _conf="$2"; _tmp="awgq$$"
    _sc="${CONF_DIR:?}/.${_name:?}.setconf"; setconf_of "$_conf" "$_sc"
    _addr="$(conf_val "$_conf" Address)"; _resp="$(conf_val "$_conf" AllowedIPs)"
    if nsx ip link show "$_name" >/dev/null 2>&1; then
        if nsx awg syncconf "$_name" "$_sc"; then
            rm -f -- "${_sc:?}"
            nsx ip addr replace "$_addr" dev "$_name"
            nsx ip route replace "$_resp" dev "$_name"
            return 0
        fi
        nsx ip link del "$_name" 2>/dev/null || true
    fi
    ip link add "$_tmp" type amneziawg || { rm -f -- "${_sc:?}"; return 1; }
    awg setconf "$_tmp" "$_sc" || { rm -f -- "${_sc:?}"; ip link del "$_tmp"; return 1; }
    rm -f -- "${_sc:?}"
    ip link set "$_tmp" netns "$NS" || { ip link del "$_tmp"; return 1; }
    nsx ip link set "$_tmp" name "$_name"
    nsx ip addr replace "$_addr" dev "$_name"
    _mtu="$(conf_val "$_conf" MTU)"; [ -n "$_mtu" ] && nsx ip link set "$_name" mtu "$_mtu"
    nsx ip link set "$_name" up
    nsx ip route replace "$_resp" dev "$_name"
}

cmd_up() {
    need_root
    [ -n "${1:-}" ] && [ -f "$1" ] || die "нет файла конфига"
    conf_check "$1" || die "конфиг зонда не прошёл проверку"
    _name="${QUO_IF:-$(basename "$1" .conf)}"
    _name="$(printf '%s' "$_name" | tr -cd 'A-Za-z0-9_' | cut -c1-15)"
    case "$_name" in awgq*) ;; *) die "имя интерфейса зонда — awgq<линия><цель>" ;; esac
    command -v awg >/dev/null 2>&1 || die "нет amneziawg-tools (awg)"
    modprobe amneziawg 2>/dev/null || true
    mkdir -p "$CONF_DIR"; chmod 0700 "$CONF_DIR"
    _dst="${CONF_DIR:?}/${_name:?}.conf"
    ns_ensure
    if [ -f "$_dst" ] && cmp -s "$1" "$_dst" && nsx ip link show "$_name" >/dev/null 2>&1; then
        table_write; say "up: same"; return 0
    fi
    install -m 0600 "$1" "$_dst"
    iface_up "$_name" "$_dst" || { rm -f -- "${_dst:?}"; die "интерфейс зонда $_name не поднялся"; }
    table_write
    say "up: ok"
}

cmd_down() {
    need_root
    _name="$(printf '%s' "${1:-}" | tr -cd 'A-Za-z0-9_' | cut -c1-15)"
    [ -n "$_name" ] || die "нет имени интерфейса"
    [ -e "$NSP" ] && nsx ip link del "$_name" 2>/dev/null
    rm -f -- "${CONF_DIR:?}/${_name:?}.conf" "${CONF_DIR:?}/.${_name:?}.setconf"
    [ -e "$NSP" ] && table_write
    say "down: ok"
}

cmd_wipe() {
    need_root
    if [ -e "$NSP" ]; then
        for i in $(nsx ip -o link show 2>/dev/null | awk -F': ' '$2 ~ /^awgq/ {print $2}'); do nsx ip link del "$i" 2>/dev/null; done
        ip netns del "$NS" 2>/dev/null
    fi
    for f in "${CONF_DIR:?}"/*.conf "${CONF_DIR:?}"/.*.setconf; do [ -f "$f" ] && rm -f -- "$f"; done
    rmdir "${CONF_DIR:?}" 2>/dev/null || true
    say "wipe: ok"
}

cmd_status() {
    if [ -e "$NSP" ]; then say "ns=present"; else say "ns=absent"; say "ifaces="; return 0; fi
    _ifs="$(nsx ip -o link show 2>/dev/null | awk -F': ' '$2 ~ /^awgq/ {print $2}' | tr '\n' ' ')"
    say "ifaces=${_ifs% }"
    for i in $_ifs; do
        _hs="$(nsx awg show "$i" latest-handshakes 2>/dev/null | awk 'NR==1{print $2}')"
        if [ -n "$_hs" ] && [ "$_hs" != "0" ]; then say "hs_$i=$(( $(date +%s) - _hs ))"; else say "hs_$i=never"; fi
        [ -f "$CONF_DIR/$i.conf" ] && say "conf_$i=present" || say "conf_$i=absent"
    done
}

# ── --selftest ──────────────────────────────────────────────────────────────
ST_Z="awgquotst-z"; ST_C="awgquotst-c"; ST_VH="awgquotv0"; ST_VC="awgquotv1"; ST_IF="awgquotst"
ST_PORT="${QUO_SELFTEST_PORT:-45998}"
st_cleanup() { ip netns del "$ST_Z" 2>/dev/null; ip netns del "$ST_C" 2>/dev/null; ip link del "$ST_VH" 2>/dev/null; ip link del "$ST_IF" 2>/dev/null; }
cmd_selftest() {
    need_root
    command -v awg >/dev/null 2>&1 || die "нет amneziawg-tools (awg)"
    modprobe amneziawg 2>/dev/null || true
    trap st_cleanup EXIT
    st_cleanup
    ok_sock=0; ok_ping=0
    ip netns add "$ST_Z" || die "пространство не создалось"
    ip netns add "$ST_C" || die "пространство не создалось"
    ip link add "$ST_VH" type veth peer name "$ST_VC" || die "veth не создался"
    ip link set "$ST_VC" netns "$ST_C"
    ip addr add 192.0.2.5/30 dev "$ST_VH"; ip link set "$ST_VH" up
    ip netns exec "$ST_C" ip addr add 192.0.2.6/30 dev "$ST_VC"
    ip netns exec "$ST_C" ip link set "$ST_VC" up
    ip netns exec "$ST_C" ip link set lo up
    sp="$(awg genkey)"; spub="$(printf '%s' "$sp" | awg pubkey)"
    cp="$(awg genkey)"; cpub="$(printf '%s' "$cp" | awg pubkey)"
    # «ответчик» — у хоста (сокет хоста, интерфейс у хоста), «зонд» — создан у клиента и перенесён
    ip link add "$ST_IF" type amneziawg || die "интерфейс amneziawg не создался"
    printf '%s' "$sp" | awg set "$ST_IF" private-key /dev/stdin listen-port "$ST_PORT" peer "$cpub" allowed-ips 10.255.88.2/32 \
        || die "awg set на интерфейсе хоста не прошёл"
    ip addr add 10.255.88.1/30 dev "$ST_IF"; ip link set "$ST_IF" up
    ip netns exec "$ST_C" ip link add awgqtst type amneziawg || die "интерфейс зонда не создался"
    printf '%s' "$cp" | ip netns exec "$ST_C" awg set awgqtst private-key /dev/stdin peer "$spub" allowed-ips 10.255.88.1/32 \
        endpoint "192.0.2.5:$ST_PORT" || die "awg set зонда не прошёл"
    ip netns exec "$ST_C" ip link set awgqtst netns "$ST_Z" || die "перенос зонда в пространство не прошёл"
    ip netns exec "$ST_Z" ip link set lo up
    ip netns exec "$ST_Z" ip addr add 10.255.88.2/32 dev awgqtst
    ip netns exec "$ST_Z" ip link set awgqtst up
    ip netns exec "$ST_Z" ip route add 10.255.88.1/32 dev awgqtst
    ip netns exec "$ST_Z" ping -c1 -W3 10.255.88.1 >/dev/null 2>&1 && ok_ping=1
    if awg show "$ST_IF" latest-handshakes 2>/dev/null | awk '{ if ($2 > 0) found = 1 } END { exit !found }'; then ok_sock=1; fi
    say "RESULT_SOCKET_AT_HOST=$ok_sock"; say "RESULT_TUNNEL_PING=$ok_ping"
    [ "$ok_sock" -eq 1 ] && [ "$ok_ping" -eq 1 ]
}

case "${1:-}" in
    --up) cmd_up "${2:-}" ;;
    --down) cmd_down "${2:-}" ;;
    --wipe) cmd_wipe ;;
    --status) cmd_status ;;
    --selftest) cmd_selftest ;;
    *) say "использование: $0 --up <conf>|--down <имя>|--wipe|--status|--selftest"; exit 2 ;;
esac
