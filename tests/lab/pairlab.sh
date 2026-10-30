#!/bin/bash
# pairlab.sh — пара серверов на стенде для сценариев журнала (s05_journal, s06_return_tail);
# подключается сценарием (`. pairlab.sh`), сам не запускается.
#
# Узлы A («Alpha», основа, пригласивший — слушает канал пары) и B («Beta», резерв,
# принявший) — tests/lab/pairnode.py в пространствах lab-A и lab-B: своя база, канал
# пары по настоящему линку AWG awgpair (10.99.98.0/30, B → 203.0.113.5:47101), свой
# awg0 с настоящими ключами. Строка пары и ключи — как после сопряжения (сопряжение
# ведут свои тесты). Макет Bot API — в пространстве маршрутизатора lab-inet на всех
# адресах :8081 (узлы ходят на адрес .1 своей сети, гость резерва — через NAT зоны);
# журнал отправок — $PL/botapi.jsonl. Запись DNS для пользователей — $PL/shared/dns.json
# (мир pairnode.LabDns).
#
#   pl_init                       каталог, макет Bot API, оба узла настроены, линки подняты
#   pl_start <узел> [--poll]      запустить узел (A или B)
#   pl_stop <узел>                остановить
#   pl_cmd <узел> <команда> [json] [срок]   команда узлу, на stdout — итог JSON
#   pl_q <узел> <выражение>       значение из status.json (выражение python над s)
#   pl_wait <узел> <выражение> <срок> [что]   ждать, пока выражение не станет истиной
#   pl_pair_up                    pl_init, оба узла с поллингом, журнал включён, снимки приняты
#   pl_open_wait <срок>           ход процесса открытия у B до done или fail (open.status)
#   pl_guest_start / pl_guest_stop   бот основы на резерве (guest_main) в зоне, команды — $PL/G
#   pl_conf <узел> <ключ.через.точку> <значение JSON>   правка app.yaml узла (до pl_start)
#   pl_gw_start <слот> / pl_gw_stop <слот>   шлюз основы с кворумом (gwnode) в lab-G1, lab-G2, lab-GR (слот 3);
#                                 основе и резерву слоты — LAB_GW_SLOTS (до pl_start)
#   pl_gq <слот> <выражение>      значение из status.json шлюза (выражение python над s)
#   pl_cleanup                    всё снять (trap EXIT сценария)
set -u
PL_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; PL_ROOT="$(cd "$PL_HERE/../.." && pwd)"
PL_USER="${SUDO_USER:-$(id -un)}"; PL_HOME="$(getent passwd "$PL_USER" | cut -d: -f6)"
PL_VENV="${LAB_VENV:-$PL_HOME/venv}"
PY="${LAB_PY:-$PL_VENV/bin/python}"
PL="${LAB_PAIR_DIR:-/var/lib/awglab-pair}"   # не /tmp и не /var/tmp: у песочницы процесса открытия PrivateTmp
PL_Z="$PL_ROOT/install/reserve-zone-setup.sh"
TOKEN_A="880100:LAB-DUMMY-alpha"; TOKEN_B="880200:LAB-DUMMY-beta"
ADMIN_A=880101; ADMIN_B=880201
RECORD="vpn-a.lab.test"
declare -A PL_PID=()
declare -A PL_NS=([A]=lab-A [B]=lab-B)
declare -A PL_GW=([A]=203.0.113.1 [B]=198.51.100.1)
PL_BOTAPI_PID=""

pl_fail() { printf '%s\n' "$*"; exit 1; }

