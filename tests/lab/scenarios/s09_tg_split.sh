#!/bin/bash
# s09_tg_split — VPN и бот основы на резерве разделены (tests/lab/pairlab.sh): аварийный переезд,
# где бот основы на резерве не взял Telegram после смены записи, зону не снимает — пользователи
# уже идут на клон; переезд кнопкой, при котором основа погибла между up и released, а гость
# забрал Telegram сам, ожившая основа не откатывает.
#
# Проверяет:
#   1. аварийное открытие по коду (A выключена и мертва для всех): запись меняет сам процесс
#      открытия (LAB_OPEN_RECORD — мир DNS стенда), open.status — done без record=skipped,
#      запись — на адрес B, отметка tg_take на месте;
#   2. боту основы на резерве закрыт макет Bot API (правило в пространстве зоны): за TAKEN_WAIT_S
#      (сжат LAB_CONST шага 1) Telegram не взят — зона не снята (клон awg0 в зоне, фаза зоны serving), B —
#      serving без tg_taken с причиной tg_wait; админу B — уведомление «VPN работает, бот … не
#      отвечает в Telegram», в карточке B — строка «└ ⚠️ бот … здесь не отвечает в Telegram»;
#   3. повтор захвата раз в TG_RETRY_S (± 40 %; сжат до RETRY): снятая отметка tg_take появляется снова
#      не позже RETRY·1,4 + 10 с, новая строка о повторном запуске юнита гостя (после снятия отметки) —
#      в журнале B;
#   4. макет снова открыт: гость ставит вебхук — B serving с tg_taken, причина и строка карточки
#      сняты, зона на месте;
#   5. новая пара, переезд кнопкой: A на шаге записи (авторитетный сервер не подтверждает факт —
#      отметка auth.hold), запись уже на B; A гибнет до released; B по сроку DONE_BUTTON_WAIT_S
#      (сжат LAB_CONST шага 5) отдаёт Telegram гостю сам (tg_take_forced, takeover) — гость держатель с
#      вебхуком, st B — in_tg forced;
#   6. A оживает: по сроку шага record (RECORD_WAIT_S, сжат LAB_CONST шага 5) уходит в away без отката записи (запись —
#      на B) и без back (гость жив, B serving); возврат кнопкой — пользователи и эпоха снова у A.
#
# Не покрыто здесь: настоящий юнит гостя — на стенде его заменяет tests/lab/guest_main.py, и
# повтор захвата поднимает юнит без кода (/opt/awg-bot нет) — видно только попытку.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/pairlab.sh"
. "$HERE/quolab.sh"
trap pl_cleanup EXIT
API_B="http://${PL_GW[B]}:8081"
INB=awgbot.domain.services.reserve.inbound
OUTB=awgbot.domain.services.reserve.outbound

inject_b() {     # inject_b <json> — обновление боту B через макет (из пространства B: A мертва)
    ip netns exec lab-B curl -s -m5 -X POST "$API_B/inject/$TOKEN_B" -H 'Content-Type: application/json' -d "$1" >/dev/null
}
tg_block() {     # tg_block on|off — боту основы на резерве закрыт макет Bot API (выход из зоны)
    ip netns exec awg-guest nft delete table ip labtg 2>/dev/null
    [ "$1" = on ] || return 0
    ip netns exec awg-guest nft -f - <<EOF
table ip labtg {
    chain out { type filter hook output priority -10; policy accept; ip daddr ${PL_GW[B]} tcp dport 8081 reject with tcp reset; }
}
EOF
}
hook_url() {     # адрес вебхука бота основы у макета
    ip netns exec lab-B curl -s -m5 "$API_B/bot$TOKEN_A/getWebhookInfo" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["result"]["url"])'
}
zone_phase() { ip netns exec lab-B bash "$PL_Z" --status 2>/dev/null | sed -n 's/^phase=//p' | head -n1; }

