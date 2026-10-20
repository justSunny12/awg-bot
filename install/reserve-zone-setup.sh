#!/bin/bash
#
# reserve-zone-setup.sh — гостевая зона резерва (netns awg-guest) на ВПС.
#
# Зона — сетевое пространство, в котором живут стороны постоянных линков
# шлюзов основы (всегда, пока есть пара) и, только на время переезда, клон
# клиентского интерфейса основы, veth к хосту, dnsmasq зоны и бот основы в
# гостевом режиме. UDP-сокеты интерфейсов AmneziaWG остаются у хоста:
# интерфейс создаётся в пространстве хоста, переносится в зону и там
# переименовывается; порт слушает хост, расшифрованный пакет выходит в зоне.
# На этом стоит вся схема, поэтому режим --selftest проверяет это на модуле
# ядра, который стоит на хосте, перед любым другим шагом.
#
# Режимы (этап 1 — только два первых; остальные появляются по этапам):
#   --plan       без root: что умеет хост — хранилище в памяти (tmpfs noswap с
#                ядра 6.4, иначе ramfs), sch_cake, пространства имён, свободный
#                TCP-порт вебхука из 443/80/88/8443; вид строками key=value
#   --selftest   root: «сокет у хоста» и диапазон persistent-keepalive на
#                установленном модуле; код 0 — обе гипотезы подтверждены,
#                1 — нет (концепт правится до этапа 3), строки RESULT_*
#   --shell | --take | --serve | --restart-clone | --relay | --teardown | --wipe | --status
#                следующие этапы; сейчас — код 2 «не реализовано»
#
# Секретное (ключи клона, PSK) — только через stdin и в хранилище в памяти;
# в аргументах и в окружении секретов нет никогда.
set -u

ZONE="${RSV_ZONE:-awg-guest}"
WEBHOOK_PORTS="443 80 88 8443"

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "нужен root"; }

kernel_ge() {   # kernel_ge MAJOR MINOR — ядро не старше
    local k; k="$(uname -r | cut -d- -f1)"
    local maj min; maj="${k%%.*}"; min="${k#*.}"; min="${min%%.*}"
    [ "$maj" -gt "$1" ] 2>/dev/null || { [ "$maj" -eq "$1" ] && [ "$min" -ge "$2" ]; } 2>/dev/null
}

port_free() {   # port_free PORT — TCP-порт не слушает никто
    if command -v ss >/dev/null 2>&1; then
        ! ss -Hltn "sport = :$1" 2>/dev/null | grep -q .
    else
        return 0
    fi
}

cmd_plan() {
    if kernel_ge 6 4; then say "store=tmpfs-noswap"; else say "store=ramfs"; fi
    if modinfo sch_cake >/dev/null 2>&1 || grep -qw sch_cake /proc/modules 2>/dev/null; then say "cake=yes"; else say "cake=no"; fi
    if [ -d /proc/self/ns ] && [ -e /proc/self/ns/net ]; then say "netns=yes"; else say "netns=no"; fi
    if modinfo amneziawg >/dev/null 2>&1 || grep -qw amneziawg /proc/modules 2>/dev/null; then say "awg=yes"; else say "awg=no"; fi
    local p chosen=""
    for p in $WEBHOOK_PORTS; do
        if port_free "$p"; then chosen="$p"; break; fi
    done
    say "webhook_port=${chosen:-none}"
    if [ -e "/run/netns/$ZONE" ]; then say "zone=present"; else say "zone=absent"; fi
}

# ── самопроверка гипотез на живом модуле ─────────────────────────────────────
ST_ZONE="awgrsvtst-z"; ST_CLI="awgrsvtst-c"; ST_VH="awgrsvtv0"; ST_VC="awgrsvtv1"; ST_IF="awgrsvtst"
ST_PORT="${RSV_SELFTEST_PORT:-45999}"

st_cleanup() {
    ip netns del "$ST_ZONE" 2>/dev/null
    ip netns del "$ST_CLI" 2>/dev/null
    ip link del "$ST_VH" 2>/dev/null
    ip link del "$ST_IF" 2>/dev/null
}

