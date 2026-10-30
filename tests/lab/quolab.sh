#!/bin/bash
# quolab.sh — пара серверов с кворумом резерва для сценариев s06_quorum_move и s07_quorum_tie;
# подключается сценарием после pairlab.sh (`. quolab.sh`), сам не запускается.
#
# Голосующие о линии A — второй сервер B и шлюзы основы: tests/lab/gwnode.py (настоящий голосующий
# агента) в lab-G1, lab-G2 и lab-GR (слот 3, ансамбль v = 4); слова каналов шлюзов — шиной стенда.
#
#   q_up <слоты через запятую>    свежая пара, шлюзы, кворум линии A включён, A — лидер, все видят A
#   fault <правило nft>           свой рычаг в таблице рычагов net.sh (net.sh heal её снимает)
#   wait_t <узел> <выражение> <срок> <что>   ждать истины; секунд с T0 — в WT
#   take_check <что> <срок>       B начал открытие не позже срока с T0 (секунды — в WT)
#   guest_epoch_check <эпоха A>   процесс открытия done, эпоха гостя — A + 1, держатель
#   tg_count <токен> <чат> <подстрока> [с ts]   сколько sendMessage бота в чат с подстрокой
#   note <строка>                 строка итога сценария
#
#   q_move_button                 переезд кнопкой A → B до отъезда (гость запущен, запись — сценарием)
#   dns_set <адрес>               запись для пользователей стенда
#   quo_defects                   известные дефекты кода — провал сразу, если не включён обход
#
# Дефекты, которые стенд обходит только по явному флагу (pairnode): порог t в слове qkeys
# затирается видом сообщения конверта (gwlink.pack: вид — в ключе t) — B отвергает qkeys, кворум не
# включается (LAB_QKEYS_SHIM=1: порог подставляется на приёме); пакет nftguard без TG_OUT4 и
# SET_TG_OUT — awg_bot_guard с awggx не собирается, у зоны резерва нет выхода наружу
# (LAB_GUARD_SHIM=1: имена из common — в пакет).
GUEST_DB=/run/awg-bot-guest/data/bot.db
API_A="http://${PL_GW[A]}:8081"
SUMMARY=""

note() { SUMMARY="$SUMMARY; $*"; printf '%s\n' "$*"; }

fault() {
    ip netns exec lab-inet nft list table ip labfault >/dev/null 2>&1 || ip netns exec lab-inet nft -f - <<'EOF'
table ip labfault {
    chain faults { type filter hook forward priority filter - 10; policy accept; }
}
EOF
    ip netns exec lab-inet nft add rule ip labfault faults $1
}

since() { echo $(( $(date +%s) - $1 )); }

wait_t() {
    local i
    for i in $(seq 1 "$3"); do
        if [ "$(pl_q "$1" "bool($2)")" = "true" ]; then WT=$(since "$T0"); return 0; fi
        sleep 1
    done
    pl_wait "$1" "$2" 1 "$4"; return 1
}

q_up() {
    export LAB_GW_SLOTS="$1"
    pl_init
    for n in A B; do pl_conf "$n" reserve.return "\"${Q_RETURN:-button}\""; done
    [ -n "${Q_MIN_INTERVAL:-}" ] && for n in A B; do pl_conf "$n" reserve.min_interval_minutes "$Q_MIN_INTERVAL"; done
    pl_start A --poll
    pl_start B --poll
    nsenter --net=/run/netns/lab-B bash "$PL_Z" --shell >/dev/null || pl_fail "оболочка зоны в lab-B не поднялась"
    local s
    for s in ${1//,/ }; do pl_gw_start "$s"; done
    local v=$(( $(echo "${1//,/ }" | wc -w) + 1 ))
    pl_wait A 's.get("jrn_on") and s["jrn"]["capture"] == 1' 120 "журнал A включён" || exit 1
    if ! pl_wait A 's["quo"]["active"]' 180 "кворум линии A включён (ключи подтверждены B и шлюзами)" >"$PL/up.err"; then
        local q; q="$(grep -m1 "сообщение 'qkeys' не обработано" "$PL/B/node.log")"
        [ -n "$q" ] && pl_fail "кворум не включился: B отвергает qkeys — «${q#*канал пары: }» (порог t затёрт видом конверта); причины у A: $(pl_q A 's["quo"]["why"]')"
        cat "$PL/up.err"; exit 1
    fi
    pl_wait A "s['quo']['count'].get('role') == 'lead' and s['quo']['count'].get('y') == $v and s['jrn']['role'] == 'holder'" 180 \
        "A — лидер, «да» от всех $v голосующих" || exit 1
    pl_wait B "s['quo']['cand'].get('own') == 'see' and s['quo']['cand'].get('see') == $v and s['quo']['cand'].get('v') == $v" 120 \
        "B — кандидат: сам видит A, шлюзы видят" || exit 1
    pl_wait B 's["pkg_in"].get("m_gen") and s["pkg_in"]["m_gen"] == '"$(pl_q A 's["m_gen"]')" 120 \
        "у B — пакет под ключом поколения, включённого кворумом" || exit 1
    case "$(pl_q A 's["quo"]["mine"]["members"]')" in
        *'"a"'*'"b"'*'"a1"'*) ;; *) pl_fail "состав линии A не тот: $(pl_q A 's["quo"]["mine"]')" ;; esac
}

