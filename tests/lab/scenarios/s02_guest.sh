#!/bin/bash
# s02_guest — бот основы на резерве: открытие по пакету, ответ на /start клиента, песочница
# (env и база резерва не видны), шифрованный снимок и подъём зоны из него после перезагрузки.
#
# ЧЕРНОВИК: набросан по образцу s01_zone, на стенде не прогонялся. Что нужно от стенда: модуль
# AmneziaWG, systemd, venv с зависимостями (~/venv пользователя sudo), openssl. Bot API — макет
# tests/lab/botapi.py на veth-адресе хоста зоны (169.254.77.1:8081); бот гостя запускается не
# юнитом, а tests/lab/guest_main.py в пространстве зоны — тем же кодом run_guest, но с макетом.
#
# Проверяет:
#   1. процесс открытия (rsvopen) доходит до done по двум долям, клон поднят в зоне;
#      конфиг клона от rsvopen принимается `awg setconf` (в нём есть Address и MTU — если скрипт
#      зоны их не снимает, шаг падает здесь);
#   2. бот гостя отвечает на /start клиента: в журнале макета есть sendMessage в чат клиента;
#   3. песочница юнита: с теми же свойствами, что у юнита гостя, /etc/awg-bot/env и
#      /var/lib/awg-bot/bot.db резерва не читаются (ошибка доступа), а хранилище гостя — читается;
#   4. через ≤ 60 с на диске резерва лежит rep.bin — шифротекст (ни имени профиля, ни «SQLite»);
#   5. «перезагрузка резерва»: бот остановлен, хранилище в памяти снято, rep.bin остался;
#      повторное открытие по K поднимает зону из rep.bin — профиль, заведённый на резерве, на месте.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"
Z="$ROOT/install/reserve-zone-setup.sh"
USER_="${SUDO_USER:-$(id -un)}"; HOME_="$(getent passwd "$USER_" | cut -d: -f6)"
PY="${LAB_PY:-$HOME_/venv/bin/python}"
RUN=/run/awg-bot-guest; VAR=/var/lib/awg-bot-guest
DATA=/var/tmp/awglab-guest-data; JOURNAL=/var/tmp/awglab-botapi.jsonl
TOKEN="880000:LAB-DUMMY-token"; NAME="Вера-на-резерве"
BOTAPI_PID=""; BOT_PID=""
cleanup() {
    [ -n "$BOT_PID" ] && kill "$BOT_PID" 2>/dev/null
    [ -n "$BOTAPI_PID" ] && kill "$BOTAPI_PID" 2>/dev/null
    bash "$Z" --wipe >/dev/null 2>&1; ip netns del awg-guest 2>/dev/null
    rm -rf "$DATA" "$JOURNAL" /etc/awg-bot/env.lab-sentinel
}
trap cleanup EXIT
cleanup
mkdir -p "$DATA/reserve"
export AWG_BOT_DATA_DIR="$DATA"

# ── пакет основы: настоящие ключи AWG, база с одним профилем, доли M ─────────────────────────────
PRIV="$(awg genkey)"; PEERPUB="$(awg genkey | awg pubkey)"
OUT="$(LAB_PRIV="$PRIV" LAB_PEER="$PEERPUB" LAB_TOKEN="$TOKEN" "$PY" - <<'PYEOF'
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
os.makedirs(os.path.join(config.DATA_DIR, "reserve"), exist_ok=True)
open(os.path.join(config.DATA_DIR, "reserve", "guest.pkg"), "wb").write(blob)
x1, y1 = gen.reserve_point(); x2, y2 = gen.gateway_point(1)
print(json.dumps({"shares": [[x1, y1.hex()], [x2, y2.hex()]], "k": k.hex()}))
PYEOF
)" || { echo "пакет не собрался: $OUT"; exit 1; }
SHARES="$(printf '%s' "$OUT" | "$PY" -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["shares"]))')"
K="$(printf '%s' "$OUT" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["k"])')"

# ── 1. открытие ──────────────────────────────────────────────────────────────────────────────────
bash "$Z" --shell >/dev/null || { echo "оболочка не поднялась"; exit 1; }
cd "$ROOT" || exit 1
res="$(printf '{"shares": %s, "set_record": false, "webhook_port": 443, "self_host": "198.51.100.7"}' "$SHARES" \
    | "$PY" -m tools.rsvopen)" || { echo "открытие не дошло до done: $res"; exit 1; }
echo "$res" | tail -n1 | grep -qx done || { echo "последняя строка хода не done: $res"; exit 1; }
for secret in "$K" "$TOKEN" "$PRIV"; do
    echo "$res" | grep -q "$secret" && { echo "секрет в выводе процесса открытия"; exit 1; }
done
ip netns exec awg-guest ip link show awg0 >/dev/null || { echo "клона в зоне нет"; exit 1; }
systemctl stop awg-bot-guest.service 2>/dev/null     # запускаем бота сами — с макетом Bot API