cmd_selftest() {
    need_root
    command -v awg >/dev/null 2>&1 || die "нет amneziawg-tools (awg)"
    modprobe amneziawg 2>/dev/null || true
    trap st_cleanup EXIT
    st_cleanup
    local sp spub cp cpub ok_ka=0 ok_sock=0 ok_ping=0
    ip netns add "$ST_ZONE" || die "пространство не создалось"
    ip netns add "$ST_CLI" || die "пространство не создалось"
    ip link add "$ST_VH" type veth peer name "$ST_VC" || die "veth не создался"
    ip link set "$ST_VC" netns "$ST_CLI"
    ip addr add 192.0.2.1/30 dev "$ST_VH"; ip link set "$ST_VH" up
    ip netns exec "$ST_CLI" ip addr add 192.0.2.2/30 dev "$ST_VC"
    ip netns exec "$ST_CLI" ip link set "$ST_VC" up
    ip netns exec "$ST_CLI" ip link set lo up
    sp="$(awg genkey)"; spub="$(printf '%s' "$sp" | awg pubkey)"
    cp="$(awg genkey)"; cpub="$(printf '%s' "$cp" | awg pubkey)"
    # интерфейс — в пространстве хоста: сокет создаётся здесь
    ip link add "$ST_IF" type amneziawg || die "интерфейс amneziawg не создался"
    printf '%s' "$sp" | awg set "$ST_IF" private-key /dev/stdin listen-port "$ST_PORT" peer "$cpub" allowed-ips 10.255.77.2/32 \
        || die "awg set на интерфейсе хоста не прошёл"
    # перенос в зону и переименование — как у клона
    ip link set "$ST_IF" netns "$ST_ZONE" || die "перенос интерфейса в пространство не прошёл"
    ip netns exec "$ST_ZONE" ip link set "$ST_IF" name awg0
    ip netns exec "$ST_ZONE" ip addr add 10.255.77.1/30 dev awg0
    ip netns exec "$ST_ZONE" ip link set awg0 up
    # клиент — в своём пространстве, Endpoint — адрес ХОСТА (не зоны)
    ip netns exec "$ST_CLI" ip link add awgc type amneziawg
    if printf '%s' "$cp" | ip netns exec "$ST_CLI" awg set awgc private-key /dev/stdin peer "$spub" allowed-ips 10.255.77.0/30 \
            endpoint "192.0.2.1:$ST_PORT" persistent-keepalive 25-35 2>/dev/null; then
        ok_ka=1
    else
        # диапазон не принят — ставим число, чтобы проверить сокет отдельно
        printf '%s' "$cp" | ip netns exec "$ST_CLI" awg set awgc private-key /dev/stdin peer "$spub" allowed-ips 10.255.77.0/30 \
            endpoint "192.0.2.1:$ST_PORT" persistent-keepalive 25
    fi
    ip netns exec "$ST_CLI" ip addr add 10.255.77.2/30 dev awgc
    ip netns exec "$ST_CLI" ip link set awgc up
    ip netns exec "$ST_CLI" ping -c1 -W3 10.255.77.1 >/dev/null 2>&1 && ok_ping=1
    if ip netns exec "$ST_ZONE" awg show awg0 latest-handshakes 2>/dev/null | awk '{ if ($2 > 0) found = 1 } END { exit !found }'; then
        ok_sock=1
    fi
    say "RESULT_SOCKET_AT_HOST=$ok_sock"
    say "RESULT_KEEPALIVE_RANGE=$ok_ka"
    say "RESULT_TUNNEL_PING=$ok_ping"
    [ "$ok_sock" -eq 1 ] && [ "$ok_ka" -eq 1 ]
}

case "${1:-}" in
    --plan) cmd_plan ;;
    --selftest) cmd_selftest ;;
    --shell|--take|--serve|--restart-clone|--relay|--teardown|--wipe|--status) die "режим $1 — следующий этап"; ;;
    *) say "использование: $0 --plan | --selftest"; exit 2 ;;
esac
