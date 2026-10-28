#!/bin/bash
# s06_return_tail — хвост старой эпохи на паре серверов стенда (tests/lab/pairlab.sh): A отрезана
# от B и шлюзов, но макет Bot API ей доступен; B открывает по аварийному коду, бот основы на
# резерве ставит вебхук; A на 409 уходит в следование, её правки за эти секунды — хвост; после
# восстановления связи хвост переносится на резерв, спорная правка — в Т-Ж4 админу основы.
#
# Проверяет:
#   1. пара с журналом; профиль «Спорный» заведён на A и подтверждён резервом;
#   2. A отрезана от B и шлюзов (net.sh cut), Bot API ей виден; за это время на A — три новых
#      профиля и «Спорный»: устройств не больше 5 (неподтверждённые события);
#   3. у B — Н4 и окно аварийного кода; админ основы вводит код в боте B (кнопка «🔑 Ввести
#      аварийный код» и сообщение с кодом — через макет Bot API, обработчики бота B) — процесс
#      открытия доходит до done, бот основы на резерве — держатель эпохи A + 1;
#   4. бот основы на резерве меняет у «Спорного» устройств не больше на 7 и ставит вебхук; A на
#      409 своего getUpdates уходит в следование, поллинг стоит, её правки после подтверждённого —
#      в jrn_tail;
#   5. связь вернулась (net.sh heal): хвост A перенесён на резерв — три профиля в базе гостя,
#      у «Спорного» осталось 7 (значение держателя), хвост у A снят, у A — то же, что у гостя;
#   6. возврат кнопкой: бот основы на резерве отдаёт эпоху, A забирает Telegram; сразу за итогом
#      возврата A шлёт админу основы Т-Ж4 — одно сообщение со строкой про «Спорного»
#      (устройств не больше 5 — на «Beta» осталось 7); гость Т-Ж4 не шлёт.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/pairlab.sh"
trap pl_cleanup EXIT
GUEST_DB=/run/awg-bot-guest/data/bot.db
API_A="http://${PL_GW[A]}:8081"

inject_b() {     # inject_b <json обновления> — обновление боту B через макет (как от Telegram)
    ip netns exec lab-A curl -s -m5 -X POST "$API_A/inject/$TOKEN_B" -H 'Content-Type: application/json' -d "$1" >/dev/null
}

sql() {          # sql <база> <запрос> — одно значение
    "$PY" -c 'import sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True)
r=c.execute(sys.argv[2]).fetchone(); print("" if r is None else r[0])' "$1" "$2"
}

# ── 1. пара, «Спорный» подтверждён ───────────────────────────────────────────────────────────────
pl_pair_up
res="$(pl_cmd A load '{"n": 1, "prefix": "Спорный"}')" || pl_fail "профиль на A не заведён: $res"
pl_wait A 's["jrn"]["acked"] == s["last_i"]' 60 "правки A подтверждены" || exit 1
acked="$(pl_q A 's["jrn"]["acked"]')"
pl_cmd A code "{\"out\": \"$PL/shared/code\"}" >/dev/null || pl_fail "аварийный код A не получен"

# ── 2. A отрезана от B и шлюзов, правит дальше ───────────────────────────────────────────────────
for n in B G1 G2 GR; do bash "$PL_HERE/net.sh" cut A "$n" >/dev/null; done
ip netns exec lab-A curl -s -m3 -o /dev/null "$API_A/journal/x" || pl_fail "макет Bot API недоступен A — разрез задел лишнее"
res="$(pl_cmd A load '{"n": 3, "prefix": "Хвост", "pause": 0.2, "base": 50}')" || pl_fail "правки A после разреза: $res"
res="$(pl_cmd A set '{"name": "Спорный-1", "field": "device_limit", "value": 5}')" || pl_fail "правка «Спорного» на A: $res"
case "$res" in *'"error"'*) pl_fail "правка «Спорного» на A отвергнута: $res" ;; esac
sleep 2
[ "$(pl_q A 's["jrn"]["acked"]')" = "$acked" ] || pl_fail "правки после разреза подтверждены — разрез не сработал"

