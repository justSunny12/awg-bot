#!/bin/bash
# s10_force_live_guest — рычаг «вернуть без резерва» и живой гость за разорванным
# линком пары (tests/lab/pairlab.sh): подтверждение рычага ещё раз спрашивает Telegram
# (getWebhookInfo макета, вебхук не снимается) и отказывает, пока бот на резерве отвечает; гость,
# увидевший в надзоре чужой вебхук, решений больше не пишет, админ резерва узнаёт об этом.
#
# Проверяет:
#   1. переезд кнопкой: гость — держатель эпохи, вебхук бота основы — у него; A в отъезде;
#   2. разорван только путь A ↔ B (линк пары), Telegram доставляет гостю: через FORCE_OFFLINE_S
#      (сжат до 60 с) первая проверка рычага проходит, а рычаг отказывает словом Telegram — «бот
#      на «Beta» отвечает в Telegram — верни пользователей обычным путём»; вебхук на месте;
#   3. гость остановлен (SIGSTOP) и доставка обновления ему сорвалась (ошибка
#      свежая, у макета last_error_date) — обе проверки рычага проходят (рычаг идёт);
#   4. гость снова работает (SIGCONT), линк по-прежнему разорван, рычаг — по-настоящему: A забирает
#      Telegram (вебхук снят, A поллит, запись — на A); гость за надзор (WATCH_SECONDS сжат до
#      30 с) видит чужой вебхук — ограждение базы (держатель self без права записи, jrn_fence),
#      правка в нём отвергнута; админу B — одно уведомление «Управление ботом «Alpha» вернулось на
#      основной сервер: гостевой экземпляр остановлен…» со звуком.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/pairlab.sh"
. "$HERE/quolab.sh"
trap 'kill -CONT "$PL_GUEST_PID" 2>/dev/null; pl_cleanup' EXIT

hook_info() {    # getWebhookInfo бота основы у макета (JSON result)
    ip netns exec lab-A curl -s -m5 "$API_A/bot$TOKEN_A/getWebhookInfo" | "$PY" -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["result"]))'
}
hook_q() { hook_info | "$PY" -c 'import json,sys; s=json.load(sys.stdin); print(eval(sys.argv[1], {"s": s}))' "$1"; }
loud() {         # loud <подстрока> <с ts> — сколько sendMessage бота B админу B со звуком
    "$PY" -c 'import json,sys
n = 0
for l in open(sys.argv[1], encoding="utf-8"):
    r = json.loads(l) if l.strip() else None
    if not r or r["method"] != "sendMessage" or r["bot"] != int(sys.argv[2]) or r["ts"] < float(sys.argv[5]):
        continue
    p = r["params"]
    if str(p.get("chat_id")) == sys.argv[3] and sys.argv[4] in str(p.get("text")) \
            and str(p.get("disable_notification")).lower() not in ("true", "1"):
        n += 1
print(n)' "$PL/botapi.jsonl" "${TOKEN_B%%:*}" "$ADMIN_B" "$1" "$2"
}
force() {        # force <json> — рычаг путём кнопки «Да» у A; на stdout — итог JSON
    pl_cmd A force "$1" 30
}

# ── 1. переезд кнопкой ───────────────────────────────────────────────────────────────────────────
WATCH=15                                     # надзор вебхука гостем (runtime/webhook.WATCH_SECONDS, на хостах 600)
export LAB_CONST="awgbot.domain.services.reserve.outbound_return:FORCE_OFFLINE_S=30;awgbot.runtime.webhook:WATCH_SECONDS=$WATCH"
pl_pair_up
nsenter --net=/run/netns/lab-B bash "$PL_Z" --shell >/dev/null || pl_fail "оболочка зоны в lab-B не поднялась"
q_move_button
url="$(hook_q 's["url"]')"
[ -n "$url" ] || pl_fail "после переезда у макета нет вебхука бота основы"

# ── 2. разорван линк пары, гость отвечает — рычаг отказывает ─────────────────────────────────────
bash "$PL_HERE/net.sh" cut A B >/dev/null
T0=$(date +%s)
for _ in $(seq 1 240); do
    r="$(force '{"dry": true}')"
    case "$r" in *'"stage": "check"'*) sleep 1 ;; *) break ;; esac
