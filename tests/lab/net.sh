#!/bin/bash
#
# net.sh — рычаги аварий на стенде (lab.sh up). Всё — правилами в
# пространстве маршрутизатора «интернет», чтобы узлы о рычаге не знали.
#
#   sudo tests/lab/net.sh kill <узел>          узел «умер»: весь трафик узла отброшен
#   sudo tests/lab/net.sh cut <узел1> <узел2>  разрезать путь между двумя узлами (в обе стороны)
#   sudo tests/lab/net.sh block-rf <узел>      «блокировка из РФ»: частные сети не видят узел (ТСПУ)
#   sudo tests/lab/net.sh heal                 снять все рычаги
#   sudo tests/lab/net.sh show                 действующие рычаги
#
# Потеря одной keepalive и перезагрузка шлюза — рычаги следующих этапов (нужен
# работающий линк).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/topo.sh"
say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "нужен root"
ns() { ip netns exec lab-inet "$@"; }

addr_of() {   # адрес узла по topo.sh
    local spec
    for spec in $LAB_LINKS; do [ "${spec%%:*}" = "$1" ] && { printf '%s' "${spec#*:}"; return 0; }; done
    die "узел $1 не известен"
}

ensure_table() {
    ns nft list table ip labfault >/dev/null 2>&1 && return 0
    ns nft -f - <<EOF
table ip labfault {
    chain faults { type filter hook forward priority filter - 10; policy accept; }
}
EOF
}

case "${1:-}" in
    kill)
        a="$(addr_of "$2")"; ensure_table
        ns nft add rule ip labfault faults ip saddr "$a" drop comment "\"kill $2\""
        ns nft add rule ip labfault faults ip daddr "$a" drop comment "\"kill $2\""
        say "узел $2 мёртв для всех" ;;
    cut)
        a="$(addr_of "$2")"; b="$(addr_of "$3")"; ensure_table
        ns nft add rule ip labfault faults ip saddr "$a" ip daddr "$b" drop comment "\"cut $2 $3\""
        ns nft add rule ip labfault faults ip saddr "$b" ip daddr "$a" drop comment "\"cut $2 $3\""
        say "путь $2 ↔ $3 разрезан" ;;
    block-rf)
        a="$(addr_of "$2")"; ensure_table
        ns nft add rule ip labfault faults ip saddr "{ $LAB_PRIVATE_NETS }" ip daddr "$a" drop comment "\"block-rf $2\""
        say "узел $2 закрыт из частных сетей" ;;
    heal)
        ns nft delete table ip labfault 2>/dev/null; say "рычаги сняты" ;;
    show)
        ns nft list table ip labfault 2>/dev/null | grep comment | sed 's/^ *//' || say "рычагов нет" ;;
    *) say "использование: $0 kill|cut|block-rf|heal|show …"; exit 2 ;;
esac
