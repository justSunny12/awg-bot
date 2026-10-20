#!/bin/bash
# s01_zone — зона резерва: оболочка, переезд с клоном (сокет у хоста), резолвер зоны, возврат, очистка.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"
Z="$ROOT/install/reserve-zone-setup.sh"
cleanup() { bash "$Z" --wipe >/dev/null 2>&1; ip netns del awg-guest 2>/dev/null; }
trap cleanup EXIT
cleanup
bash "$Z" --shell >/dev/null || { echo "оболочка не поднялась"; exit 1; }
ip netns exec awg-guest nft list table inet awg_guest >/dev/null || { echo "таблицы зоны нет"; exit 1; }
PRIV="$(awg genkey)"; PEERPRIV="$(awg genkey)"; PEERPUB="$(printf '%s' "$PEERPRIV" | awg pubkey)"
CONF="[Interface]
PrivateKey = $PRIV
ListenPort = 45877
Jc = 4
Jmin = 40
Jmax = 70
S1 = 10
S2 = 66
H1 = 5-500
H2 = 600-700
H3 = 800-900
H4 = 1000-1100

[Peer]
PublicKey = $PEERPUB
AllowedIPs = 10.8.1.2/32
"
printf '%s' "$CONF" | GUEST_IF=awg0 GUEST_ADDR=10.8.1.1/24 GUEST_DNS="10.8.1.1" GUEST_UPSTREAMS="1.1.1.1" bash "$Z" --take >/dev/null 2>&1 || { echo "переезд не поднялся"; exit 1; }
ip netns exec awg-guest ip link show awg0 >/dev/null || { echo "клона в зоне нет"; exit 1; }
ip link show awgc$$ >/dev/null 2>&1 && { echo "временный интерфейс остался у хоста"; exit 1; }
[ "$(ip netns exec awg-guest awg show awg0 listen-port)" = "45877" ] || { echo "порт клона не тот"; exit 1; }
mountpoint -q /run/awg-bot-guest || { echo "хранилище гостя не смонтировано"; exit 1; }
grep -q '^listen-address=10.8.1.1' /run/awg-bot-guest/dnsmasq.conf || { echo "резолвер зоны без адреса"; exit 1; }
ip netns exec awg-guest ip route show default | grep -q 169.254.77.1 || { echo "маршрут зоны по умолчанию не через veth"; exit 1; }
systemctl is-active awg-bot-guest-dns.service >/dev/null || { echo "dnsmasq зоны не активен"; exit 1; }
bash "$Z" --serve >/dev/null || { echo "serve не прошёл"; exit 1; }
grep -q '"phase": "serving"' /run/awg-bot/rsv/state.json || { echo "состояние не serving"; exit 1; }
RELAY_TO=203.0.113.5:45877 bash "$Z" --relay >/dev/null || { echo "relay не прошёл"; exit 1; }
ip netns exec awg-guest ip link show awg0 >/dev/null 2>&1 && { echo "клон не снят"; exit 1; }
bash "$Z" --wipe >/dev/null || { echo "wipe не прошёл"; exit 1; }
mountpoint -q /run/awg-bot-guest && { echo "хранилище не снято"; exit 1; }
ip netns exec awg-guest nft list table inet awg_guest >/dev/null || { echo "оболочка после wipe пропала"; exit 1; }
echo ok
