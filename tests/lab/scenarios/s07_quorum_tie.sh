#!/bin/bash
# s07_quorum_tie — ничья и возвраты по кворуму резерва на паре настоящих ботов стенда
# (tests/lab/pairlab.sh, tests/lab/quolab.sh; шлюзы основы — tests/lab/gwnode.py).
#
# Проверяет (каждый пункт — на своей свежей паре; выбрать — LAB_S07="tie back q2"):
#   tie. v = 4 (шлюзы G1, G2, G3): B и Шлюз 1 не видят A (порт AWG A закрыт им), Шлюз 2 и Шлюз 3
#      видят — «да» 2 ≥ 2, «нет» 2 не > 2: A держится, B не забирает; дольше T_tie — по одному Т-К5
#      (бот A своему админу, поимённо) и Т-К5б (бот B своему админу; админ A бот B не открывал — строка
#      об этом), справка — со слов видящего шлюза; у B — окно кода (свой взгляд «не вижу»); после
#      починки и T_tie_clear без ничьей — по одному Т-К6 от каждого, повторов нет (пределы — от цифр
#      уровня, QT_* из pairlab.sh);
#   q2. возврат «сразу» (reserve.return: immediate, min_interval_minutes: 0): переезд кнопкой; в отъезде
#      голосующие видят A дома — Q2 «да» 3 из 3 не позже 120 с после отъезда, и дольше healthy_seconds — A
#      сама зовёт пользователей обратно. Ответчик дома открыт и на шагах передачи (leaving record/release/
#      released): шлюзам уже «away», их пробы домой получают ответ — без этого «не вижу» и обязательство
#      «нет» держали Q2 на 1 из 3 до срока обязательства;
#   back. аварийный возврат: A в отъезде, порт клона на B закрыт для A и шлюзов (бот основы на резерве
#      ни с кем не на связи) — A по Q1 о клоне забирает пользователей сама (rsv_out.quorum_return).
#
# Не покрыто: Т-К5в (админ A в роли второго сервера — нужен открытый им бот B: в макете Bot API
# только сообщение «/start» от него, проверяется в e2e); «гость после возврата сети следует» в back —
# бот основы на резерве на стенде без вебхука в настоящий Telegram; Н3 и корреляция — юнит-тесты.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/pairlab.sh"
. "$HERE/quolab.sh"
trap pl_cleanup EXIT
PARTS="${LAB_S07:-tie back q2}"
has() { case " $PARTS " in *" $1 "*) return 0 ;; esac; return 1; }

# ── ничья при v = 4 ──────────────────────────────────────────────────────────────────────────────
if has tie; then
    q_up 1,2,3
    ts0="$(date +%s)"
    T0=$ts0
    fault "ip saddr { 198.51.100.7, 10.1.1.2 } ip daddr 203.0.113.5 udp dport 45871 drop comment \"ничья\""
    wait_t B 's["quo"]["cand"].get("no") == 2 and s["quo"]["cand"].get("see") == 2' 120 "у B «нет» 2 и «да» 2" || exit 1
    t_tie0=$WT
    for _ in $(seq 1 300); do
        [ "$(pl_q B 's["in"].get("state", "idle")')" = "idle" ] || pl_fail "B забрал при ничьей: $(pl_q B 's["in"]')"
        [ "$(pl_q A 's["quo"]["fenced"]')" = "false" ] || pl_fail "A лишена кворума при «да» 2 из 4: $(pl_q A 's["quo"]["count"]')"
        [ "$(tg_count "$TOKEN_A" "$ADMIN_A" "🚨 Ничья о сервере" "$ts0")" -ge 1 ] && [ "$(tg_count "$TOKEN_B" "$ADMIN_B" "🚨 Ничья о сервере" "$ts0")" -ge 1 ] && break
        sleep 1
    done
    t_alarm=$(since "$T0")
    na="$(tg_count "$TOKEN_A" "$ADMIN_A" "🚨 Ничья о сервере" "$ts0")"; nb="$(tg_count "$TOKEN_B" "$ADMIN_B" "🚨 Ничья о сервере" "$ts0")"
    [ "$na" = 1 ] && [ "$nb" = 1 ] || pl_fail "тревога ничьей: от A $na, от B $nb (через $t_alarm с) — ожидалось по одной"
    [ $((t_alarm - t_tie0)) -ge $((QT_TIE - 10)) ] || pl_fail "тревога ничьей через $((t_alarm - t_tie0)) с после начала — раньше T_tie ($QT_TIE с)"
    [ "$(tg_count "$TOKEN_B" "$ADMIN_B" "ещё не открывал" "$ts0")" = 1 ] || pl_fail "Т-К5б без строки «админ бот не открывал»"
    [ "$(tg_count "$TOKEN_B" "$ADMIN_B" "со слов шлюз" "$ts0")" -ge 1 ] || pl_fail "в Т-К5б нет справки со слов видящего шлюза"
    [ "$(pl_q B '(s.get("code_window") or {}).get("open")')" = "true" ] || \
        pl_fail "у B при ничьей и своём «не вижу» окно кода закрыто · $(pl_quo_trace B)"
    sleep $((QT_TIE_CLEAR + 10))
    na="$(tg_count "$TOKEN_A" "$ADMIN_A" "🚨 Ничья о сервере" "$ts0")"; nb="$(tg_count "$TOKEN_B" "$ADMIN_B" "🚨 Ничья о сервере" "$ts0")"
    [ "$na" = 1 ] && [ "$nb" = 1 ] || pl_fail "тревога ничьей повторилась: от A $na, от B $nb"
    bash "$PL_HERE/net.sh" heal >/dev/null
    T0=$(date +%s)
    # отбой — T_tie_clear после конца ничьей, а она кончается позже починки (два ответа подряд видящему):
    # отсчёт от починки — нижний предел без запаса, от конца ничьей у B (status.json раз в секунду, цикл опроса сценария) —
    # с запасом на них (6 с); отбой без ожидания T_tie_clear пришёл бы через такт после конца ничьей
    t_gone=""; t_oa=""; t_ob=""
    for _ in $(seq 1 240); do
        [ -n "$t_gone" ] || [ "$(pl_q B 'not (s["quo"]["cand"].get("no") == 2 and s["quo"]["cand"].get("see") == 2)')" != "true" ] || t_gone=$(since "$T0")
        oa="$(tg_count "$TOKEN_A" "$ADMIN_A" "больше нет" "$ts0")"; ob="$(tg_count "$TOKEN_B" "$ADMIN_B" "больше нет" "$ts0")"
        [ -n "$t_oa" ] || [ "$oa" -lt 1 ] || t_oa=$(since "$T0")
        [ -n "$t_ob" ] || [ "$ob" -lt 1 ] || t_ob=$(since "$T0")
        [ "$oa" -ge 1 ] && [ "$ob" -ge 1 ] && break
        sleep 1
    done
    t_over=$(since "$T0")
    sleep $((QT_TIE_CLEAR / 2))
    oa="$(tg_count "$TOKEN_A" "$ADMIN_A" "больше нет" "$ts0")"; ob="$(tg_count "$TOKEN_B" "$ADMIN_B" "больше нет" "$ts0")"
    [ "$oa" = 1 ] && [ "$ob" = 1 ] || pl_fail "отбой ничьей: от A $oa, от B $ob (за $t_over с после починки) — ожидалось по одному · $(pl_quo_trace B)"
    t_first=$(( t_oa < t_ob ? t_oa : t_ob ))
    [ "$t_first" -ge "$QT_TIE_CLEAR" ] || \
        pl_fail "отбой ничьей через $t_first с после починки (A $t_oa, B $t_ob) — раньше T_tie_clear ($QT_TIE_CLEAR с): ничья не может кончиться раньше починки"
    [ -n "$t_gone" ] || pl_fail "отбой ничьей от B через $t_ob с после починки, а конца ничьей у B в status.json не было · $(pl_quo_trace B)"
    [ $((t_ob - t_gone)) -ge $((QT_TIE_CLEAR - 6)) ] || \
        pl_fail "отбой ничьей от B через $((t_ob - t_gone)) с после конца ничьей у B — меньше T_tie_clear ($QT_TIE_CLEAR с) за вычетом запаса на status.json и опрос (6 с) · $(pl_quo_trace B)"
    note "tie: тревога через $((t_alarm - t_tie0)) с ничьей, отбой через $t_first–$t_over с после починки ($((t_ob - t_gone)) с от конца ничьей у B)"
    pl_cleanup_quiet
