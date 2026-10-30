#!/bin/bash
# s05_quorum_probes — зонды кворума резерва: пространство awg-quo на голосующих, пиры-зонды и второй
# адрес на awg0 основы, ответчик, пробы, файервол хоста основы.
#
# Что нужно от стенда: топология topo.sh (A, B, G1, G2, inet, cA), модуль AmneziaWG, venv (~/venv).
# Ответчик — tests/lab/quorum_node.py (правило лидера util/quorum на UDP-сокете в пространстве A без
# Telegram и базы); зонды — install/quorum-probe-setup.sh в пространствах B, G1, G2 (QUO_NS=awg-quo-<узел>:
# три голосующих на одном хосте).
#
# Проверяет:
#   1. --selftest скрипта зондов на модуле стенда: RESULT_SOCKET_AT_HOST=1 (сокет интерфейса, созданного у
#      хоста и перенесённого в пространство, остаётся у хоста) и RESULT_TUNNEL_PING=1;
#   2. awg0 «Alpha» на A (запись — infra/awg add_peer и ensure_second_address, как у основы): второй адрес
#      169.254.88.1/26 — отдельной строкой Address сразу после основной, без комментария; пиры-зонды B (.4),
#      G1 (.5), G2 (.6) — ровно один /32 каждый; правило рельса 4.1.x (probenet) узнаёт зонды и не узнаёт
#      пользователя; адрес на живом интерфейсе;
#   3. зонды в awg-quo на B, G1, G2 (--up): проба {"q":1,"l":"a","g":1,"n":…,"e":5,"w":"see"} доходит до
#      ответчика 169.254.88.1:8789, ответ — до зонда; после проб с трёх узлов y = 3, v = 3; повторный --up
#      тем же конфигом — «up: same», сессия не рвётся; --status — рукопожатие есть;
#   4. из awg-quo наружу (203.0.113.5), в подсеть пользователей (10.8.1.1) и на другой порт ответчика —
#      отказ: маршрута по умолчанию нет, таблица awg_quo пускает только udp на ответчик;
#   5. пользователь cA (туннель A, 10.8.1.2): проба на 169.254.88.1:8789 — ответа нет и в счёт не идёт
#      (адрес не зонда); с подменой источника на 169.254.88.5 — AWG основы отбрасывает её до ответчика
#      (AllowedIPs пира — его /32); таблица awg_bot_guard (рендер бота, форма с фильтром) — проба
#      пользователя до ответчика не доходит вовсе, пробы зондов идут; проба с подменённым узлом (n=a2 с
#      адреса .5) — не в счёт;
#   6. поиск аплинка агентом шлюза (gwguard.uplink_interface — тот же, что в 4.1.x) на G1 при поднятом
#      awg-quo находит аплинк: awgqah с тем же Endpoint, что у линка, ему не виден; контроль — тот же
#      интерфейс у хоста ломает поиск;
#   7. наблюдение по выключению каждого узла: зонд узла --down — y падает на 1 через окно W (40 с), не
#      раньше, другие узлы в счёте; узел вернулся (--up) — снова в счёте; --wipe снимает пространство и
#      каталог конфигов.
#
# Не покрыто здесь: клон в зоне второго сервера отвечает на .2 и счёт в карточке настоящего бота — это
# сценарий пары ботов (s06_quorum_move: ответчик клона — бот основы на резерве).
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"
USER_="${SUDO_USER:-$(id -un)}"; HOME_="$(getent passwd "$USER_" | cut -d: -f6)"
PY="${LAB_PY:-$HOME_/venv/bin/python}"
SCRIPT="$ROOT/install/quorum-probe-setup.sh"
Q=/var/lib/awglab-quo
A_PUB_IP=203.0.113.5; A_PORT=45871; RESP=169.254.88.1
declare -A ADDR=([B]=169.254.88.4 [G1]=169.254.88.5 [G2]=169.254.88.6)
declare -A NODE=([B]=b [G1]=a1 [G2]=a2)
PIDS=""