# ── 1. аварийное открытие по коду, запись меняет процесс открытия ────────────────────────────────
RETRY=20                                       # повтор захвата гостем (inbound_tgwait.TG_RETRY_S, на хостах 60 ± 40 %)
export LAB_OPEN_RECORD=1 LAB_CONST="$INB:TAKEN_WAIT_S=30;awgbot.domain.services.reserve.inbound_tgwait:TG_RETRY_S=$RETRY"
pl_pair_up
pl_cmd A code "{\"out\": \"$PL/shared/code\"}" >/dev/null || pl_fail "аварийный код A не получен"
nsenter --net=/run/netns/lab-B bash "$PL_Z" --shell >/dev/null || pl_fail "оболочка зоны в lab-B не поднялась"
tg_block on || pl_fail "правило зоны на макет Bot API не встало"
pl_stop A; bash "$PL_HERE/net.sh" kill A >/dev/null
pl_wait B '(s.get("code_window") or {}).get("open")' 200 "у B открыт приём аварийного кода (Н4)" || exit 1
cbdata="$(cd "$PL_ROOT" && "$PY" -c 'from awgbot.bot.callbacks import PeerCB; print(PeerCB(action="code").pack())')"
inject_b "{\"callback_query\": {\"id\": \"lab-1\", \"chat_instance\": \"lab\", \"data\": \"$cbdata\",
  \"from\": {\"id\": $ADMIN_A, \"is_bot\": false, \"first_name\": \"Админ A\"},
  \"message\": {\"message_id\": 7, \"date\": 1, \"chat\": {\"id\": $ADMIN_A, \"type\": \"private\"}, \"text\": \"роль\"}}}"
sleep 3
inject_b "{\"message\": {\"message_id\": 8, \"date\": 1, \"chat\": {\"id\": $ADMIN_A, \"type\": \"private\"},
  \"from\": {\"id\": $ADMIN_A, \"is_bot\": false, \"first_name\": \"Админ A\"}, \"text\": \"$(cat "$PL/shared/code")\"}}"
pl_wait B '(s.get("in") or {}).get("state") in ("taking", "serving")' 60 "B принял код и запустил открытие" || exit 1
st="$(pl_open_wait 180)"
case "$st" in *'"step": "done"'*) ;; *) pl_fail "процесс открытия по коду не дошёл до done: $st · $(tail -n3 /run/awg-bot/rsv/rsvopen.err 2>/dev/null)" ;; esac
case "$st" in *skipped*) pl_fail "процесс открытия не менял запись (record=skipped) — мир DNS стенда не подключён: $st" ;; esac
[ "$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$PL/shared/dns.json" "$RECORD")" = 198.51.100.7 ] \
    || pl_fail "запись после открытия не на B: $(cat "$PL/shared/dns.json")"
[ -e /run/awg-bot-guest/tg_take ] || pl_fail "после record_fact нет отметки tg_take"
pl_guest_start
T0=$(date +%s)

# ── 2. Telegram не взят за TAKEN_WAIT_S — зона остаётся ──────────────────────────────────────────
wait_t B 's["in"].get("state") == "serving" and not s["in"].get("tg_taken") and s["in"].get("tg_wait")' 150 \
    "B: serving без бота (tg_wait) после срока TAKEN_WAIT_S" || exit 1
t_wait=$WT
ip netns exec awg-guest ip link show awg0 >/dev/null 2>&1 || pl_fail "клона awg0 в зоне нет — зону сняли из-за Telegram"
[ "$(zone_phase)" = serving ] || pl_fail "фаза зоны $(zone_phase), ожидалась serving"
[ "$(pl_q B 's["in_tg"]')" = wait ] || pl_fail "st.in_tg у B: $(pl_q B 's["in_tg"]'), ожидалось wait"
for _ in $(seq 1 20); do [ "$(tg_count "$TOKEN_B" "$ADMIN_B" "VPN работает")" -ge 1 ] && break; sleep 1; done
[ "$(tg_count "$TOKEN_B" "$ADMIN_B" "VPN работает")" = 1 ] \
    || pl_fail "админу B уведомлений «VPN работает, бот не отвечает»: $(tg_count "$TOKEN_B" "$ADMIN_B" "VPN работает"), ожидалось одно"
