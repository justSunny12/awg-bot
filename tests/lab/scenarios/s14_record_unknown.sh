#!/bin/bash
# s14_record_unknown — исход смены записи DNS неизвестен (tests/lab/pairlab.sh): процесс открытия на
# резерве погиб сразу после того, как провайдер принял правку, — до шага record_set. Прежде бот
# резерва считал это «запись не сменена» и снимал зону, хотя запись уже вела пользователей на резерв.
#
# Проверяет:
#   1. аварийное открытие по коду (A выключена и мертва для всех), запись меняет процесс открытия
#      (LAB_OPEN_RECORD — мир DNS стенда); рычаг set.kill: провайдер стенда пишет запись и убивает
#      процесс (SIGKILL) — в open.status остаётся шаг-намерение record_try, запись — на адресе B;
#   2. бот B сам читает авторитетный ответ (мир DNS стенда): адрес его — открытие доделано
#      (rec_done, rec_moved), отметка tg_take на месте, зона не снята (клон awg0 в зоне), строки
#      «принять не удалось» админу B нет, «не принял» основе (down) — нет;
#   3. гость запущен — ставит вебхук: B serving с tg_taken, гость — держатель эпохи, вебхук бота
#      основы у макета стоит.
#
# Не покрыто здесь: ожидание факта до предела провайдера (5 минут у API-провайдеров) и срыв по
# пределу — в tests/integration/test_reserve_record_unknown.py и test_rsvopen.py.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/pairlab.sh"
. "$HERE/quolab.sh"
trap 'rm -f "$PL/shared/set.kill"; pl_cleanup' EXIT
API_B="http://${PL_GW[B]}:8081"

inject_b() {     # inject_b <json> — обновление боту B через макет (из пространства B: A мертва)
    ip netns exec lab-B curl -s -m5 -X POST "$API_B/inject/$TOKEN_B" -H 'Content-Type: application/json' -d "$1" >/dev/null
}
hook_url() {     # адрес вебхука бота основы у макета
    ip netns exec lab-B curl -s -m5 "$API_B/bot$TOKEN_A/getWebhookInfo" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["result"]["url"])'
}
zone_phase() { ip netns exec lab-B bash "$PL_Z" --status 2>/dev/null | sed -n 's/^phase=//p' | head -n1; }
record_now() {   # адрес записи пользователей в мире DNS стенда
    "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$PL/shared/dns.json" "$RECORD"
}

# ── 1. аварийное открытие по коду, процесс погибает после записи ─────────────────────────────────
export LAB_OPEN_RECORD=1
pl_pair_up
pl_cmd A code "{\"out\": \"$PL/shared/code\"}" >/dev/null || pl_fail "аварийный код A не получен"
nsenter --net=/run/netns/lab-B bash "$PL_Z" --shell >/dev/null || pl_fail "оболочка зоны в lab-B не поднялась"
pl_stop A; bash "$PL_HERE/net.sh" kill A >/dev/null
pl_wait B '(s.get("code_window") or {}).get("open")' 200 "у B открыт приём аварийного кода (Н4)" || exit 1
: > "$PL/shared/set.kill"                      # провайдер стенда: правка легла — процесс убит до ответа
cbdata="$(cd "$PL_ROOT" && "$PY" -c 'from awgbot.bot.callbacks import PeerCB; print(PeerCB(action="code").pack())')"
inject_b "{\"callback_query\": {\"id\": \"lab-1\", \"chat_instance\": \"lab\", \"data\": \"$cbdata\",
  \"from\": {\"id\": $ADMIN_A, \"is_bot\": false, \"first_name\": \"Админ A\"},
  \"message\": {\"message_id\": 7, \"date\": 1, \"chat\": {\"id\": $ADMIN_A, \"type\": \"private\"}, \"text\": \"роль\"}}}"
sleep 3
inject_b "{\"message\": {\"message_id\": 8, \"date\": 1, \"chat\": {\"id\": $ADMIN_A, \"type\": \"private\"},
  \"from\": {\"id\": $ADMIN_A, \"is_bot\": false, \"first_name\": \"Админ A\"}, \"text\": \"$(cat "$PL/shared/code")\"}}"
pl_wait B '(s.get("in") or {}).get("state") in ("taking", "serving")' 60 "B принял код и запустил открытие" || exit 1
T0=$(date +%s)
wait_t B '(s.get("in") or {}).get("rec_done") or (s.get("in") or {}).get("state") == "idle"' 180 \
    "B: исход смены записи после гибели процесса решён" || exit 1
t_done=$WT
st="$(cat /run/awg-bot/rsv/open.status 2>/dev/null)"
case "$st" in *'"step": "record_try"'*) ;; *) pl_fail "в open.status не намерение record_try — процесс погиб не там: $st · $(tail -n3 /run/awg-bot/rsv/rsvopen.err 2>/dev/null)" ;; esac
[ ! -e "$PL/shared/set.kill" ] || pl_fail "рычаг set.kill не сработал — провайдер стенда процесс не убивал"
[ "$(record_now)" = 198.51.100.7 ] || pl_fail "запись после гибели процесса не на B: $(cat "$PL/shared/dns.json")"

# ── 2. бот B доделал открытие по факту, зона на месте ────────────────────────────────────────────
[ "$(pl_q B 's["in"].get("state")')" = taking ] || pl_fail "B не в приёме после гибели процесса: $(pl_q B 's["in"]')"
[ "$(pl_q B 'bool(s["in"].get("rec_moved"))')" = true ] || pl_fail "B не считает запись своей: $(pl_q B 's["in"]')"
ip netns exec awg-guest ip link show awg0 >/dev/null 2>&1 || pl_fail "клона awg0 в зоне нет — зону сняли при применённой записи"
case "$(zone_phase)" in taking|serving) ;; *) pl_fail "фаза зоны $(zone_phase), ожидалась taking или serving" ;; esac
[ -e /run/awg-bot-guest/tg_take ] || pl_fail "факт записи есть, а отметки tg_take нет"
grep -q "открытие доделываю сам" "$PL/B/node.log" || pl_fail "в журнале B нет строки о доделанном открытии"
! grep -q "пользователей «.*» не принял" "$PL/B/node.log" || pl_fail "B сорвал приём: $(grep 'не принял' "$PL/B/node.log" | tail -n2)"
[ "$(tg_count "$TOKEN_B" "$ADMIN_B" "принять не удалось")" = 0 ] || pl_fail "админу B ушло «принять не удалось» при применённой записи"
note "1–2: исход решён через ${t_done} с после старта открытия, зона на месте"

# ── 3. гость ставит вебхук и держит эпоху ────────────────────────────────────────────────────────
pl_guest_start
T0=$(date +%s)
wait_t B 's["in"].get("state") == "serving" and s["in"].get("tg_taken")' 180 "B: гость взял Telegram (tg_taken)" || exit 1
t_taken=$WT
pl_wait G 's.get("role") == "holder" and s.get("holder") == "self"' 120 "гость — держатель эпохи" || exit 1
[ -n "$(hook_url)" ] || pl_fail "B считает tg_taken, а вебхука бота основы у макета нет"
ip netns exec awg-guest ip link show awg0 >/dev/null 2>&1 || pl_fail "клон awg0 пропал после захвата Telegram"
note "3: Telegram у гостя через ${t_taken} с после его старта"
echo "ok (${SUMMARY#; })"