fail() { printf 'FAIL: %s\n' "$*"; exit 1; }
qsh() {       # qsh <узел> <аргументы скрипта> — скрипт зондов в пространстве узла, своё awg-quo
    local n="$1"; shift
    # nsenter --net, не ip netns exec: у того своё пространство монтирования, и `ip netns add` скрипта
    # смонтировал бы файл awg-quo-<узел> только в нём (снаружи — пустой файл, setns — EINVAL)
    nsenter --net="/run/netns/lab-$n" env QUO_NS="awg-quo-$n" QUO_CONF_DIR="$Q/$n/quo" sh "$SCRIPT" "$@"
}
qpy() {       # qpy <пространство> <аргументы quorum_node>
    local ns="$1"; shift
    (cd "$ROOT" && ip netns exec "$ns" env PYTHONPATH="$ROOT" "$PY" -m tests.lab.quorum_node "$@")
}
rq() {        # rq <выражение над s> — из статуса ответчика
    "$PY" -c 'import json,sys
try: s=json.load(open(sys.argv[1]))
except Exception: s={}
try: print(json.dumps(eval(sys.argv[2], {"s": s})))
except Exception: print("null")' "$Q/resp.json" "$1"
}
wait_rq() {   # wait_rq <выражение> <срок> — ждать истины; на stdout — сколько секунд ждали
    local i
    for i in $(seq 0 "$2"); do [ "$(rq "bool($1)")" = "true" ] && { echo "$i"; return 0; }; sleep 1; done
    echo "$2"; return 1
}
cleanup() {
    local p n
    for p in $PIDS; do kill "$p" 2>/dev/null; done
    for n in B G1 G2; do qsh "$n" --wipe >/dev/null 2>&1; ip netns del "awg-quo-$n" 2>/dev/null; done
    ip -n lab-A link del awg0 2>/dev/null; ip -n lab-cA link del awgc 2>/dev/null
    for n in awglink awg1 awgqtst; do ip -n lab-G1 link del "$n" 2>/dev/null; done
    ip netns exec lab-A nft delete table inet awg_bot_guard 2>/dev/null
    rm -rf "$Q"
}
trap cleanup EXIT
cleanup
mkdir -p "$Q/A" "$Q/B" "$Q/G1" "$Q/G2"

# ── 1. самопроверка скрипта зондов на модуле стенда ───────────────────────────────────────────────
out="$(sh "$SCRIPT" --selftest 2>&1)" || fail "selftest скрипта зондов не прошёл: $out"
printf '%s\n' "$out" | grep -q 'RESULT_SOCKET_AT_HOST=1' || fail "сокет зонда не остался у хоста: $out"
printf '%s\n' "$out" | grep -q 'RESULT_TUNNEL_PING=1' || fail "туннель зонда не ожил: $out"

# ── 2. awg0 «Alpha»: подсеть пользователей, пиры-зонды, второй адрес ─────────────────────────────
OBF='Jc = 4
Jmin = 40
Jmax = 70
S1 = 10
S2 = 66
H1 = 5-500
H2 = 600-700
H3 = 800-900
H4 = 1000-1100'
A_PRIV="$(awg genkey)"; A_PUB="$(printf '%s' "$A_PRIV" | awg pubkey)"
cat > "$Q/A/awg0.conf" <<EOF
[Interface]
PrivateKey = $A_PRIV
Address = 10.8.1.1/24
ListenPort = $A_PORT
$OBF
EOF
chmod 600 "$Q/A/awg0.conf"
ip -n lab-A link add awg0 type amneziawg || fail "awg0 в lab-A не создан"
ip netns exec lab-A awg setconf awg0 <(grep -v '^Address' "$Q/A/awg0.conf") || fail "awg setconf awg0"
ip -n lab-A addr add 10.8.1.1/24 dev awg0; ip -n lab-A link set awg0 up
declare -A PRIV PUB PSK
for n in B G1 G2 cA; do PRIV[$n]="$(awg genkey)"; PUB[$n]="$(printf '%s' "${PRIV[$n]}" | awg pubkey)"; PSK[$n]="$(awg genpsk)"; done
out="$(qpy lab-A awg0 --conf "$Q/A/awg0.conf" --resp "$RESP/26" --peer "${PUB[cA]},${PSK[cA]},10.8.1.2" \
    --peer "${PUB[B]},${PSK[B]},${ADDR[B]}" --peer "${PUB[G1]},${PSK[G1]},${ADDR[G1]}" --peer "${PUB[G2]},${PSK[G2]},${ADDR[G2]}" 2>&1)" \
    || fail "пиры-зонды на awg0 не записаны: $out"