card="$(pl_cmd B card)" || pl_fail "карточка B не собрана: $card"
case "$card" in *"здесь не отвечает в Telegram"*) ;; *) pl_fail "в карточке B нет строки о боте без Telegram: ${card:0:600}" ;; esac

# ── 3. повтор захвата ────────────────────────────────────────────────────────────────────────────
# строки журнала о запуске юнита гостя такт пишет на каждом повторе, пока юнит не активен, — шаг 2 мог
# оставить их до снятия отметки: в зачёт идёт только новая
tg_up_re="бот основы на резерве (запущен снова|не запустился)"
n_up0=$(grep -cE "$tg_up_re" "$PL/B/node.log" 2>/dev/null); n_up0=${n_up0:-0}
rm -f /run/awg-bot-guest/tg_take
t_rm=$(date +%s)
for _ in $(seq 1 100); do [ -e /run/awg-bot-guest/tg_take ] && break; sleep 1; done
[ -e /run/awg-bot-guest/tg_take ] || pl_fail "отметка tg_take не вернулась за 100 с — повтора захвата нет"
t_retry=$(( $(date +%s) - t_rm ))
[ "$t_retry" -le $((RETRY * 14 / 10 + 10)) ] || pl_fail "повтор захвата через $t_retry с — дольше $RETRY с + 40 % и такта"
# отметку такт пишет до запуска юнита гостя (systemctl start — до 60 с): строка журнала — после неё
n_up=$n_up0
for _ in $(seq 1 60); do n_up=$(grep -cE "$tg_up_re" "$PL/B/node.log" 2>/dev/null); [ "${n_up:-0}" -gt "$n_up0" ] && break; sleep 1; done
[ "${n_up:-0}" -gt "$n_up0" ] \
    || pl_fail "в журнале B нет новой попытки поднять юнит гостя при повторе захвата (60 с после отметки; строк до снятия отметки $n_up0, сейчас ${n_up:-0})"

# ── 4. макет открыт — гость берёт Telegram ───────────────────────────────────────────────────────
tg_block off
T0=$(date +%s)
wait_t B 's["in"].get("state") == "serving" and s["in"].get("tg_taken") and not s["in"].get("tg_wait")' 120 \
    "B: гость взял Telegram (tg_taken), причина снята" || exit 1
t_taken=$WT
[ -n "$(hook_url)" ] || pl_fail "B считает tg_taken, а вебхука бота основы у макета нет"
ip netns exec awg-guest ip link show awg0 >/dev/null 2>&1 || pl_fail "клон awg0 пропал после захвата Telegram"
card="$(pl_cmd B card)"
case "$card" in *"здесь не отвечает в Telegram"*) pl_fail "строка о боте без Telegram осталась в карточке B: ${card:0:600}" ;; esac
note "1–4: без бота через ${t_wait} с после старта гостя, повтор захвата через ${t_retry} с, Telegram взят через ${t_taken} с после снятия блокировки"
pl_cleanup_quiet

