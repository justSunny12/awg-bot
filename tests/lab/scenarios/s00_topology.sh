#!/bin/bash
# s00_topology — топология стенда живая: публичные адреса видят друг друга,
# частные сети выходят через NAT, рычаг kill и heal работают.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ns() { ip netns exec "lab-$1" "${@:2}"; }
ns A ping -c1 -W1 198.51.100.7 >/dev/null || { echo "A не видит B"; exit 1; }
ns G1 ping -c1 -W1 203.0.113.5 >/dev/null || { echo "G1 не видит A через NAT"; exit 1; }
ns cB ping -c1 -W1 198.51.100.7 >/dev/null || { echo "cB не видит B"; exit 1; }
bash "$HERE/net.sh" kill A >/dev/null
if ns G1 ping -c1 -W1 203.0.113.5 >/dev/null 2>&1; then echo "после kill A узел A всё ещё отвечает"; exit 1; fi
bash "$HERE/net.sh" heal >/dev/null
ns G1 ping -c1 -W1 203.0.113.5 >/dev/null || { echo "после heal A не ожил"; exit 1; }
echo "ok"