addr_lines="$(grep -n '^Address' "$Q/A/awg0.conf")"
[ "$(printf '%s\n' "$addr_lines" | sed -n 1p | cut -d: -f2-)" = "Address = 10.8.1.1/24" ] || fail "первая строка Address не основная: $addr_lines"
[ "$(printf '%s\n' "$addr_lines" | sed -n 2p | cut -d: -f2-)" = "Address = $RESP/26" ] || fail "второй адрес не отдельной строкой без комментария: $addr_lines"
l1="$(printf '%s\n' "$addr_lines" | sed -n 1p | cut -d: -f1)"; l2="$(printf '%s\n' "$addr_lines" | sed -n 2p | cut -d: -f1)"
[ "$l2" -eq $((l1 + 1)) ] || fail "второй адрес не сразу после основного: $addr_lines"
for n in B G1 G2; do
    got="$(awk -v k="PublicKey = ${PUB[$n]}" '$0 == k {f=1; next} f && /^AllowedIPs/ {print; exit}' "$Q/A/awg0.conf")"
    [ "$got" = "AllowedIPs = ${ADDR[$n]}/32" ] || fail "пир-зонд $n: «$got», ожидался ровно ${ADDR[$n]}/32"
done
ip -n lab-A -4 addr show awg0 | grep -q "inet $RESP/26" || fail "второго адреса нет на живом awg0: $(ip -n lab-A -4 addr show awg0)"
out="$(cd "$ROOT" && "$PY" - "$Q/A/awg0.conf" "${PUB[cA]}" <<'PYEOF'
import sys
from awgbot.infra.awg.parsers import _split_conf
from awgbot.util import probenet
conf = open(sys.argv[1]).read()
header, peers = _split_conf(conf)
users = probenet.user_nets(probenet.iface_addrs(header))
if users != ["10.8.1.1/24"]:
    print("подсеть пользователей по конфигу:", users); sys.exit(1)
if probenet.primary_addr(probenet.iface_addrs(header)) != "10.8.1.1/24":
    print("основной адрес по конфигу не 10.8.1.1/24"); sys.exit(1)
for p in peers:
    allowed = [ln.split("=", 1)[1] for ln in p["lines"] if ln.strip().startswith("AllowedIPs")]
    probe = probenet.peer_is_probe(allowed, users)
    if probe != (p["pubkey"] != sys.argv[2]):
        print("правило зондов ошиблось на пире", p["pubkey"][:8], allowed); sys.exit(1)
print("ok")
PYEOF
)" || fail "$out"
[ "$(ip netns exec lab-A awg show awg0 peers | wc -l)" = 4 ] || fail "на живом awg0 не 4 пира: $(ip netns exec lab-A awg show awg0 allowed-ips)"

# ── 3. ответчик и зонды ──────────────────────────────────────────────────────────────────────────
qpy lab-A responder --side a --addr "$RESP" --iface awg0 --members a,b,a1,a2 --g 1 --epoch 5 --status "$Q/resp.json" \
    >"$Q/resp.log" 2>&1 & PIDS="$PIDS $!"
