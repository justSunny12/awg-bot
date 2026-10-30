#!/bin/bash
# s06_quorum_move — решение кворума резерва на паре настоящих ботов стенда (tests/lab/pairlab.sh):
# правило лидера (лишение кворума, старт, qpause), правило кандидата, исполнение (по точкам, по
# просьбе ask{q} → go). Голосующие — второй сервер B (его зонд к awg0 A в awg-quo-B) и шлюзы основы:
# настоящий голосующий агента (tests/lab/gwnode.py) в lab-G1 и lab-G2, пробы — через NAT стенда к
# порту AWG основы; слова гостевого канала и канала конфигурации — шиной стенда (tests/lab/labbus),
# доставка — пока ping до сервера проходит. Строк слотов в базе основы нет (линков слотов стенд не
# поднимает): состав линии и снимки слотов — из шлюзов-заглушек (pairnode, LAB_GW_SLOTS).
#
# Возврат пользователей — кнопкой (reserve.return: button), иначе возврат «сразу» вмешался бы в замеры.
#
# Проверяет (каждый пункт — на своей свежей паре; выбрать пункты — LAB_S06="a3 a2 b v g e z d",
# латиницей: а3, а2, б, в, г, е и ж, з, д):
#   включение: кворум линии A включается (состав a, b, a1[, a2], ключи подтверждены B и шлюзами),
#      A — лидер, все голосующие видят A;
#   а3, а2. A выключена (процесс остановлен, сеть убита) — B забирает по точкам шлюзов: от гибели до
#      начала открытия ≤ 170 с, при v = 3 и при v = 2; процесс открытия — done, эпоха гостя — A + 1;
#   б. A отрезана, но жива (net.sh kill A; Bot API ей виден): лишена кворума раньше, чем B начал
#      открытие; правки A после лишения кворума отвергает база (сторож), последняя принятая — не
#      позже 65 с после разреза (хвост ≤ W + T_down + такт);
#   в. A жива, линк пары жив, порт AWG A закрыт для всех голосующих: A лишена кворума, B забирает
#      по просьбе — ask{q} → A отдаёт сама путём кнопки (переезд «dead» у A), ни точек, ни кода;
#      переезд доходит до отъезда, хвоста у A нет;
#   г. пробы B до A не доходят, шлюзы видят: 4 мин ничего не происходит — A лидер, B не открывает,
#      окна кода нет;
#   е. перезапуск бота A с qpause (слово второму серверу и шлюзам) и простоем 150 с — переезда нет;
#      без qpause и простоем — переезд;
#   ж. старт лидера после перезапуска: без права записи (держатель self, роль follower) с первых
#      секунд — правка через 2 с после старта отвергнута (сейчас принимается: ограждение ставит первый
#      такт кворума; провал — в конце сценария), правка на старте отвергнута, пользователю — Т-К9 в
#      макете Bot API; первое большинство «да» — снова лидер;
#   з. возврат кнопкой при включённом кворуме: переезд кнопкой, правка бота основы на резерве — в
#      базе A; возврат — A держатель и снова лидер, гость передал право записи (released) и
#      остановился — к записи по «да» ему вернуться не из чего;
#   д. случай 3 (шлюзам закрыт порт AWG A, B видит): A лишена кворума не раньше 780 с (T_down_rf
#      820 с), B забирает по просьбе не раньше 880 с (T_up_rf 900 с); только при LAB_LONG=1 (≈ 20 мин).
#
# Не покрыто здесь: Т-К9 на кнопку (всплывашка) — только сообщение; уведомления Т-К7/Т-К8 и
# ничья — юнит-тесты и s07_quorum_tie.
#
# Дефекты кода, найденные стендом (quolab.sh: quo_defects — проверка за секунды, без обхода сценарий
# падает сразу): порог `t` в слове `qkeys` затирается видом сообщения конверта (кворум не включается;
# обход LAB_QKEYS_SHIM=1) и пакет nftguard без `TG_OUT4`/`SET_TG_OUT` (awg_bot_guard с awggx не
# собирается, у зоны резерва нет выхода наружу; обход LAB_GUARD_SHIM=1).
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/pairlab.sh"
. "$HERE/quolab.sh"
trap pl_cleanup EXIT
PARTS="${LAB_S06:-a3 a2 b v g e z}"
[ "${LAB_LONG:-}" = 1 ] && PARTS="$PARTS d"
has() { case " $PARTS " in *" $1 "*) return 0 ;; esac; return 1; }