# ── 3. Н4 у B и аварийный код ────────────────────────────────────────────────────────────────────
pl_wait B '(s.get("code_window") or {}).get("open")' 150 "у B открыт приём аварийного кода (Н4)" || exit 1
nsenter --net=/run/netns/lab-B bash "$PL_Z" --shell >/dev/null || pl_fail "оболочка зоны в lab-B не поднялась"
cbdata="$(cd "$PL_ROOT" && "$PY" -c 'from awgbot.bot.callbacks import PeerCB; print(PeerCB(action="code").pack())')"
inject_b "{\"callback_query\": {\"id\": \"lab-1\", \"chat_instance\": \"lab\", \"data\": \"$cbdata\",
  \"from\": {\"id\": $ADMIN_A, \"is_bot\": false, \"first_name\": \"Админ A\"},
  \"message\": {\"message_id\": 7, \"date\": 1, \"chat\": {\"id\": $ADMIN_A, \"type\": \"private\"}, \"text\": \"роль\"}}}"
sleep 3
inject_b "{\"message\": {\"message_id\": 8, \"date\": 1, \"chat\": {\"id\": $ADMIN_A, \"type\": \"private\"},
  \"from\": {\"id\": $ADMIN_A, \"is_bot\": false, \"first_name\": \"Админ A\"}, \"text\": \"$(cat "$PL/shared/code")\"}}"
pl_wait B '(s.get("in") or {}).get("state") in ("taking", "serving")' 60 "B принял код и запустил открытие" || exit 1
st="$(pl_open_wait 150)"
case "$st" in *'"step": "done"'*) ;; *) pl_fail "процесс открытия по коду не дошёл до done: $st" ;; esac
pl_guest_start
# факт записи: на хостах запись меняет процесс открытия через провайдера и ждёт её у авторитетного
# сервера, затем кладёт отметку tg_take; у стенда нет ни провайдера, ни авторитетного DNS — запись
# меняет сценарий и кладёт ту же отметку (как процесс открытия после record_fact)
"$PY" -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d[sys.argv[2]]="198.51.100.7"; json.dump(d, open(p, "w"))' \
    "$PL/shared/dns.json" "$RECORD"
printf '1' > /run/awg-bot-guest/tg_take; chmod 600 /run/awg-bot-guest/tg_take
pl_wait G 's.get("role") == "holder" and s.get("holder") == "self"' 60 "бот основы на резерве — держатель" || exit 1
ae="$(sql "$PL/A/data/bot.db" 'SELECT epoch FROM jrn_meta')"
[ "$(pl_q G 's["epoch"]')" = "$((ae + 1))" ] || pl_fail "эпоха гостя $(pl_q G 's["epoch"]'), а у A — $ae (ожидалась A + 1)"
printf '{"name": "Спорный-1", "field": "device_limit", "value": 7}' > "$PL/G/set"
for _ in $(seq 1 20); do [ -e "$PL/G/set.done" ] && break; sleep 0.5; done
[ -e "$PL/G/set.done" ] || pl_fail "бот основы на резерве не выполнил правку «Спорного» за 10 с"
grep -q '"error"' "$PL/G/set.done" && pl_fail "правка «Спорного» на резерве отвергнута: $(cat "$PL/G/set.done")"

# ── 4. вебхук гостя → 409 у A → следование, хвост ────────────────────────────────────────────────
pl_wait A '(s.get("poll") or {}).get("state") == "away" and s["poll"].get("why") in ("webhook", "webhook_info")' 120 \
    "A получила 409 на getUpdates (вебхук у бота основы на резерве), поллинг стоит" || exit 1
# B для A не на связи (линк пары молчит, разрез): правило home — 409 после перехода домой, следование
pl_wait A 's["jrn"]["role"] == "follower"' 30 \
    "после 409 A следует: B не на связи (seen — down; флаг сессии в статусе — session)" || exit 1