wait_rq 's.get("v") == 3' 10 >/dev/null || fail "ответчик не поднялся: $(cat "$Q/resp.log")"
probe_conf() {   # probe_conf <узел> — конфиг зонда к основе
    cat <<EOF
[Interface]
Address = ${ADDR[$1]}/32
PrivateKey = ${PRIV[$1]}
$OBF

[Peer]
PublicKey = $A_PUB
PresharedKey = ${PSK[$1]}
AllowedIPs = $RESP/32
Endpoint = $A_PUB_IP:$A_PORT
EOF
}
for n in B G1 G2; do
    probe_conf "$n" > "$Q/$n/awgqah.conf"; chmod 600 "$Q/$n/awgqah.conf"
    out="$(QUO_IF=awgqah qsh "$n" --up "$Q/$n/awgqah.conf" 2>&1)" || fail "зонд $n не поднялся: $out"
    [ "$out" = "up: ok" ] || fail "зонд $n: «$out»"
    ip -n "lab-$n" link show awgqah >/dev/null 2>&1 && fail "интерфейс зонда $n остался у хоста"
    ip netns exec "awg-quo-$n" ip -4 addr show awgqah | grep -q "inet ${ADDR[$n]}/32" || fail "у зонда $n не его адрес"
done
s=10
for n in B G1 G2; do
    s=$((s + 1))
    rep="$(qpy "awg-quo-$n" probe --src "${ADDR[$n]}" --to "$RESP" --l a --g 1 --n "${NODE[$n]}" --e 5 --w see --s "$s" --timeout 5)" \
        || fail "проба $n не получила ответа: $rep; ответчик: $(cat "$Q/resp.json")"
    case "$rep" in *'"l": "a"'*'"e": 5'*'"s": '"$s"*) ;; *) fail "ответ зонду $n не тот: $rep" ;; esac
done
case "$rep" in *'"y": 3, "v": 3'*) ;; *) fail "после трёх проб ответчик считает не 3 из 3: $rep" ;; esac
wait_rq 's["role"] == "lead"' 8 >/dev/null || fail "ответчик с тремя «да» не лидер: $(cat "$Q/resp.json")"
out="$(qsh B --status)"
printf '%s\n' "$out" | grep -q '^ns=present' || fail "status B: $out"
printf '%s\n' "$out" | grep -q '^hs_awgqah=[0-9]' || fail "у зонда B нет рукопожатия: $out"
out="$(QUO_IF=awgqah qsh B --up "$Q/B/awgqah.conf" 2>&1)"
[ "$out" = "up: same" ] || fail "повторный --up тем же конфигом: «$out»"
qpy awg-quo-B probe --src "${ADDR[B]}" --to "$RESP" --l a --g 1 --n b --e 5 --s 20 >/dev/null || fail "после повторного --up проба B без ответа"

# ── 4. из зонда — только на ответчик ─────────────────────────────────────────────────────────────
[ -z "$(ip -n awg-quo-B route show default)" ] || fail "в awg-quo-B есть маршрут по умолчанию"
ip netns exec awg-quo-B ping -c1 -W2 "$A_PUB_IP" >/dev/null 2>&1 && fail "из зонда виден интернет ($A_PUB_IP)"
ip netns exec awg-quo-B ping -c1 -W2 10.8.1.1 >/dev/null 2>&1 && fail "из зонда видна подсеть пользователей"
ip netns exec awg-quo-B ping -c1 -W2 "$RESP" >/dev/null 2>&1 && fail "из зонда проходит icmp на ответчик (выход — только udp на порт)"
qpy awg-quo-B probe --src "${ADDR[B]}" --to "$RESP" --port 8790 --l a --n b --s 21 --timeout 2 >/dev/null 2>&1 \
    && fail "из зонда проходит udp на другой порт ответчика"