pl_env() {   # окружение узла; у A свои каталоги состояния зоны — зону и локальный канал гостя ведёт B
    local n="$1" w="$PL/$1"
    [ "$n" = A ] && printf '%s\n' "RSV_STATE_DIR=$w/rsv" "RSV_RUN=$w/grun" "RSV_VAR=$w/gvar" "RSV_ZONE=awg-guest-a"
    printf '%s\n' "AWG_BOT_CONF_DIR=$w/conf" "AWG_BOT_DATA_DIR=$w/data" "AWG_BOT_ENV=$w/env" \
        "LAB_SHARED=$PL/shared" "LAB_BOTAPI=http://${PL_GW[$n]}:8081" "LAB_VENV=$PL_VENV" \
        "LAB_NETNS=${PL_NS[$n]}" "LAB_RECORD=$RECORD" "PYTHONPATH=$PL_ROOT" "HOME=/root" \
        "QUO_NS=awg-quo-$n" "QUO_CONF_DIR=$w/quo" "LAB_GW_SLOTS=${LAB_GW_SLOTS:-}" "LAB_QKEYS_SHIM=${LAB_QKEYS_SHIM:-}" "LAB_GUARD_SHIM=${LAB_GUARD_SHIM:-}"
}

# Узлы входят только в сетевое пространство (nsenter --net), а не через `ip netns exec`:
# у того своё пространство монтирования, и хранилище гостя, смонтированное ботом, не
# увидели бы ни хост, ни юнит процесса открытия (на хосте бот и юнит — в одном).
pl_py() {    # pl_py <узел> <аргументы python…> — в пространстве узла, с его окружением
    local n="$1"; shift
    (cd "$PL_ROOT" && exec nsenter --net="/run/netns/${PL_NS[$n]}" env $(pl_env "$n") "$PY" "$@")
}

pl_awg_conf() {   # конфиг для awg setconf: без расширений awg-quick
    grep -v -E '^(Address|MTU|DNS|Table|PostUp|PostDown|SaveConfig) *=' "$1"
}

pl_links() {
    local OBF='Jc = 4
Jmin = 40
Jmax = 70
S1 = 10
S2 = 66
H1 = 5-500
H2 = 600-700
H3 = 800-900
H4 = 1000-1100'
    local pa pb ua ub psk
    pa="$(awg genkey)"; pb="$(awg genkey)"; ua="$(printf '%s' "$pa" | awg pubkey)"; ub="$(printf '%s' "$pb" | awg pubkey)"
    psk="$(awg genpsk)"
    cat > "$PL/A/awg/awgpair.conf" <<EOF
# Линк пары серверов AWG (резерв). Сгенерировано reserve-pair-setup.sh.
[Interface]
Address = 10.99.98.2/30
ListenPort = 47101
PrivateKey = $pa
$OBF

[Peer]
PublicKey = $ub
PresharedKey = $psk
AllowedIPs = 10.99.98.1/32
PersistentKeepalive = 25
EOF
    cat > "$PL/B/awg/awgpair.conf" <<EOF
# Линк пары серверов AWG (резерв). Сгенерировано reserve-pair-setup.sh.
[Interface]
Address = 10.99.98.1/30
ListenPort = 47101
PrivateKey = $pb
$OBF

[Peer]
PublicKey = $ua
PresharedKey = $psk
AllowedIPs = 10.99.98.2/32
Endpoint = 203.0.113.5:47101
PersistentKeepalive = 25
EOF
    local n port sub priv
    for n in A B; do
        [ "$n" = A ] && { port=45871; sub=10.8.1; } || { port=45872; sub=10.8.2; }
        priv="$(awg genkey)"
        # пиров нет: их заводит бот по устройствам базы (чужой пир сверка увела бы в карантин)
        cat > "$PL/$n/awg/awg0.conf" <<EOF
[Interface]
PrivateKey = $priv
Address = $sub.1/24
ListenPort = $port
MTU = 1376
$OBF
EOF
        awg genpsk > "$PL/$n/awg/wireguard_psk.key"           # общий PSK сервера: параметры интерфейса читают его вместе с конфигом
        chmod 600 "$PL/$n/awg/"*.conf "$PL/$n/awg/wireguard_psk.key"
        local ns="${PL_NS[$n]}" ifc addr
        ip netns exec "$ns" sysctl -qw net.ipv4.ip_forward=1      # сервер AWG пересылает (выход зоны гостя — через него)
        for ifc in awg0 awgpair; do
            ip -n "$ns" link del "$ifc" 2>/dev/null
            ip -n "$ns" link add "$ifc" type amneziawg || pl_fail "интерфейс $ifc в $ns не создан"
            ip netns exec "$ns" awg setconf "$ifc" <(pl_awg_conf "$PL/$n/awg/$ifc.conf") || pl_fail "awg setconf $ifc в $ns"
            addr="$(awk -F' *= *' '/^Address/{print $2}' "$PL/$n/awg/$ifc.conf")"
            ip -n "$ns" addr add "$addr" dev "$ifc"
            ip -n "$ns" link set "$ifc" up
        done
    done
}