quo_defects                                   # без обхода стенда — провал с причиной сразу

# ── а. A выключена — по точкам ────────────────────────────────────────────────────────────────────
for v in 3 2; do
    has "a$v" || continue
    [ "$v" = 3 ] && q_up 1,2 || q_up 1
    e_a="$(pl_q A 's["jrn"]["epoch"]')"
    T0=$(date +%s)
    pl_stop A; bash "$PL_HERE/net.sh" kill A >/dev/null
    take_check "B начал открытие после гибели A (v = $v)" 240
    t_take=$WT
    [ "$t_take" -le 170 ] || pl_fail "v = $v: B начал открытие через $t_take с после гибели A — дольше 170 с"
    [ "$(pl_q B 's["quo"]["moved_by"] and s["quo"]["moved_by"].get("quorum")')" = "true" ] \
        || pl_fail "v = $v: открытие не по точкам кворума: $(pl_q B 's["quo"]["moved_by"]') · $(pl_q B 's["quo"]["exec"]')"
    guest_epoch_check "$e_a"
    note "а v=$v: открытие через ${t_take} с"
    pl_cleanup_quiet
done

# ── б. A отрезана, но жива ───────────────────────────────────────────────────────────────────────
if has b; then
    q_up 1,2
    ip netns exec lab-A curl -s -m3 -o /dev/null "$API_A/journal/x" || pl_fail "макет Bot API недоступен A"
    : > "$PL/edits.log"
    ( i=0; while :; do i=$((i + 1)); r="$(pl_cmd A load "{\"n\": 1, \"prefix\": \"Хвост-$i\", \"base\": $((i % 200))}" 10)"
        printf '%s %s\n' "$(date +%s)" "$r" >> "$PL/edits.log"; sleep 2; done ) & EDITS=$!
    sleep 6
    T0=$(date +%s)
    bash "$PL_HERE/net.sh" kill A >/dev/null
    wait_t A 's["quo"]["fenced"] is True and s["jrn"]["role"] == "follower" and s["jrn"]["holder"] == "self"' 120 \
        "A лишена кворума (держатель self, роль follower)" || exit 1
    t_fence=$WT
    take_check "B начал открытие после разреза A" 240
    t_take=$WT
    sleep 6; kill "$EDITS" 2>/dev/null; wait "$EDITS" 2>/dev/null
    [ "$t_fence" -lt "$t_take" ] || pl_fail "A лишена кворума через $t_fence с, а B начал открытие раньше — через $t_take с"
    last_ok="$(grep -v '"error"' "$PL/edits.log" | grep '"made"' | tail -n1 | cut -d' ' -f1)"
    first_err="$(grep '"error"' "$PL/edits.log" | head -n1)"
    [ -n "$last_ok" ] || pl_fail "ни одной принятой правки A — нагрузка не шла: $(head -n3 "$PL/edits.log")"
    [ -n "$first_err" ] || pl_fail "правки A после лишения кворума не отвергались: $(tail -n3 "$PL/edits.log")"
    tail_s=$(( last_ok - T0 ))
    [ "$tail_s" -le 65 ] || pl_fail "последняя правка A принята через $tail_s с после разреза — хвост длиннее 65 с"
    late="$(awk -v t=$((T0 + t_fence + 1)) '$1 > t && $0 !~ /"error"/ && $0 ~ /"made"/' "$PL/edits.log" | head -n1)"
    [ -z "$late" ] || pl_fail "правка A принята после лишения кворума: $late"
    asked="$(grep -c 'не ответил на просьбу' "$PL/B/node.log")"        # линк пары лёг, а сессия ещё «на связи» — ask и ожидание
    note "б: лишена кворума через ${t_fence} с, открытие B через ${t_take} с (просьба без ответа: $asked), последняя правка A через ${tail_s} с"
    pl_cleanup_quiet