# ── 5. пользователь основы и подмена ─────────────────────────────────────────────────────────────
ip -n lab-cA link add awgc type amneziawg || fail "awgc в lab-cA не создан"
ip netns exec lab-cA awg setconf awgc <(cat <<EOF
[Interface]
PrivateKey = ${PRIV[cA]}
$OBF

[Peer]
PublicKey = $A_PUB
PresharedKey = ${PSK[cA]}
AllowedIPs = 10.8.1.0/24, 169.254.88.0/24
Endpoint = $A_PUB_IP:$A_PORT
PersistentKeepalive = 25
EOF
) || fail "awg setconf awgc"
ip -n lab-cA addr add 10.8.1.2/32 dev awgc; ip -n lab-cA link set awgc up
ip -n lab-cA route add 10.8.1.0/24 dev awgc; ip -n lab-cA route add 169.254.88.0/24 dev awgc
ip netns exec lab-cA ping -c1 -W3 10.8.1.1 >/dev/null || fail "туннель пользователя cA не живой (контроль)"
sleep 2; got0="$(rq 's["got"]')"        # статус ответчика — раз в секунду: база после него
qpy lab-cA probe --src 10.8.1.2 --to "$RESP" --l a --n a1 --e 5 --s 30 --timeout 3 >/dev/null && fail "пользователь получил ответ ответчика"
sleep 1
[ "$(rq 's["got"]')" -gt "$got0" ] || fail "проба пользователя (форма без фильтра) не дошла до ответчика — проверка отказа пуста"
[ "$(rq 'any(r["src"] == "10.8.1.2" for r in s["rejected"])')" = "true" ] || fail "проба пользователя не отвергнута ответчиком: $(cat "$Q/resp.json")"
[ "$(rq 'sorted(s["seen"])')" = '["a1", "a2", "b"]' ] || fail "в счёте лишний узел: $(rq 's["seen"]')"
ip -n lab-cA addr add 169.254.88.5/32 dev awgc
sleep 2; got0="$(rq 's["got"]')"        # статус ответчика — раз в секунду: база после него
qpy lab-cA probe --src 169.254.88.5 --to "$RESP" --l a --n a1 --e 5 --s 31 --timeout 3 >/dev/null && fail "проба с подменённым адресом зонда получила ответ"
sleep 1
[ "$(rq 's["got"]')" = "$got0" ] || fail "датаграмма с подменённым адресом зонда дошла до ответчика (AllowedIPs пира пользователя)"
ip -n lab-cA addr del 169.254.88.5/32 dev awgc
qpy lab-A guard --users 10.8.1.0/24 --awg-port "$A_PORT" > "$Q/guard.nft" || fail "рендер awg_bot_guard"
ip netns exec lab-A nft -f "$Q/guard.nft" || fail "nft отверг awg_bot_guard"
sleep 2; got0="$(rq 's["got"]')"        # статус ответчика — раз в секунду: база после него
qpy lab-cA probe --src 10.8.1.2 --to "$RESP" --l a --n a1 --e 5 --s 32 --timeout 3 >/dev/null && fail "при awg_bot_guard пользователь получил ответ"
sleep 1
[ "$(rq 's["got"]')" = "$got0" ] || fail "при awg_bot_guard проба пользователя дошла до ответчика"
for n in B G1 G2; do
    qpy "awg-quo-$n" probe --src "${ADDR[$n]}" --to "$RESP" --l a --n "${NODE[$n]}" --e 5 --s 33 --timeout 5 >/dev/null \
        || fail "при awg_bot_guard проба зонда $n не прошла"
