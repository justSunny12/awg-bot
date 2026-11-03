#!/bin/bash
# s11_c9_wait — отметка С9 без рычага на журнальном пути (tests/lab/pairlab.sh): переезд кнопкой
# сорвался после передачи бота (released ушёл, гость взял эпоху, а Telegram не взял) — основа
# дома снова берёт эпоху без released гостя и ждёт его правок хвостом С9.
#
# Проверяет:
#   1. переезд кнопкой, гостю закрыт макет Bot API (правило в пространстве зоны):
#      A передаёт бота (released), гость — держатель эпохи; резерв выключается (бот B и гость
#      остановлены, сеть B убита); A по сроку шага released (сжат до 60 с) и слову Telegram «вебхука
#      нет» — срыв, дома держатель; отметка С9 — под эпоху гостя;
#   2. данные карточки (reserve_c9_wait_view): ожидание без срока — в карточке «└ ⏳ жду события с
#      «Beta»» без ссылки; после C9_WAIT_S (сжат до 90 с) — со ссылкой «(не ждать?)»;
#   3. «🗑 Не ждать» (reserve_c9_drop): отметки нет, строка журнала A «отметка С9 снята админом»,
#      админу A — итог «Изменений от бота на «Beta» больше не жду»; повтор — отметки уже нет;
#   4. новая пара, тот же срыв, но резерв жив за разорванным линком пары (гость жив, Telegram ему
#      закрыт): отметка С9 под эпоху гостя; гость правит профиль после взятия эпохи основой; линк
#      вернулся — правка гостя приходит хвостом С9 (rsv_c9: перенесено ≥ 1, upto дальше F),
#      профиль — в базе A.
#
# Не покрыто здесь: замена отметки прошлого эпизода эпохой второго гостя (два эпизода подряд без
# released) — между эпизодами нужен свежий пакет у резерва без гостя, а живой резерв без гостя
# снимает отметку фактом (reserve_c9_settle) раньше, чем откроется второй гость; путь —
# tests/integration/test_reserve_c9_epoch.py.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/pairlab.sh"
. "$HERE/quolab.sh"
trap pl_cleanup EXIT
C9W=awgbot.domain.services.reserve.journal.c9wait
OUTB=awgbot.domain.services.reserve.outbound

tg_block() {     # гостю закрыт макет Bot API (выход из зоны)
    ip netns exec awg-guest nft delete table ip labtg 2>/dev/null
    ip netns exec awg-guest nft -f - <<EOF
table ip labtg {
    chain out { type filter hook output priority -10; policy accept; ip daddr ${PL_GW[B]} tcp dport 8081 reject with tcp reset; }
}
EOF
}
sql() {
    "$PY" -c 'import sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True)
r=c.execute(sys.argv[2]).fetchone(); print("" if r is None else r[0])' "$1" "$2"
}
released_to_guest() {   # переезд кнопкой до released: гость — держатель, Telegram ему закрыт
    nsenter --net=/run/netns/lab-B bash "$PL_Z" --shell >/dev/null || pl_fail "оболочка зоны в lab-B не поднялась"
    tg_block || pl_fail "правило зоны на макет Bot API не встало"
    local res st
    rm -f /run/awg-bot/rsv/open.status                 # ход прошлого открытия не сойдёт за это
    res="$(pl_cmd A move '{}')" || pl_fail "переезд кнопкой не начат: $res"
    pl_wait A 's["out"].get("state") != "leaving" or s["out"].get("step") != "pkg"' 150 "A вышла из шага pkg" || exit 1
    st="$(pl_open_wait 150)"
    case "$st" in *'"step": "done"'*) ;; *) pl_fail "процесс открытия при переезде не дошёл до done: $st" ;; esac
    pl_guest_start
    pl_wait A 's["out"].get("step") in ("record", "release", "released")' 150 "A на шаге записи" || exit 1
    dns_set 198.51.100.7
    pl_wait A 's["out"].get("step") == "released" and s["jrn"]["role"] == "follower"' 150 "A передала бота (released) и следует" || exit 1
    pl_wait G 's.get("role") == "holder" and s.get("holder") == "self"' 180 "гость — держатель эпохи по released" || exit 1
    GE="$(pl_q G 's["epoch"]')"
}
fail_home() {   # срыв после released: A дома держатель, отметка С9 под эпоху гостя
    T0=$(date +%s)
    wait_t A 's["out"].get("state") == "home" and s["jrn"]["role"] == "holder" and s["jrn"]["holder"] == "self"' 600 \
        "A: срыв переезда после released, дома держатель" || exit 1
    T_HOME=$WT
    pl_wait A "(s.get('c9') or {}).get('e') == $GE" 30 "отметка С9 под эпоху гостя $GE" || exit 1
}