# ── 2. /start клиента ────────────────────────────────────────────────────────────────────────────
"$PY" -m tests.lab.botapi --listen 169.254.77.1:8081 --journal "$JOURNAL" >/dev/null 2>&1 & BOTAPI_PID=$!
sleep 1
set -a; . "$RUN/env"; set +a
ip netns exec awg-guest env AWG_BOT_GUEST=1 AWG_BOT_CONF_DIR="$RUN/conf" AWG_BOT_DATA_DIR="$RUN/data" \
    AWG_BOT_ENV="$RUN/env" LAB_BOTAPI=http://169.254.77.1:8081 LAB_MAKE_CLIENT="$NAME" PYTHONPATH="$ROOT" \
    "$PY" -m tests.lab.guest_main >"$DATA/bot.log" 2>&1 & BOT_PID=$!
sleep 6
kill -0 "$BOT_PID" 2>/dev/null || { echo "бот гостя не запустился: $(tail -n5 "$DATA/bot.log")"; exit 1; }
curl -s -X POST "http://169.254.77.1:8081/inject/$TOKEN" -H 'Content-Type: application/json' \
    -d '{"message": {"message_id": 1, "date": 1, "chat": {"id": 880002, "type": "private"}, "from": {"id": 880002, "is_bot": false, "first_name": "Аня"}, "text": "/start"}}' >/dev/null
ok=""; for _ in $(seq 1 20); do
    if grep -q '"method": "sendMessage"' "$JOURNAL" 2>/dev/null; then ok=1; break; fi; sleep 1
done
[ -n "$ok" ] || { echo "бот гостя не ответил на /start: $(tail -n5 "$DATA/bot.log")"; exit 1; }

# ── 3. песочница ─────────────────────────────────────────────────────────────────────────────────
mkdir -p /etc/awg-bot && printf 'BOT_TOKEN=SENTINEL-reserve-token\n' > /etc/awg-bot/env.lab-sentinel
props=()
for p in TemporaryFileSystem InaccessiblePaths ReadWritePaths ProtectSystem; do
    v="$(systemctl show awg-bot-guest.service -p "$p" --value 2>/dev/null)"
    [ -n "$v" ] && props+=(-p "$p=$v")
done
[ "${#props[@]}" -gt 0 ] || { echo "у юнита гостя нет свойств песочницы"; exit 1; }
sand() { systemd-run --wait --pipe --collect -q "${props[@]}" -p NetworkNamespacePath=/run/netns/awg-guest "$@" 2>&1; }
out="$(sand cat /etc/awg-bot/env.lab-sentinel)"
echo "$out" | grep -q SENTINEL && { echo "песочница пропускает /etc/awg-bot резерва"; exit 1; }
[ -n "$(sand ls /var/lib/awg-bot/bot.db 2>/dev/null | grep -v 'No such file\|cannot access\|Permission denied')" ] \
    && { echo "песочница пропускает /var/lib/awg-bot резерва"; exit 1; }
sand test -r "$RUN/data/bot.db" || { echo "хранилище гостя в песочнице не читается"; exit 1; }

# ── 4. rep.bin — шифротекст ──────────────────────────────────────────────────────────────────────
for _ in $(seq 1 60); do [ -s "$VAR/rep.bin" ] && break; sleep 1; done
[ -s "$VAR/rep.bin" ] || { echo "через 60 с rep.bin нет"; exit 1; }
grep -q "SQLite format" "$VAR/rep.bin" && { echo "rep.bin — открытая база"; exit 1; }
grep -q "$NAME" "$VAR/rep.bin" && { echo "rep.bin несёт имя профиля открытым текстом"; exit 1; }
[ "$(stat -c %a "$VAR/rep.bin")" = "600" ] || { echo "права rep.bin не 0600"; exit 1; }

# ── 5. перезагрузка резерва во время обслуживания ───────────────────────────────────────────────
kill "$BOT_PID" 2>/dev/null; BOT_PID=""
bash "$Z" --teardown >/dev/null; umount -l "$RUN" 2>/dev/null; rm -rf "$RUN"
[ -s "$VAR/rep.bin" ] || { echo "rep.bin пропал при снятии зоны"; exit 1; }
res="$(printf '{"k": "%s", "set_record": false, "webhook_port": 443, "self_host": "198.51.100.7"}' "$K" | "$PY" -m tools.rsvopen)" \
    || { echo "повторное открытие по K не прошло: $res"; exit 1; }
[ -n "$("$PY" - <<PYEOF
import sqlite3
con = sqlite3.connect("$RUN/data/bot.db")
print(con.execute("SELECT 1 FROM clients WHERE name = ?", ("$NAME",)).fetchone() or "")
PYEOF
)" ] || { echo "после перезагрузки зона поднялась из пакета, а не из rep.bin: профиль $NAME потерян"; exit 1; }
echo ok