done
ip netns exec lab-A nft delete table inet awg_bot_guard
sleep 2; counted0="$(rq 's["counted"]')"
qpy awg-quo-G1 probe --src "${ADDR[G1]}" --to "$RESP" --l a --n a2 --e 5 --s 34 --timeout 3 >/dev/null && fail "проба с чужим узлом получила ответ"
sleep 1
[ "$(rq 's["counted"]')" = "$counted0" ] || fail "проба с чужим узлом (a2 с адреса a1) засчитана"
[ "$(rq 'any(r["n"] == "a2" and r["src"] == "'"${ADDR[G1]}"'" for r in s["rejected"])')" = "true" ] || fail "подмена узла не видна ответчику"

# ── 6. поиск аплинка на G1 при поднятом awg-quo ──────────────────────────────────────────────────
mkif() {   # mkif <имя> <порт Endpoint> <сеть пира> — интерфейс AWG в lab-G1 с одним пиром к A
    local k; k="$(awg genkey)"
    ip -n lab-G1 link add "$1" type amneziawg || return 1
    printf '%s' "$k" | ip netns exec lab-G1 awg set "$1" private-key /dev/stdin peer "$A_PUB" allowed-ips "$3" \
        endpoint "$A_PUB_IP:$2" || return 1
}
mkif awglink 47201 10.99.97.0/30 && mkif awg1 "$A_PORT" 0.0.0.0/0 || fail "интерфейсы линка и аплинка на G1 не созданы"
up="$(qpy lab-G1 uplink --link-if awglink 2>&1)"
[ "$up" = "awg1" ] || fail "при поднятом awg-quo агент нашёл аплинк «$up», ожидался awg1"
mkif awgqtst "$A_PORT" 169.254.88.1/32 || fail "контрольный интерфейс не создан"
up="$(qpy lab-G1 uplink --link-if awglink 2>&1)"
[ "$up" = "-" ] || fail "контроль: зонд у хоста должен ломать поиск аплинка, а найден «$up» — проверка пуста"
ip -n lab-G1 link del awgqtst

# ── 7. выключение каждого узла — счёт ────────────────────────────────────────────────────────────
for n in B G1 G2; do
    qpy "awg-quo-$n" voter --src "${ADDR[$n]}" --to "$RESP" --l a --n "${NODE[$n]}" --e 5 --every 3 --status "$Q/$n/voter.json" \
        >"$Q/$n/voter.log" 2>&1 & PIDS="$PIDS $!"
done
wait_rq 's["y"] == 3' 15 >/dev/null || fail "три голосующих — не 3 «да»: $(cat "$Q/resp.json")"
times=""
for n in G2 G1 B; do
    t0=$(date +%s)
    qsh "$n" --down awgqah >/dev/null || fail "--down зонда $n"
    sleep 30
    [ "$(rq 's["y"]')" = 3 ] || fail "узел $n выпал из счёта раньше окна W (30 с после --down): $(cat "$Q/resp.json")"
    wait_rq 's["y"] == 2' 25 >/dev/null || fail "узел $n не выпал из счёта за W + 15 с: $(cat "$Q/resp.json")"
    dt=$(( $(date +%s) - t0 ))
    [ "$(rq '"'"${NODE[$n]}"'" not in s["yes"]')" = "true" ] || fail "выпал не тот узел: $(rq 's["yes"]')"
    times="$times $n:${dt}с"
    out="$(QUO_IF=awgqah qsh "$n" --up "$Q/$n/awgqah.conf" 2>&1)" || fail "зонд $n не поднялся снова: $out"
    wait_rq 's["y"] == 3' 20 >/dev/null || fail "узел $n после --up не вернулся в счёт: $(cat "$Q/resp.json")"
done
qsh G2 --wipe >/dev/null || fail "--wipe на G2"
ip netns list | grep -q '^awg-quo-G2' && fail "--wipe не снял пространство"
[ -e "$Q/G2/quo" ] && fail "--wipe не снял каталог конфигов: $(ls -a "$Q/G2/quo")"
wait_rq 's["y"] == 2' 60 >/dev/null || fail "после --wipe G2 остался в счёте: $(cat "$Q/resp.json")"
echo "ok (выпадение из счёта после --down:$times)"