pl_init() {
    pl_cleanup_quiet
    rm -rf "$PL"
    mkdir -p "$PL/shared" "$PL/A" "$PL/B"
    printf '{"%s": "203.0.113.5"}' "$RECORD" > "$PL/shared/dns.json"
    ip netns exec lab-inet env PYTHONPATH="$PL_ROOT" "$PY" -m tests.lab.botapi --listen 0.0.0.0:8081 \
        --journal "$PL/botapi.jsonl" >"$PL/botapi.log" 2>&1 & PL_BOTAPI_PID=$!
    local n
    (cd "$PL_ROOT" && "$PY" -m tests.lab.pairnode setup --work "$PL/A" --name Alpha --host "$RECORD" --self-ip 203.0.113.5 \
        --port 45871 --subnet 10.8.1 --token "$TOKEN_A" --admin "$ADMIN_A" --venv "$PL_VENV") || pl_fail "узел A не настроен"
    (cd "$PL_ROOT" && "$PY" -m tests.lab.pairnode setup --work "$PL/B" --name Beta --host 198.51.100.7 --self-ip 198.51.100.7 \
        --port 45872 --subnet 10.8.2 --token "$TOKEN_B" --admin "$ADMIN_B" --venv "$PL_VENV") || pl_fail "узел B не настроен"
    pl_links
    pl_py A -m tests.lab.pairnode seed --role inviter --peer-name Beta --peer-host 198.51.100.7 --peer-admin "$ADMIN_B" \
        >"$PL/A/seed.log" 2>&1 || pl_fail "база A не засеяна: $(tail -n5 "$PL/A/seed.log")"
    pl_py B -m tests.lab.pairnode seed --role acceptor --peer-name Alpha --peer-host 203.0.113.5 --peer-admin "$ADMIN_A" \
        >"$PL/B/seed.log" 2>&1 || pl_fail "база B не засеяна: $(tail -n5 "$PL/B/seed.log")"
    for _ in $(seq 1 20); do ip netns exec lab-A curl -s -o /dev/null "http://${PL_GW[A]}:8081/journal/x" && break; sleep 0.5; done
}

pl_pair_up() {
    pl_init
    pl_start A --poll
    pl_start B --poll
    pl_wait A 's.get("jrn_on") and s["jrn"]["capture"] == 1 and (s.get("pkg_sent") or {}).get("jrn")' 90 \
        "журнал включён, снимок A подтверждён резервом" || exit 1
    pl_wait B '(s.get("pkg_in") or {}).get("gen")' 30 "у B — снимок A (pkg3)" || exit 1
}

pl_open_wait() {
    local i st=""
    for i in $(seq 1 "$1"); do
        st="$(cat /run/awg-bot/rsv/open.status 2>/dev/null)"
        case "$st" in *'"step": "done"'*|*'"step": "fail"'*) break ;; esac
        sleep 1
    done
    printf '%s' "$st"
}

pl_start() {
    local n="$1"; shift
    # одна подоболочка с exec: $! — сам процесс узла (его и гасит pl_stop)
    (cd "$PL_ROOT" && exec nsenter --net="/run/netns/${PL_NS[$n]}" env $(pl_env "$n") "$PY" -m tests.lab.pairnode run \
        --work "$PL/$n" "$@") >>"$PL/$n/node.log" 2>&1 &
    PL_PID[$n]=$!
    sleep 2
    kill -0 "${PL_PID[$n]}" 2>/dev/null || pl_fail "узел $n не запустился: $(tail -n8 "$PL/$n/node.log")"
}

