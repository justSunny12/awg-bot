#!/bin/bash
# s04_open_sandbox — процесс открытия в той же песочнице systemd-run, что у бота
# резерва (openproc.rsvopen_sandbox_props): хранилище гостя смонтировано у хоста ДО
# юнита и видно хосту и юнитам гостя после него; каталоги ReadWritePaths с «-» не
# роняют юнит (код 226); из каталога данных резерва процессу виден только пакет.
#
# Проверяет:
#   1. хранилище гостя монтирует «бот» (скрипт зоны --store) в пространстве хоста;
#   2. systemd-run --wait --pipe со свойствами песочницы доводит процесс открытия до done;
#   3. после юнита на хосте есть /run/awg-bot-guest/env (tmpfs, смонтированный внутри
#      юнита со своим пространством монтирования, хосту был бы не виден);
#   4. юнит бота основы на резерве (awg-bot-guest) запущен скриптом зоны;
#   5. --wipe снимает хранилище и env.
#
# Отличие от боевого запуска одно: venv стенда лежит в домашнем каталоге, а
# ProtectHome=yes его скрывает; на хостах venv — в /opt/awg-bot. Здесь ProtectHome
# переопределён на tmpfs с привязкой venv только для чтения.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"
Z="$ROOT/install/reserve-zone-setup.sh"
USER_="${SUDO_USER:-$(id -un)}"; HOME_="$(getent passwd "$USER_" | cut -d: -f6)"
PY="${LAB_PY:-$HOME_/venv/bin/python}"
RUN=/run/awg-bot-guest
DATA=/var/tmp/awglab-open-data
TOKEN="880000:LAB-DUMMY-token"
cleanup() {
    systemctl stop awg-bot-guest.service awg-bot-guest-dns.service 2>/dev/null
    bash "$Z" --teardown >/dev/null 2>&1; bash "$Z" --wipe >/dev/null 2>&1
    ip netns del awg-guest 2>/dev/null
    rm -rf "$DATA"
}
trap cleanup EXIT
cleanup
mkdir -p "$DATA/reserve"

# ── пакет основы: настоящие ключи AWG, база с одним профилем, доли M ─────────────────────────────
PRIV="$(awg genkey)"; PEERPUB="$(awg genkey | awg pubkey)"
OUT="$(AWG_BOT_DATA_DIR="$DATA" LAB_PRIV="$PRIV" LAB_PEER="$PEERPUB" LAB_TOKEN="$TOKEN" "$PY" - <<'PYEOF'
import json, os, tempfile
from awgbot.core import config
from awgbot.infra.db import Database
from awgbot.util import rsvkeys
from awgbot.util.rsvpkg import bot as pkgbot, seal, vpn as pkgvpn
tmp = tempfile.mkdtemp()
db = Database(os.path.join(tmp, "src.db")); db.init_schema()
cid = db.create_client("Аня", 3, "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00", "LAB-A")
db.activate_client(cid, 880002); path = db.path; db.close()
live = f"""[Interface]
PrivateKey = {os.environ['LAB_PRIV']}
Address = 10.8.1.1/24
ListenPort = 45877
MTU = 1376
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
PublicKey = {os.environ['LAB_PEER']}
AllowedIPs = 10.8.1.2/32
"""
gen = rsvkeys.Generation.new(1); k = seal.new_k()
vpn = pkgvpn.build([("awg0", live)], admin=["10.8.1.2"], dns=["10.8.1.1"], upstreams=["198.51.100.53"], tz="UTC")
botpart = {"db": pkgbot.replica(path).hex(), "conf": {}, "app_guest": {"network": {"server_host": "203.0.113.7", "server_port": 45877}},
           "env": {"BOT_TOKEN": os.environ["LAB_TOKEN"], "ADMIN_ID": 880001}}