# ── 1. срыв после передачи бота, резерв выключен ─────────────────────────────────────────────────
export LAB_CONST="$OUTB:RELEASED_WAIT_S=30,RELEASED_WAKE_S=30;awgbot.domain.services.reserve.journal.handover:RELEASED_WAIT_S=30;$C9W:C9_WAIT_S=45"
pl_pair_up
res="$(pl_cmd A load '{"n": 3, "prefix": "До-переезда", "pause": 0.05}' 60)" || pl_fail "правки A: $res"
pl_wait A 's["jrn"]["acked"] == s["last_i"]' 60 "правки A подтверждены" || exit 1
released_to_guest
pl_guest_stop; pl_stop B; bash "$PL_HERE/net.sh" kill B >/dev/null
fail_home
grep -q "резерв не взял право отвечать" "$PL/A/node.log" || pl_fail "срыв A не по слову Telegram «вебхука нет»: $(pl_q A 's["out"]')"

# ── 2. плашка карточки: ожидание, затем ссылка ───────────────────────────────────────────────────
[ "$(pl_q A 'bool(s["c9_view"]) and not s["c9_view"]["overdue"] and not s["c9_view"]["online"]')" = true ] \
    || pl_fail "данные плашки С9 до срока: $(pl_q A 's["c9_view"]')"
card="$(pl_cmd A card)" || pl_fail "карточка A не собрана: $card"
case "$card" in *"жду события с «Beta»"*) ;; *) pl_fail "в карточке A нет плашки «жду события»: ${card:0:700}" ;; esac
case "$card" in *"не ждать?"*) pl_fail "ссылка «не ждать?» до срока C9_WAIT_S: ${card:0:700}" ;; esac
T0=$(date +%s)
wait_t A 's["c9_view"].get("overdue") is True' 150 "срок ожидания С9 вышел" || exit 1
t_over=$WT
card="$(pl_cmd A card)"
case "$card" in *"не ждать?"*) ;; *) pl_fail "после срока в карточке нет ссылки «не ждать?»: ${card:0:700}" ;; esac

# ── 3. «не ждать» ────────────────────────────────────────────────────────────────────────────────
t_drop=$(date +%s)
res="$(pl_cmd A c9drop)" || pl_fail "«не ждать» не выполнено: $res"
case "$res" in *'"dropped": true'*'"notes": 1'*) ;; *) pl_fail "«не ждать» не сняло отметку: $res" ;; esac
[ "$(pl_q A 'bool(s.get("c9"))')" = false ] || pl_fail "после «не ждать» отметка С9 на месте: $(pl_q A 's["c9"]')"
[ "$(pl_q A 'bool(s.get("c9_view"))')" = false ] || pl_fail "после «не ждать» плашка на месте: $(pl_q A 's["c9_view"]')"
grep -q "отметка С9 снята админом" "$PL/A/node.log" || pl_fail "в журнале A нет строки о снятии отметки админом"
for _ in $(seq 1 15); do [ "$(tg_count "$TOKEN_A" "$ADMIN_A" "больше не жду" "$t_drop")" -ge 1 ] && break; sleep 1; done
[ "$(tg_count "$TOKEN_A" "$ADMIN_A" "больше не жду" "$t_drop")" = 1 ] || pl_fail "админу A итог «не ждать»: $(tg_count "$TOKEN_A" "$ADMIN_A" "больше не жду" "$t_drop") сообщений"
res="$(pl_cmd A c9drop)"
case "$res" in *'"dropped": false'*) ;; *) pl_fail "повтор «не ждать»: $res" ;; esac
note "1–3: срыв дома через ${T_HOME} с, ссылка «не ждать?» через ${t_over} с после проверки плашки"
cp "$PL/A/node.log" /var/tmp/s11_c9_wait.A1.log 2>/dev/null   # журнал первой пары — для разбора (пара 4 — свежая)
pl_cleanup_quiet

# ── 4. резерв жив за разорванным линком: правки гостя — хвостом С9 ──────────────────────────────
pl_pair_up
released_to_guest
bash "$PL_HERE/net.sh" cut A B >/dev/null
fail_home
F="$(pl_q A 's["c9"]["F"]')"
printf '{"name": "Правка-гостя-С9"}' > "$PL/G/edit_one"
for _ in $(seq 1 40); do [ -e "$PL/G/edit_one.done" ] && break; sleep 0.25; done
grep -q '"id"' "$PL/G/edit_one.done" 2>/dev/null || pl_fail "гость (держатель за разрезом) правку не выполнил: $(cat "$PL/G/edit_one.done" 2>/dev/null)"
bash "$PL_HERE/net.sh" heal >/dev/null
T0=$(date +%s)
wait_t A "int((s.get('c9') or {}).get('moved') or 0) >= 1" 300 "правка гостя перенесена хвостом С9" || exit 1
t_tail=$WT
[ "$(pl_q A "int(s['c9']['upto']) > $F")" = true ] || pl_fail "upto отметки С9 не дальше F=$F: $(pl_q A 's["c9"]')"
[ "$(sql "$PL/A/data/bot.db" "SELECT count(*) FROM clients WHERE name = 'Правка-гостя-С9'")" = 1 ] \
    || pl_fail "правки гостя нет в базе A после хвоста С9"
note "4: правка гостя хвостом С9 через ${t_tail} с после восстановления линка"
echo "ok (${SUMMARY#; })"