fi

# ── в. линк пары жив, порт AWG A закрыт всем голосующим — по просьбе ─────────────────────────────
if has v; then
    q_up 1,2
    e_a="$(pl_q A 's["jrn"]["epoch"]')"
    T0=$(date +%s)
    fault "ip daddr 203.0.113.5 udp dport 45871 drop comment \"awg A закрыт\""
    wait_t A 's["quo"]["fenced"] is True' 150 "A лишена кворума" || exit 1
    t_fence=$WT
    wait_t A 's["out"].get("state") == "leaving"' 240 "A начала переезд по просьбе кворума (ask{q} → go)" || exit 1
    t_ask=$WT
    [ "$(pl_q A 's["out"].get("kind")')" = "dead" ] || pl_fail "вид переезда у A: $(pl_q A 's["out"]')"
    [ "$(pl_q A 's["online"]')" = "true" ] || pl_fail "линк пары лёг — проверка пути «по просьбе» пуста"
    [ "$(pl_q B 's["quo"]["moved_by"]')" = "null" ] || pl_fail "B открывал по точкам при живом линке пары: $(pl_q B 's["quo"]["moved_by"]')"
    [ "$(pl_q B '(s.get("code_window") or {}).get("open")')" != "true" ] || pl_fail "у B открыто окно кода при пути «по просьбе»"
    # дальше — путь кнопки: свежий снимок, открытие, гость, запись (меняет сценарий)
    pl_wait A 's["out"].get("state") != "leaving" or s["out"].get("step") != "pkg"' 150 "A вышла из шага pkg" || exit 1
    st="$(pl_open_wait 150)"
    case "$st" in *'"step": "done"'*) ;; *) pl_fail "процесс открытия по просьбе не дошёл до done: $st" ;; esac
    pl_guest_start
    pl_wait A 's["out"].get("step") in ("record", "released") or s["out"].get("state") == "away"' 150 "A на шаге записи" || exit 1
    "$PY" -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d[sys.argv[2]]="198.51.100.7"; json.dump(d, open(p, "w"))' \
        "$PL/shared/dns.json" "$RECORD"
    pl_wait A 's["out"].get("state") == "away" and s["jrn"]["role"] == "follower" and s["jrn"]["holder"] == "peer"' 240 \
        "A в отъезде и следует (эпоху отдала сама)" || exit 1
    pl_wait G 's.get("role") == "holder" and s.get("holder") == "self"' 60 "гость — держатель эпохи" || exit 1
    tl="$(pl_q A 'sum((s.get("tail") or {}).values()) if isinstance(s.get("tail"), dict) else s.get("tail")')"
    [ "$tl" = 0 ] || pl_fail "у A хвост после передачи эпохи по просьбе: $(pl_q A 's["tail"]')"
    note "в: лишена кворума через ${t_fence} с, просьба принята через ${t_ask} с, хвоста нет"
    pl_cleanup_quiet
fi

# ── г. пробы B не доходят, шлюзы видят — ничего ─────────────────────────────────────────────────
if has g; then
    q_up 1,2
    T0=$(date +%s)
    fault "ip saddr 198.51.100.7 ip daddr 203.0.113.5 udp dport 45871 drop comment \"B не видит A\""
    wait_t B 's["quo"]["cand"].get("own") == "uns"' 90 "B: свой взгляд на A — «не вижу»" || exit 1
    t_uns=$WT
    for _ in $(seq 1 240); do
        [ "$(pl_q B 's["in"].get("state", "idle")')" = "idle" ] || pl_fail "B открыл при видящих шлюзах: $(pl_q B 's["in"]')"
        [ "$(pl_q A 's["quo"]["fenced"]')" = "false" ] || pl_fail "A лишена кворума при «да» шлюзов: $(pl_q A 's["quo"]["count"]')"
        [ "$(pl_q B '(s.get("code_window") or {}).get("open")')" != "true" ] || pl_fail "у B окно кода при видящих шлюзах"
        sleep 1
    done
    [ "$(pl_q B 's["quo"]["cand"].get("no")')" -le 1 ] || pl_fail "у B «нет» больше одного: $(pl_q B 's["quo"]["cand"]')"
    note "г: B «не вижу» через ${t_uns} с, 4 мин без переезда"
    pl_cleanup_quiet