pl_stop() {
    local n="$1" p="${PL_PID[$1]:-}"
    [ -n "$p" ] || return 0
    kill "$p" 2>/dev/null
    for _ in $(seq 1 20); do kill -0 "$p" 2>/dev/null || break; sleep 0.25; done
    kill -9 "$p" 2>/dev/null
    PL_PID[$n]=""
}

# Бот основы на резерве: юнит зоны ходил бы в настоящий Telegram (и venv у него —
# /opt/awg-bot) — его гасим и запускаем tests/lab/guest_main.py в пространстве зоны
# с окружением из хранилища гостя и макетом Bot API (через veth и NAT зоны).
PL_GUEST_PID=""
pl_guest_start() {
    local run=/run/awg-bot-guest
    systemctl stop awg-bot-guest.service 2>/dev/null
    [ -s "$run/env" ] || pl_fail "в хранилище гостя нет env — процесс открытия не дошёл до store"
    mkdir -p "$PL/G"
    (cd "$PL_ROOT" && set -a && . "$run/env" && set +a && exec nsenter --net=/run/netns/awg-guest env AWG_BOT_GUEST=1 \
        AWG_BOT_CONF_DIR="$run/conf" AWG_BOT_DATA_DIR="$run/data" AWG_BOT_ENV="$run/env" \
        LAB_BOTAPI="http://${PL_GW[B]}:8081" LAB_GUEST_CMD="$PL/G" PYTHONPATH="$PL_ROOT" "$PY" -m tests.lab.guest_main) \
        >>"$PL/G/guest.log" 2>&1 &
    PL_GUEST_PID=$!
    sleep 3
    kill -0 "$PL_GUEST_PID" 2>/dev/null || pl_fail "бот основы на резерве не запустился: $(tail -n8 "$PL/G/guest.log")"
}

pl_guest_stop() {
    [ -n "$PL_GUEST_PID" ] || return 0
    kill "$PL_GUEST_PID" 2>/dev/null; sleep 1; kill -9 "$PL_GUEST_PID" 2>/dev/null; PL_GUEST_PID=""
}

pl_conf() {  # pl_conf <узел> <ключ.через.точку> <значение JSON> — правка conf/app.yaml узла
    "$PY" -c 'import json,sys,yaml
p=sys.argv[1]; d=yaml.safe_load(open(p, encoding="utf-8"))
cur=d; keys=sys.argv[2].split(".")
for k in keys[:-1]: cur=cur.setdefault(k, {})
cur[keys[-1]]=json.loads(sys.argv[3])
open(p, "w", encoding="utf-8").write(yaml.safe_dump(d, allow_unicode=True))' "$PL/$1/conf/app.yaml" "$2" "$3"
}

# Шлюз основы с кворумом: tests/lab/gwnode.py в пространстве lab-G<слот> (вход — nsenter --net:
# пространство зондов awg-quo-G<слот> скрипт должен смонтировать в общем пространстве монтирования).
declare -A PL_GWPID=()
declare -A PL_GWNS=([1]=G1 [2]=G2 [3]=GR)      # слот 3 (ансамбль v = 4) — в пространстве GR топологии
pl_gw_start() {
    local s="$1" w="$PL/G$1"
    mkdir -p "$w"
    (cd "$PL_ROOT" && exec nsenter --net="/run/netns/lab-${PL_GWNS[$s]}" env QUO_NS="awg-quo-G$s" QUO_CONF_DIR="$w/quo" \
        PYTHONPATH="$PL_ROOT" HOME=/root "$PY" -m tests.lab.gwnode --work "$w" --slot "$s" --pair "$PL") >>"$w/node.log" 2>&1 &
    PL_GWPID[$s]=$!
    sleep 1
    kill -0 "${PL_GWPID[$s]}" 2>/dev/null || pl_fail "шлюз $s не запустился: $(tail -n8 "$w/node.log")"
}

pl_gw_stop() {
    local p="${PL_GWPID[$1]:-}"
    [ -n "$p" ] || return 0
    kill "$p" 2>/dev/null; sleep 0.5; kill -9 "$p" 2>/dev/null; PL_GWPID[$1]=""
}