take_check() {
    wait_t B 's["in"].get("state") in ("taking", "serving")' "$2" "$1" || exit 1
}

guest_epoch_check() {
    local st; st="$(pl_open_wait 180)"
    case "$st" in *'"step": "done"'*) ;; *) pl_fail "процесс открытия не дошёл до done: $st · $(tail -n3 /run/awg-bot/rsv/rsvopen.err 2>/dev/null)" ;; esac
    systemctl stop awg-bot-guest.service 2>/dev/null
    local got; got="$("$PY" -c 'import sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True)
print("%s/%s/%s" % c.execute("SELECT epoch, holder, role FROM jrn_meta").fetchone())' "$GUEST_DB")"
    [ "$got" = "$(( $1 + 1 ))/self/holder" ] || pl_fail "эпоха гостя после открытия по кворуму $got, ожидалась $(( $1 + 1 ))/self/holder"
}

tg_count() {
    "$PY" -c 'import json,sys
path, bot, chat, sub = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
since = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0
try:
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
except OSError:
    rows = []
print(sum(1 for r in rows if r["method"] == "sendMessage" and r["bot"] == bot and r["ts"] >= since
          and str(r["params"].get("chat_id")) == chat and sub in str(r["params"].get("text"))))' "$PL/botapi.jsonl" "${1%%:*}" "$2" "$3" "${4:-0}"
}

q_move_button() {   # переезд кнопкой A → B до отъезда: снимок, открытие, гость, запись (меняет сценарий)
    local res; res="$(pl_cmd A move '{}')" || pl_fail "переезд кнопкой не начат: $res"
    pl_wait A 's["out"].get("state") != "leaving" or s["out"].get("step") != "pkg"' 150 "A вышла из шага pkg" || exit 1
    [ "$(pl_q A 's["out"].get("state")')" = "leaving" ] || pl_fail "переезд сорван на шаге pkg: $(pl_q A 's["out"]')"
    local st; st="$(pl_open_wait 150)"
    case "$st" in *'"step": "done"'*) ;; *) pl_fail "процесс открытия при переезде не дошёл до done: $st" ;; esac
    pl_guest_start
    pl_wait A 's["out"].get("step") in ("record", "released") or s["out"].get("state") == "away"' 150 "A на шаге записи" || exit 1
    dns_set 198.51.100.7
    pl_wait A 's["out"].get("state") == "away" and s["jrn"]["role"] == "follower" and s["jrn"]["holder"] == "peer"' 240 \
        "A в отъезде и следует" || exit 1
    # гость на старте — без права записи до большинства «да» о клоне (A и шлюзы: два ответа подряд каждому)
    pl_wait G 's.get("role") == "holder" and s.get("holder") == "self"' 180 "гость — держатель эпохи" || exit 1
}

dns_set() {         # запись для пользователей — на адрес (у стенда нет провайдера: меняет сценарий)
    "$PY" -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d[sys.argv[2]]=sys.argv[3]; json.dump(d, open(p, "w"))' \
        "$PL/shared/dns.json" "$RECORD" "$1"
}

quo_defects() {     # известные стенду дефекты кода: без обхода (LAB_*_SHIM=1) — провал сразу, с причиной
    local out
    out="$(cd "$PL_ROOT" && "$PY" - "${LAB_QKEYS_SHIM:-}" "${LAB_GUARD_SHIM:-}" <<'PYEOF'
import sys
from awgbot.util import gwlink, pairproto
bad = []
if sys.argv[1] != "1":
    # слово qkeys через настоящий конверт канала пары: порог t не должен затираться видом сообщения
    key, nonce = b"k" * 32, b"n" * 16
    body = {"l": "a", "m_gen": 2, "q_gen": 1, "t": 2, "x": 1, "y": "00" * 16, "qbox": "00" * 72,
            "c_gen": 1, "cy": "00" * 16, "cbox": "00" * 72}
    msg = gwlink.unpack(key, gwlink.pack(key, "qkeys", body, seq=1, nonce=nonce), nonce=nonce)
    try:
        pairproto.check(pairproto.PAIR_PROTO, "qkeys", msg, pairproto.INVITER)
    except pairproto.ProtocolError as e:
        bad.append(f"qkeys не проходит конверт канала пары ({e}: gwlink.pack кладёт вид в ключ t) — кворум не включится")
if sys.argv[2] != "1":
    # таблица awg_bot_guard при оболочке зоны с awggx (выход агентов шлюзов в Telegram)
    from awgbot.infra import nftguard
    from awgbot.infra.nftguard.render import render
    spec = nftguard.GuardSpec(ssh_port=22, ssh_open=True, tunnel_nets4=["10.8.2.0/24"], wan_ifs=["v-B"],
                              zone_tg_if="awggx-h", zone_tg_net4="169.254.77.8/30")
    try:
        render(spec)
    except AttributeError as e:
        bad.append(f"awg_bot_guard с awggx не собирается ({e}) — файервол хоста резерва не ставится, у зоны нет выхода наружу")
print("; ".join(bad))
PYEOF
)" || pl_fail "проверка дефектов не прошла: $out"
    [ -z "$out" ] || pl_fail "ДЕФЕКТ: $out (обход стенда: LAB_QKEYS_SHIM=1 LAB_GUARD_SHIM=1)"
}