# ── 5. переезд кнопкой, A гибнет между up и released ─────────────────────────────────────────────
# порядок сроков — как на хостах: гость берёт Telegram принудительно (DONE_BUTTON_WAIT_S у B) раньше, чем у A
# истекает шаг записи (RECORD_WAIT_S) — A оживает внутри шага и уходит в away по его сроку
export LAB_OPEN_RECORD="" LAB_CONST="$INB:TAKEN_WAIT_S=60,DONE_BUTTON_WAIT_S=90;$OUTB:RECORD_WAIT_S=120"
pl_pair_up
nsenter --net=/run/netns/lab-B bash "$PL_Z" --shell >/dev/null || pl_fail "оболочка зоны в lab-B не поднялась (кнопка)"
: > "$PL/shared/auth.hold"                     # факт записи не подтвердится — A стоит на шаге record
res="$(pl_cmd A move '{}')" || pl_fail "переезд кнопкой не начат: $res"
pl_wait A 's["out"].get("state") != "leaving" or s["out"].get("step") != "pkg"' 150 "A вышла из шага pkg" || exit 1
st="$(pl_open_wait 150)"
case "$st" in *'"step": "done"'*) ;; *) pl_fail "процесс открытия при переезде не дошёл до done: $st" ;; esac
pl_guest_start
pl_wait A 's["out"].get("step") == "record" and s["out"].get("record_set_at")' 60 "A на шаге записи, запись сменена" || exit 1
[ "$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$PL/shared/dns.json" "$RECORD")" = 198.51.100.7 ] \
    || pl_fail "A на шаге записи, а запись не на B: $(cat "$PL/shared/dns.json")"
pl_stop A; bash "$PL_HERE/net.sh" kill A >/dev/null
T0=$(date +%s)
wait_t B 's["in"].get("state") == "serving" and s["in"].get("tg_taken") and s["in"].get("tg_take_forced")' 300 \
    "B: Telegram у гостя по принудительному захвату (основа не отдала released)" || exit 1
t_forced=$WT
[ "$(pl_q B 's["in_tg"]')" = forced ] || pl_fail "st.in_tg у B: $(pl_q B 's["in_tg"]'), ожидалось forced"
pl_wait G 's.get("role") == "holder" and s.get("holder") == "self"' 120 "гость — держатель эпохи после takeover" || exit 1
[ -n "$(hook_url)" ] || pl_fail "гость держатель, а вебхука бота основы у макета нет"

# ── 6. A оживает — away без отката записи, возврат ───────────────────────────────────────────────
bash "$PL_HERE/net.sh" heal >/dev/null
pl_start A --poll
T0=$(date +%s)
wait_t A 's["out"].get("state") == "away"' 420 "ожившая A по сроку шага ушла в away (бот у гостя принудительно)" || exit 1
t_away=$WT
grep -q "по принудительному захвату" "$PL/A/node.log" || pl_fail "A ушла в away не путём «бот у гостя принудительно»: $(pl_q A 's["out"]')"
[ "$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$PL/shared/dns.json" "$RECORD")" = 198.51.100.7 ] \
    || pl_fail "ожившая A откатила запись: $(cat "$PL/shared/dns.json")"
[ "$(pl_q B 's["in"].get("state")')" = serving ] || pl_fail "B после оживления A не serving: $(pl_q B 's["in"]')"
kill -0 "$PL_GUEST_PID" 2>/dev/null || pl_fail "бот основы на резерве снят после оживления A: $(tail -n5 "$PL/G/guest.log")"
grep -q "back от" "$PL/B/node.log" && note "6: B получил back от A — ответил taken"
rm -f "$PL/shared/auth.hold"
pl_wait A 's.get("alive")' 90 "B на связи для кнопки возврата" || exit 1
res="$(pl_cmd A return '{}')" || pl_fail "возврат не начат: $res"
case "$res" in *'"acts": []'*) pl_fail "возврат кнопкой ничего не начал: $res · $(pl_q A 's["out"]')" ;; esac
dns_set 203.0.113.5
T0=$(date +%s)
wait_t A 's["out"].get("state") == "home" and s["jrn"]["role"] == "holder" and s["jrn"]["holder"] == "self"' 300 \
    "возврат: пользователи и эпоха снова у A" || exit 1
note "5–6: Telegram у гостя принудительно через ${t_forced} с после гибели A, A в away через ${t_away} с после оживления, возврат за ${WT} с"
echo "ok (${SUMMARY#; })"
