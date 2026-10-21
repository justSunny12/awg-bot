#!/bin/bash
#
# lab.sh — стенд резерва в сетевых пространствах одной Linux-машины (ВМ awg-lab).
#
# Поднимает топологию из topo.sh: сервер A, сервер B, шлюзы основы G1 и G2,
# шлюз резерва GR, «интернет» (маршрутизатор с NAT), «РФ-сайт», клиенты A и B
# — каждый в своём пространстве; «публичные» адреса — 198.51.100.0/24 и
# 203.0.113.0/24, сети шлюзов и клиентов — за NAT маршрутизатора.
#
#   sudo tests/lab/lab.sh up        поднять (идемпотентно: сначала снимает прежнее)
#   sudo tests/lab/lab.sh down      снять всё
#   sudo tests/lab/lab.sh status    кто поднят, адреса, связность
#   sudo tests/lab/lab.sh exec <узел> <команда…>   команда в пространстве узла
#
# В поставку хостов стенд не входит; модуль AmneziaWG на ВМ ставится
# install/awg-kernel-install.sh по манифесту install/awg.lock.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/topo.sh"

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "нужен root"; }
ns() { ip netns exec "$1" "${@:2}"; }

lab_down() {
    local n
    for n in $LAB_NODES; do ip netns del "lab-$n" 2>/dev/null; done
}

lab_up() {
    need_root
    lab_down
    local n
    for n in $LAB_NODES; do ip netns add "lab-$n"; ns "lab-$n" ip link set lo up; done
    ns lab-inet sysctl -qw net.ipv4.ip_forward=1
    # каждый узел — veth до маршрутизатора «интернет»; у маршрутизатора — адрес .1 сети узла
    local spec node addr gw
    for spec in $LAB_LINKS; do
        node="${spec%%:*}"; addr="${spec#*:}"; gw="${addr%.*}.1"
        ip link add "v-$node" type veth peer name "u-$node"
        ip link set "v-$node" netns "lab-$node"
        ip link set "u-$node" netns lab-inet
        ns "lab-$node" ip addr add "$addr/24" dev "v-$node"
        ns "lab-$node" ip link set "v-$node" up
        ns "lab-$node" ip route add default via "$gw"
        ns lab-inet ip addr add "$gw/24" dev "u-$node"
        ns lab-inet ip link set "u-$node" up
    done
    # NAT для сетей за маршрутизатором (шлюзы, клиенты, РФ-сайт); публичные — без NAT
    ns lab-inet nft -f - <<EOF
table ip lab {
    set private4 { type ipv4_addr; flags interval; elements = { $LAB_PRIVATE_NETS } }
    chain post {
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr @private4 ip daddr != @private4 masquerade
    }
    chain lab_forward {                      # «fwd» — ключевое слово nft (fwd to …), именем цепочки быть не может
        type filter hook forward priority filter; policy accept;
    }
}
EOF
    say "стенд поднят: $LAB_NODES"
}

lab_status() {
    local n
    for n in $LAB_NODES; do
        if [ -e "/run/netns/lab-$n" ]; then
            printf '%-6s %s\n' "$n" "$(ns "lab-$n" ip -4 -o addr show 2>/dev/null | awk '$2 != "lo" {print $2": "$4}' | tr '\n' ' ')"
        else
            printf '%-6s —\n' "$n"
        fi
    done
    [ -e /run/netns/lab-A ] || return 0
    say "--- связность: A → B, G1 → A, cA → A, cA → rf"
    ns lab-A ping -c1 -W1 "$LAB_B_PUB" >/dev/null 2>&1 && say "A→B ok" || say "A→B НЕТ"
    ns lab-G1 ping -c1 -W1 "$LAB_A_PUB" >/dev/null 2>&1 && say "G1→A ok" || say "G1→A НЕТ"
    ns lab-cA ping -c1 -W1 "$LAB_A_PUB" >/dev/null 2>&1 && say "cA→A ok" || say "cA→A НЕТ"
    ns lab-cA ping -c1 -W1 "$LAB_RF_ADDR" >/dev/null 2>&1 && say "cA→rf ok" || say "cA→rf НЕТ"
}

case "${1:-}" in
    up) lab_up ;;
    down) need_root; lab_down; say "стенд снят" ;;
    status) lab_status ;;
    exec) need_root; shift; n="$1"; shift; ns "lab-$n" "$@" ;;
    *) say "использование: $0 up | down | status | exec <узел> <команда…>"; exit 2 ;;
esac