fi

# ── е, ж. перезапуск с qpause, старт лидера, без qpause ──────────────────────────────────────────
if has e; then
    q_up 1,2
    res="$(pl_cmd A qpause '{"seconds": 300}')" || pl_fail "qpause: $res"
    case "$res" in *'send:qpause'*'gwlink:1'*'gwlink:2'*) ;; *) pl_fail "qpause ушла не всем: $res" ;; esac
    pl_stop A
    T0=$(date +%s)
    for _ in $(seq 1 150); do
        [ "$(pl_q B 's["in"].get("state", "idle")')" = "idle" ] || pl_fail "B открыл в плановой паузе A: $(pl_q B 's["in"]')"
        sleep 1
    done
    for s in 1 2; do
        case "$(pl_gq "$s" 's["got"]')" in *'in:link:qpause'*) ;; *) pl_fail "шлюз $s не получил qpause: $(pl_gq "$s" 's["got"]')" ;; esac
    done
    pl_start A --poll
    early="$(pl_cmd A load '{"n": 1, "prefix": "Сразу-после-старта", "base": 50}' 10)"
    T0=$(date +%s)
    wait_t A 's["jrn"]["role"] == "follower" and s["jrn"]["holder"] == "self"' 20 "ж: A на старте без права записи" || exit 1
    res="$(pl_cmd A load '{"n": 1, "prefix": "На-старте", "base": 60}' 10)"
    case "$res" in *'"error"'*) ;; *) pl_fail "ж: правка A на старте (без большинства «да») принята: $res" ;; esac
    ip netns exec lab-A curl -s -m5 -X POST "$API_A/inject/$TOKEN_A" -H 'Content-Type: application/json' -d \
        '{"message": {"message_id": 5, "date": 1, "chat": {"id": 880555, "type": "private"}, "from": {"id": 880555, "is_bot": false, "first_name": "Гость"}, "text": "/start"}}' >/dev/null
    tk9="$(cd "$PL_ROOT" && "$PY" - "$PL/botapi.jsonl" "${TOKEN_A%%:*}" <<'PYEOF'
import json, sys, time
from awgbot.bot.texts.reserve import quorum as qt
path, bot_a = sys.argv[1], int(sys.argv[2])
for _ in range(20):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    got = [r for r in rows if r["method"] == "sendMessage" and r["bot"] == bot_a and str(r["params"].get("chat_id")) == "880555"]
    if got:
        break
    time.sleep(0.5)
print("|".join(str(r["params"].get("text")) for r in got) if got else "нет", end="")
print("" if got and all(str(r["params"].get("text")) == qt.USER_FENCED for r in got) else " ≠ Т-К9", end="")
PYEOF
)"
    case "$tk9" in *"≠ Т-К9"*) pl_fail "ж: пользователю на старте лидера не Т-К9: $tk9" ;; esac
    wait_t A 's["jrn"]["role"] == "holder" and s["quo"]["count"].get("role") == "lead"' 90 "ж: A снова лидер по большинству «да»" || exit 1
    t_lead=$WT
    res="$(pl_cmd A load '{"n": 1, "prefix": "После-старта", "base": 70}' 10)"
    case "$res" in *'"error"'*) pl_fail "ж: правка A после возврата к лидерству отвергнута: $res" ;; esac
    case "$early" in
        *'"error"'*) early_s="отвергнута" ;;
        *) early_s="принята"                         # провал — в конце сценария, чтобы прошли остальные пункты
           DEFER="${DEFER:-}; ж: правка A через 2 с после старта принята — старт лидера без права записи начинается только с первого такта кворума: $early" ;;
    esac
    note "е: 150 с простоя с qpause — без переезда; ж: лидер через ${t_lead} с после старта, правка сразу после старта — $early_s"
    pl_wait B 's["quo"]["cand"].get("own") == "see"' 90 "B снова видит A" || exit 1
    pl_stop A
    T0=$(date +%s)
    take_check "без qpause: B начал открытие при простое A" 240
    note "е: без qpause — открытие через ${WT} с"
    pl_cleanup_quiet