done
WT=$(since "$T0")
case "$r" in *'"stage": "check"'*) pl_fail "рычаг невозможен и через 240 с после разреза: $r" ;; esac
res="$(force '{}')"
case "$res" in *'"stage": "webhook"'*) ;; *) pl_fail "рычаг при живом госте не отказал словом Telegram: $res" ;; esac
case "$res" in *"отвечает в Telegram — верни пользователей обычным путём"*) ;; *) pl_fail "отказ рычага не тот: $res" ;; esac
[ "$(hook_q 's["url"]')" = "$url" ] || pl_fail "отказ рычага снял вебхук гостя: $(hook_info)"
[ "$(pl_q A 's["out"].get("state")')" = away ] || pl_fail "после отказа рычага A не в away: $(pl_q A 's["out"]')"
note "2: рычаг возможен через ${WT} с после разреза, отказ словом Telegram"

# ── 3. гость остановлен, доставка сорвалась — рычаг идёт ─────────────────────────────────────────
kill -STOP "$PL_GUEST_PID"
ip netns exec lab-A curl -s -m5 -X POST "$API_A/inject/$TOKEN_A" -H 'Content-Type: application/json' -d \
    '{"message": {"message_id": 9, "date": 1, "chat": {"id": 880555, "type": "private"}, "from": {"id": 880555, "is_bot": false, "first_name": "Гость"}, "text": "/start"}}' >/dev/null
for _ in $(seq 1 60); do [ "$(hook_q 'bool(s.get("last_error_date"))')" = True ] && break; sleep 1; done
[ "$(hook_q 'bool(s.get("last_error_date"))')" = True ] || pl_fail "доставка остановленному гостю не дала ошибки у макета: $(hook_info) · макет: $(tail -n5 "$PL/botapi.log") · $(ip netns exec lab-inet ss -tn 2>/dev/null | grep -c ':443')"
res="$(force '{"dry": true}')"
case "$res" in *'"go": true'*) ;; *) pl_fail "гость остановлен, ошибка доставки свежая — а рычаг отказывает: $res" ;; esac
kill -CONT "$PL_GUEST_PID"

# ── 4. рычаг при живом госте за разорванным линком: ограждение и уведомление админу B ───────────
sleep 5
t_lever=$(date +%s)
res="$(force '{}')"
case "$res" in *'"go": true'*) ;; *) pl_fail "рычаг (ошибка доставки свежая) не пошёл: $res" ;; esac
T0=$(date +%s)
for _ in $(seq 1 60); do [ -z "$(hook_q 's["url"]')" ] && break; sleep 1; done
[ -z "$(hook_q 's["url"]')" ] || pl_fail "рычаг прошёл, а вебхук гостя не снят: $(hook_info)"
pl_wait A '(s.get("poll") or {}).get("state") == "mine"' 60 "A забрала Telegram (поллит)" || exit 1
wait_t G 's.get("holder") == "self" and s.get("role") == "follower"' 120 \
    "гость за надзор увидел чужой вебхук — ограждение базы (держатель без права записи)" || exit 1
t_fence=$WT
grep -q "вебхук указывает не сюда" "$PL/G/guest.log" || pl_fail "в журнале гостя нет строки о чужом вебхуке"
rm -f "$PL/G/edit_one.done"
printf '{"name": "После-рычага"}' > "$PL/G/edit_one"
for _ in $(seq 1 40); do [ -e "$PL/G/edit_one.done" ] && break; sleep 0.25; done
grep -q '"error"' "$PL/G/edit_one.done" 2>/dev/null || pl_fail "огражденный гость правку принял: $(cat "$PL/G/edit_one.done" 2>/dev/null)"
for _ in $(seq 1 60); do [ "$(tg_count "$TOKEN_B" "$ADMIN_B" "вернулось на основной сервер" "$t_lever")" -ge 1 ] && break; sleep 1; done
n="$(tg_count "$TOKEN_B" "$ADMIN_B" "вернулось на основной сервер" "$t_lever")"
[ "$n" = 1 ] || pl_fail "админу B уведомлений о чужом вебхуке: $n, ожидалось одно"
[ "$(loud "вернулось на основной сервер" "$t_lever")" = 1 ] || pl_fail "уведомление о чужом вебхуке без звука"
sleep $((WATCH + 15))                                        # следующий надзор гостя — повтор слова, не уведомления
n="$(tg_count "$TOKEN_B" "$ADMIN_B" "вернулось на основной сервер" "$t_lever")"
[ "$n" = 1 ] || pl_fail "уведомление о чужом вебхуке повторилось: $n"
note "4: гость огражден через ${t_fence} с после рычага, уведомление админу B одно"
echo "ok (${SUMMARY#; })"