pl_wait A 'sum((s.get("tail") or {}).values()) >= 4' 30 "неподтверждённые правки A — в хвосте" || exit 1

# ── 5. связь вернулась: хвост перенесён ──────────────────────────────────────────────────────────
bash "$PL_HERE/net.sh" heal >/dev/null
pl_wait A 'not (s.get("tail") or {})' 240 "хвост A перенесён и снят" || exit 1
for i in 1 2 3; do
    [ "$(sql "$GUEST_DB" "SELECT count(*) FROM clients WHERE name = 'Хвост-$i'")" = 1 ] || pl_fail "правка хвоста «Хвост-$i» не перенесена в базу гостя"
done
[ "$(sql "$GUEST_DB" "SELECT device_limit FROM clients WHERE name = 'Спорный-1'")" = 7 ] \
    || pl_fail "спорная правка перезаписала значение держателя: $(sql "$GUEST_DB" "SELECT device_limit FROM clients WHERE name = 'Спорный-1'")"
pl_wait A 's["jrn"]["applied"] >= 1' 30 "A применяет события держателя" || exit 1
for _ in $(seq 1 30); do
    [ "$(sql "$PL/A/data/bot.db" "SELECT device_limit FROM clients WHERE name = 'Спорный-1'")" = 7 ] \
        && [ "$(sql "$PL/A/data/bot.db" "SELECT count(*) FROM clients WHERE name LIKE 'Хвост-%'")" = 3 ] && break
    sleep 1
done
[ "$(sql "$PL/A/data/bot.db" "SELECT device_limit FROM clients WHERE name = 'Спорный-1'")" = 7 ] \
    || pl_fail "у A после переноса хвоста у «Спорного» не значение держателя"

# ── 6. возврат и Т-Ж4 ────────────────────────────────────────────────────────────────────────────
pl_wait A 's["out"].get("state") == "away"' 60 "A знает, что пользователи у B" || exit 1
res="$(pl_cmd A return '{}')" || pl_fail "возврат не начат: $res"
pl_wait A 's["out"].get("step") in ("record", "record_back") or s["out"].get("state") == "home"' 240 \
    "возврат дошёл до записи" || exit 1
"$PY" -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d[sys.argv[2]]="203.0.113.5"; json.dump(d, open(p, "w"))' \
    "$PL/shared/dns.json" "$RECORD"                       # запись обратно меняет человек
pl_wait A 's["out"].get("state") == "home" and s["jrn"]["role"] == "holder"' 300 "пользователи и эпоха — снова у A" || exit 1
out="$(cd "$PL_ROOT" && "$PY" - "$PL/botapi.jsonl" "${TOKEN_A%%:*}" "$ADMIN_A" <<'PYEOF'
import json, sys, time
path, bot_a, admin = sys.argv[1], int(sys.argv[2]), str(sys.argv[3])
for _ in range(30):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    tj4 = [r for r in rows if r["method"] == "sendMessage" and str(r["params"].get("chat_id")) == admin
           and "Не перенесен" in str(r["params"].get("text"))]
    if tj4:
        break
    time.sleep(1)
if len(tj4) != 1:
    print(f"Т-Ж4 админу основы: {len(tj4)} сообщений, ожидалось одно"); sys.exit(1)
r = tj4[0]
if r["bot"] != bot_a or r.get("from") != "203.0.113.5":
    # у бота основы и бота основы на резерве токен один — различает адрес отправителя
    print(f"Т-Ж4 прислал бот {r['bot']} с {r.get('from')}, а не бот основы с 203.0.113.5 (гость Т-Ж4 не шлёт)"); sys.exit(1)
text = str(r["params"]["text"])
if "Спорный-1" not in text or "устройств не больше 5" not in text or "осталось 7" not in text:
    print("в Т-Ж4 нет строки про «Спорного»: " + text[:400]); sys.exit(1)
if "Хвост-" in text:
    print("перенесённые правки попали в Т-Ж4 как спорные: " + text[:400]); sys.exit(1)
print("ok")
PYEOF
)" || pl_fail "$out"
echo ok