pl_gq() {    # pl_gq <слот> <выражение над s>
    "$PY" -c 'import json,sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    s = {}
try:
    v = eval(sys.argv[2], {"s": s})
except Exception:
    v = None
print(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)' "$PL/G$1/status.json" "$2"
}

pl_cmd() {   # pl_cmd <узел> <команда> [json] [срок, с]
    local n="$1" c="$2" body="${3:-{\}}" budget="${4:-60}" d="$PL/$1/cmd"
    rm -f "$d/$c.done" "$d/$c.err"
    printf '%s' "$body" > "$d/$c.tmp" && mv "$d/$c.tmp" "$d/$c"
    local i
    for i in $(seq 1 $((budget * 4))); do
        [ -e "$d/$c.done" ] && { cat "$d/$c.done"; return 0; }
        [ -e "$d/$c.err" ] && { cat "$d/$c.err"; return 1; }
        sleep 0.25
    done
    printf 'команда %s узлу %s не выполнена за %s с' "$c" "$n" "$budget"; return 1
}

pl_q() {     # pl_q <узел> <выражение над s>
    "$PY" -c 'import json,sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    s = {}
try:
    v = eval(sys.argv[2], {"s": s})
except Exception:
    v = None
print(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)' "$PL/$1/status.json" "$2"
}

pl_wait() {  # pl_wait <узел> <выражение> <срок> [что ждём]
    local n="$1" expr="$2" budget="$3" what="${4:-$2}"
    local i
    for i in $(seq 1 "$budget"); do
        [ "$(pl_q "$n" "bool($expr)")" = "true" ] && return 0
        sleep 1
    done
    printf 'не дождался за %s с на %s: %s\n' "$budget" "$n" "$what"
    printf 'статус %s: %s\n' "$n" "$(cat "$PL/$n/status.json" 2>/dev/null | cut -c1-1500)"
    printf 'журнал %s:\n%s\n' "$n" "$(tail -n 25 "$PL/$n/node.log" 2>/dev/null)"
    return 1
}

pl_cleanup_quiet() {
    local n
    pl_guest_stop
    pkill -9 -f "[g]uest_main" 2>/dev/null
    for n in A B; do pl_stop "$n"; done
    pkill -9 -f "[p]airnode run --work $PL/" 2>/dev/null       # и узлы прерванного прогона
    for n in 1 2 3; do pl_gw_stop "$n"; done
    pkill -9 -f "[g]wnode --work $PL/" 2>/dev/null
    for n in A B G1 G2 G3; do ip netns del "awg-quo-$n" 2>/dev/null; done   # зонды — вместе с пространством
    [ -n "$PL_BOTAPI_PID" ] && kill -9 "$PL_BOTAPI_PID" 2>/dev/null; PL_BOTAPI_PID=""
    systemctl stop awg-bot-guest.service awg-bot-guest-dns.service awg-bot-rsvopen.service 2>/dev/null
    ip netns exec lab-B bash "$PL_Z" --teardown >/dev/null 2>&1; ip netns exec lab-B bash "$PL_Z" --wipe >/dev/null 2>&1
    bash "$PL_Z" --teardown >/dev/null 2>&1; bash "$PL_Z" --wipe >/dev/null 2>&1
    ip netns del awg-guest 2>/dev/null
    for n in A B; do ip -n "lab-$n" link del awgpair 2>/dev/null; ip -n "lab-$n" link del awg0 2>/dev/null; done
    for n in A B; do                                       # «гибель» узла (link down) снимает и маршрут по умолчанию
        ip -n "lab-$n" link set "v-$n" up 2>/dev/null
        ip -n "lab-$n" route replace default via "${PL_GW[$n]}" 2>/dev/null
    done
    bash "$PL_HERE/net.sh" heal >/dev/null 2>&1
}

pl_cleanup() {
    pl_cleanup_quiet
    [ -n "${PL_KEEP:-}" ] || rm -rf "$PL"
}