fi

# ── аварийный возврат: клон ни с кем не на связи ─────────────────────────────────────────────────
if has back; then
    q_up 1,2
    q_move_button
    T0=$(date +%s)
    wait_t A 's["quo"]["views"].get("awgqar", {}).get("w") == "see"' 120 "A видит свой клон на B (зонд awgqar)" || exit 1
    for s in 1 2; do
        for _ in $(seq 1 90); do [ "$(pl_gq "$s" '(s["voters"].get("awgqar") or {}).get("view")')" = "see" ] && break; sleep 1; done
        [ "$(pl_gq "$s" '(s["voters"].get("awgqar") or {}).get("view")')" = "see" ] || pl_fail "шлюз $s не видит клон: $(pl_gq "$s" 's["voters"]')"
    done
    T0=$(date +%s)
    fault "ip daddr 198.51.100.7 udp dport 45871 drop comment \"клон недоступен\""
    wait_t A 's["out"].get("quorum_return")' 260 "A забрала пользователей сама (аварийный возврат по Q1)" || exit 1
    t_back=$WT
    [ "$t_back" -ge "$QT_UP" ] || pl_fail "аварийный возврат через $t_back с — раньше T_up ($QT_UP с)"
    note "back: аварийный возврат через $t_back с"
    pl_cleanup_quiet
fi
# ── возврат «сразу» по Q2 ────────────────────────────────────────────────────────────────────────
if has q2; then
    Q_RETURN=immediate Q_MIN_INTERVAL=0 q_up 1,2
    q_move_button
    away_ts="$(pl_q A 'int(s["out"]["at_ts"])')"           # отъезд: с него Q2 может набирать «здоров» (HEALTHY_S)
    T0=$(date +%s)
    wait_t A 's["quo"]["count"].get("role") == "follow" and s["quo"]["count"].get("y") == 3' 120 "A в отъезде: Q2 — «да» 3 из 3" || exit 1
    wait_t A 's["out"].get("state") in ("returning", "home")' 600 "A зовёт пользователей обратно сама (возврат «сразу»)" || exit 1
    t_ret=$(( $(date +%s) - away_ts ))
    [ "$t_ret" -ge $((QT_HEALTHY - 5)) ] || pl_fail "возврат «сразу» через $t_ret с после отъезда — раньше reserve.healthy_seconds ($QT_HEALTHY с)"
    dns_set 203.0.113.5
    pl_wait A 's["out"].get("state") == "home" and s["jrn"]["role"] == "holder" and s["jrn"]["holder"] == "self"' 300 \
        "пользователи и эпоха — снова у A" || exit 1
    note "q2: возврат «сразу» начат через $t_ret с после отъезда"
    pl_cleanup_quiet
fi

echo "ok (${SUMMARY#; })"