fi

# ── з. возврат кнопкой при включённом кворуме ────────────────────────────────────────────────────
if has z; then
    q_up 1,2
    q_move_button
    printf '{"name": "Правка-гостя"}' > "$PL/G/edit_one"
    for _ in $(seq 1 40); do [ -e "$PL/G/edit_one.done" ] && break; sleep 0.25; done
    grep -q '"id"' "$PL/G/edit_one.done" 2>/dev/null || pl_fail "з: правка бота основы на резерве не выполнена: $(cat "$PL/G/edit_one.done" 2>/dev/null)"
    seen=""
    for _ in $(seq 1 60); do
        n="$("$PY" -c 'import sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True)
print(c.execute("SELECT count(*) FROM clients WHERE name = ?", ("Правка-гостя",)).fetchone()[0])' "$PL/A/data/bot.db")"
        [ "$n" = 1 ] && { seen=1; break; }
        sleep 0.25
    done
    [ -n "$seen" ] || pl_fail "з: правка бота основы на резерве не дошла до базы A за 15 с"
    T0=$(date +%s)
    res="$(pl_cmd A return '{}')" || pl_fail "з: возврат кнопкой не начат: $res"
    dns_set 203.0.113.5
    wait_t A 's["out"].get("state") == "home" and s["jrn"]["role"] == "holder" and s["jrn"]["holder"] == "self"' 300 \
        "з: пользователи и эпоха — снова у A" || exit 1
    t_back=$WT
    # гость передаёт право записи (released) и останавливается: к записи по «да» вернуться ему не из чего
    for _ in $(seq 1 30); do kill -0 "$PL_GUEST_PID" 2>/dev/null || break; sleep 1; done
    kill -0 "$PL_GUEST_PID" 2>/dev/null && pl_fail "з: бот основы на резерве жив через 30 с после возврата: $(tail -n3 "$PL/G/guest.log")"
    grep -q "передал право записи" "$PL/G/guest.log" || pl_fail "з: гость остановился без передачи права записи: $(tail -n5 "$PL/G/guest.log")"
    for _ in $(seq 1 60); do
        [ "$(pl_q A 's["jrn"]["holder"] == "self" and s["jrn"]["role"] == "holder"')" = "true" ] \
            || pl_fail "з: A потеряла эпоху после возврата: $(pl_q A 's["jrn"]')"
        sleep 1
    done
    [ "$(pl_q A 's["quo"]["count"].get("role")')" = "lead" ] || pl_fail "з: A после возврата не лидер: $(pl_q A 's["quo"]["count"]')"
    note "з: возврат кнопкой — A держатель через ${t_back} с, гость передал право записи и остановился"
    pl_cleanup_quiet
fi

# ── д. случай 3: шлюзам закрыт порт AWG A, B видит (LAB_LONG=1) ──────────────────────────────────
if has d; then
    q_up 1,2
    T0=$(date +%s)
    fault "ip saddr { 10.1.1.2, 10.1.2.2 } ip daddr 203.0.113.5 udp dport 45871 drop comment \"случай 3\""
    wait_t A 's["quo"]["fenced"] is True' 1000 "случай 3: A лишена кворума" || exit 1
    t_fence=$WT
    [ "$t_fence" -ge 780 ] || pl_fail "случай 3: A лишена кворума через $t_fence с — раньше T_down_rf (820 с)"
    wait_t A 's["out"].get("state") == "leaving"' 400 "случай 3: B забрал по просьбе" || exit 1
    t_ask=$WT
    [ "$t_ask" -ge 880 ] || pl_fail "случай 3: переезд через $t_ask с — раньше T_up_rf (900 с)"
    [ "$t_fence" -lt "$t_ask" ] || pl_fail "случай 3: лишение кворума ($t_fence с) не раньше переезда ($t_ask с)"
    note "д: лишена кворума через ${t_fence} с, просьба через ${t_ask} с"
    pl_cleanup_quiet
fi
[ -z "${DEFER:-}" ] || pl_fail "${DEFER#; } (прочее: ${SUMMARY#; })"
echo "ok (${SUMMARY#; })"