blob = seal.seal({"vpn": vpn, "bot": botpart}, k=k, m=gen.m, pkg_gen=1, m_gen=1)
open(os.path.join(config.DATA_DIR, "reserve", "guest.pkg"), "wb").write(blob)
x1, y1 = gen.reserve_point(); x2, y2 = gen.gateway_point(1)
print(json.dumps({"shares": [[x1, y1.hex()], [x2, y2.hex()]]}))
PYEOF
)" || { echo "пакет не собрался: $OUT"; exit 1; }
SHARES="$(printf '%s' "$OUT" | "$PY" -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["shares"]))')"

# ── 1. оболочка и хранилище — у хоста, до юнита ──────────────────────────────────────────────────
bash "$Z" --shell >/dev/null || { echo "оболочка не поднялась"; exit 1; }
bash "$Z" --store >/dev/null || { echo "хранилище гостя не смонтировалось"; exit 1; }
mountpoint -q "$RUN" || { echo "хранилище гостя не смонтировано у хоста"; exit 1; }
mkdir -p /var/lib/awg-bot-guest /run/awg-bot/rsv

# ── 2. процесс открытия в песочнице бота ─────────────────────────────────────────────────────────
mapfile -t PROPS < <(cd "$ROOT" && AWG_BOT_DATA_DIR="$DATA" "$PY" -c '
import sys
from awgbot.domain.services.reserve import openproc
print("\n".join(openproc.rsvopen_sandbox_props(sys.argv[1])))' "$ROOT")
[ "${#PROPS[@]}" -gt 10 ] || { echo "свойств песочницы нет: ${PROPS[*]}"; exit 1; }
printf '%s\n' "${PROPS[@]}" | grep -qx 'ProtectSystem=strict' || { echo "песочница не strict: ${PROPS[*]}"; exit 1; }
LAB_PROPS=(-p ProtectHome=tmpfs -p "BindReadOnlyPaths=$HOME_/venv")       # venv стенда — в домашнем каталоге
res="$(printf '{"shares": %s, "set_record": false, "webhook_port": 443, "self_host": "198.51.100.7"}' "$SHARES" \
    | systemd-run --wait --pipe --collect --quiet --unit=awg-bot-rsvopen-lab "${PROPS[@]}" "${LAB_PROPS[@]}" \
        --setenv=AWG_BOT_DATA_DIR="$DATA" --setenv=AWG_BOT_ENV=/dev/null "$PY" -m tools.rsvopen 2>&1)"
rc=$?
[ "$rc" -eq 0 ] || { echo "юнит открытия завершился с кодом $rc (226 — путь ReadWritePaths без «-»): $res"; exit 1; }
echo "$res" | tail -n1 | grep -qx done || { echo "последняя строка хода не done: $res"; exit 1; }

# ── 3. хранилище с env видно хосту ───────────────────────────────────────────────────────────────
[ -s "$RUN/env" ] || { echo "после юнита на хосте нет $RUN/env — хранилище смонтировано внутри юнита"; exit 1; }
[ "$(stat -c %a "$RUN/env")" = "600" ] || { echo "права env гостя не 0600"; exit 1; }
grep -q "$TOKEN" "$RUN/env" || { echo "env гостя без токена основы"; exit 1; }

# ── 4. юнит бота основы на резерве запущен ───────────────────────────────────────────────────────
state="$(systemctl is-active awg-bot-guest.service 2>/dev/null)"
case "$state" in active|activating) ;; *) echo "юнит гостя не запущен: $state"; exit 1 ;; esac
systemctl stop awg-bot-guest.service 2>/dev/null

# ── 5. стирание ──────────────────────────────────────────────────────────────────────────────────
bash "$Z" --teardown >/dev/null; bash "$Z" --wipe >/dev/null || { echo "--wipe отказал"; exit 1; }
[ ! -e "$RUN/env" ] || { echo "после --wipe env гостя на месте"; exit 1; }
mountpoint -q "$RUN" && { echo "после --wipe хранилище гостя смонтировано"; exit 1; }
echo ok
