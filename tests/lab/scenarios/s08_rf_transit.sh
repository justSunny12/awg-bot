#!/bin/bash
# s08_rf_transit — РФ-доступ пользователей через второй сервер на паре настоящих ботов стенда
# (tests/lab/pairlab.sh, tests/lab/rtlab.sh): у каждого сервера свой слот 1 с настоящим линком AWG до
# своего шлюза (A «Alpha» ↔ G1, B «Beta» ↔ GR), пользователь cA на awg0 A, «РФ-сайт» на 192.0.2.10
# (в наборе пользователя) и 192.0.2.20 (не в наборе). Работают сервисы основного бота: такт живости
# слотов с тактом rfpeer, файервол хоста, наборы и цепочка маркировки, опрос трафика (таблица учёта),
# канал пары с `st.rfp`, поллинг на макете Bot API (кнопки — нажатиями через макет).
#
# Проверяет (одна пара, пункты по порядку — каждый начинается из состояния предыдущего):
#   а. согласие: у B свои шлюзы живы и тумблер «Мои шлюзы для резерва» включён — у A слот 29 в
#      списке слотов, широкие AllowedIPs линка пары (0.0.0.0/0), у B правило 151, rfpeer.json у обоих;
#      РФ-сайт видит cA адресом G1, адрес не из набора — адресом A;
#   б. G1 выключен (net.sh kill) — A на слоте 29 не дольше 60 с; сайт видит cA адресом GR, адрес не из
#      набора — по-прежнему адресом A; скачивание 5 МБ через транзит проходит; счётчики rfp_up/rfp_dn
#      у B растут на объём; Т-РТ6 админу A и Т-РТ10 админу B — по одному, без звука; согласие A —
#      `peer` (петли нет); в input таблицы A правило `iifname "awgpair" ip daddr != … drop` раньше
#      `ct state established,related accept` в обеих формах файервола;
#   ж. безопасность в эпизоде: с A через линк пары к хосту B на чужой порт — отказ (канал пары —
#      проходит); к сети за шлюзом B (10.1.3.2) с адреса линка пары — не доходит; к пользователю B
#      (10.8.2.5) с адреса линка пары и с подменённым источником — не доходит (контроль: от B доходит);
#   в. G1 снова жив — назад на G1 не раньше 60 с после переключения; Т-РТ7 админу A, Т-РТ11 админу B
#      с объёмом; сайт снова видит адрес G1;
#   г. G1 снова мёртв, A на 29; админ B выключает тумблер в эпизоде (диалог «Выключить?» → «да») — A
#      уходит со слота за такт, Т-РТ8 со звуком («его админ выключил…»), сайт видит адрес A, правила
#      151 у B нет; тумблер обратно;
#   е. A на 29 снова; линк пары оборван (net.sh cut A B) — слот пропал не позже 180 с + такт после
#      последнего рукопожатия, Т-РТ8 «нет связи с «Beta»»;
#   з. B сама на слоте 29 (GR выключен, A здорова) — согласие B `peer`, у A слота 29 нет;
#   и. G1 и GR мертвы оба — ни один не на слоте 29, таблица 100 у обоих — свой линк, не awgpair;
#   д. удаление пары в эпизоде (кнопка «Удалить резерв» у A) — у A слота 29, rfpeer.json, политики
#      слота 29 нет, трафик на своём слоте, Т-РТ8 «резерв удалён»; у B — ни правила 151, ни правил
#      транзита в таблице, ни rfpeer.json.
#
# Не покрыто здесь: перезапуски ботов и `awg-pair` в эпизоде (60 запросов подряд), кворум с транзитом
# (линки слотов A закрыты при живых зондах, клон A в РФ через awgrsv), перезагрузка пространства B
# (поколение таблицы учёта), правило с линка пары в lan_space B — у B нет адресов локальных сетей
# в туннеле; снимки экранов — screen-проходом.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/pairlab.sh"
. "$HERE/quolab.sh"
. "$HERE/rtlab.sh"
trap pl_cleanup EXIT

seen_via() {     # seen_via <ожидаемый src> <адрес> <что> — один запрос /ip от cA, сайт видит ожидаемый адрес
    local got
    got="$(rt_get cA "$2")"
    [ "$got" = "$1" ] || pl_fail "$3: сайт $2 видит cA адресом «${got:-нет ответа}», ожидался $1 · A: $(pl_q A 's["rt"]' | cut -c1-900)"
}

in_order() {     # in_order <узел> — в input таблицы правило линка пары раньше established
    local out a e
    out="$(ip netns exec "lab-$1" nft -a list chain inet awg_bot_guard input 2>&1)"
    a="$(printf '%s\n' "$out" | grep -n 'iifname "awgpair" ip daddr != 10.99.98.2 drop' | head -n1 | cut -d: -f1)"
    e="$(printf '%s\n' "$out" | grep -n 'ct state established,related accept' | head -n1 | cut -d: -f1)"
    [ -n "$a" ] && [ -n "$e" ] && [ "$a" -lt "$e" ] || pl_fail "input $1: правило линка пары (строка ${a:-нет}) не раньше established (строка ${e:-нет}): $out"
}

spy() {          # spy <пространство> <порт> — счётчик TCP SYN на порт в prerouting пространства
    ip netns exec "$1" nft -f - <<EOF
table ip labspy
delete table ip labspy
table ip labspy {
    counter c { }
    chain pre { type filter hook prerouting priority -300; policy accept; tcp dport $2 tcp flags syn counter name "c"; }
}
EOF
}
spy_n() { ip netns exec "$1" nft -j list counter ip labspy c 2>/dev/null | "$PY" -c 'import json,sys
d=json.load(sys.stdin); print(next(x["counter"]["packets"] for x in d["nftables"] if "counter" in x))' 2>/dev/null || echo "?"; }

syn_from() {     # syn_from <src> <dst> <порт> — TCP-коннект из lab-A с адреса src (IP_FREEBIND): ok / refused (дошёл,
                 # порт закрыт — RST) / timeout (не дошёл или отброшен)
    ip netns exec lab-A "$PY" -c 'import socket,sys
s=socket.socket(); s.setsockopt(socket.SOL_IP, 15, 1); s.settimeout(3)
try:
    s.bind((sys.argv[1], 0)); s.connect((sys.argv[2], int(sys.argv[3]))); print("ok")
except ConnectionRefusedError:
    print("refused")
except OSError:
    print("timeout")' "$1" "$2" "$3"
}

# ── а. согласие и слот 29 ────────────────────────────────────────────────────────────────────────
rt_up
T0=$(date +%s)
rt_wait B 'r.get("offer") == [True, ""]' 120 "B даёт свои шлюзы (свой слот жив, тумблер включён)" || exit 1
rt_wait A '29 in (r.get("slots") or [])' 90 "у A слот 29 «шлюзы «Beta»»" || exit 1
t_slot=$WT
rt_wait A '"0.0.0.0/0" in (r.get("allowed") or [])' 40 "широкие AllowedIPs линка пары у A" || exit 1
rt_wait B 'r.get("rule151") is True' 20 "правило 151 у B" || exit 1
[ "$(rt_q A '(r.get("file") or {}).get("slot")')" = "true" ] || pl_fail "а: rfpeer.json у A без slot: $(rt_q A 'r.get("file")')"
[ "$(rt_q B '(r.get("file") or {}).get("offer")')" = "true" ] || pl_fail "а: rfpeer.json у B без offer: $(rt_q B 'r.get("file")')"
[ "$(rt_q A 'r.get("active")')" = "1" ] || pl_fail "а: A не на своём слоте: $(rt_q A 'r.get("active")')"
rt_wait A 'r.get("link_ok") is True' 60 "маркировка A включена (свой слот проходит)" || exit 1
seen_via 10.1.1.2 "$RT_RF_SITE" "а: РФ-адрес через свой шлюз"
seen_via 203.0.113.5 "$RT_RF_OTHER" "а: адрес не из набора — мимо шлюза"
note "а: согласие и слот 29 у A через ${t_slot} с"

# ── б. свои шлюзы A мертвы — слот 29 ─────────────────────────────────────────────────────────────
c0_up="$(rt_q B 'r["counters"]["up"]')"; c0_dn="$(rt_q B 'r["counters"]["dn"]')"
ts_b="$(date +%s)"
T0=$ts_b
bash "$PL_HERE/net.sh" kill G1 >/dev/null
rt_wait A 'r.get("active") == 29' 120 "A на слоте 29 после гибели G1" || exit 1
t_sw=$WT; sw_at=$(date +%s)
[ "$t_sw" -le 60 ] || pl_fail "б: переключение на слот 29 через $t_sw с — дольше 60 с"
seen_via 10.1.3.2 "$RT_RF_SITE" "б: РФ-адрес через шлюз B"
seen_via 203.0.113.5 "$RT_RF_OTHER" "б: адрес не из набора — мимо транзита"
got="$(ip netns exec lab-cA curl -s -m 90 -o /dev/null -w '%{size_download}' "http://$RT_RF_SITE/blob?n=5000000")"
[ "$got" = 5000000 ] || pl_fail "б: скачивание 5 МБ через транзит — получено ${got:-0} байт"
sleep 2
c1_up="$(rt_q B 'r["counters"]["up"]')"; c1_dn="$(rt_q B 'r["counters"]["dn"]')"
[ "$(( c1_dn - c0_dn ))" -ge 5000000 ] || pl_fail "б: rfp_dn у B вырос на $(( c1_dn - c0_dn )) байт — меньше скачанного (5 МБ): $(rt_q B 'r["counters"]')"
[ "$(( c1_up - c0_up ))" -gt 0 ] || pl_fail "б: rfp_up у B не растёт: $(rt_q B 'r["counters"]')"
for _ in $(seq 1 30); do [ "$(rt_tg B '🇷🇺 РФ-доступ пользователей' "$ts_b")" -ge 1 ] && break; sleep 1; done
[ "$(rt_tg A '🔁 РФ-доступ — через шлюзы' "$ts_b" silent)" = 1 ] || pl_fail "б: Т-РТ6 админу A: $(rt_tg A '🔁 РФ-доступ — через шлюзы' "$ts_b")"
[ "$(rt_tg B '🇷🇺 РФ-доступ пользователей' "$ts_b" silent)" = 1 ] || pl_fail "б: Т-РТ10 админу B: $(rt_tg B '🇷🇺 РФ-доступ пользователей' "$ts_b")"
[ "$(rt_tg A 'Шлюз' "$ts_b")" = 0 ] || pl_fail "б: при переключении на слот 29 ушло ещё и обычное письмо о шлюзе: $(rt_tg A 'Шлюз' "$ts_b")"
rt_wait A 'r.get("offer") == [False, "peer"]' 40 "согласие A — peer (сам хожу через шлюзы B)" || exit 1
rt_wait B '29 not in (r.get("slots") or [])' 60 "у B нет слота 29 через шлюзы A, пока A на транзите" || exit 1
ip netns exec lab-B nft list table inet awg_bot_guard | grep -q 'ip saddr 10.99.98.2 masquerade' \
    || pl_fail "б: у B нет маскарада транзита: $(ip netns exec lab-B nft list table inet awg_bot_guard | grep -n 10.99.98.2)"
in_order A
res="$(pl_cmd A fw '{"enabled": true}')" || pl_fail "б: файервол A с фильтром не встал: $res"
in_order A
seen_via 10.1.3.2 "$RT_RF_SITE" "б: РФ-адрес через шлюз B при файерволе с фильтром"
res="$(pl_cmd A fw '{"enabled": false}')" || pl_fail "б: файервол A без фильтра не встал: $res"
in_order A
note "б: слот 29 через ${t_sw} с, 5 МБ через транзит, rfp ↑$(( c1_up - c0_up )) ↓$(( c1_dn - c0_dn ))"

# ── ж. безопасность в эпизоде (линк пары: A — 10.99.98.2, B — 10.99.98.1) ────────────────────────────────────────────────────────────────────
ip netns exec lab-B "$PY" -c 'import socket,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("0.0.0.0", 2222)); s.listen(8)
time.sleep(120)' & SPY_L=$!
sleep 1
# порт канала пары у B открыт с линка пары (слушает A — у B ответ RST): пакет до хоста B доходит
r="$(syn_from 10.99.98.2 10.99.98.1 8788)"
[ "$r" = refused ] || pl_fail "ж: порт канала пары у B с линка пары — «$r», ожидался отказ RST (пакет дошёл) — проверка пуста"
r="$(syn_from 10.99.98.2 10.99.98.1 2222)"
[ "$r" = timeout ] || pl_fail "ж: с линка пары к хосту B на порт 2222 (слушает) — «$r», ожидалось: отброшено"
kill "$SPY_L" 2>/dev/null
spy lab-GR 8099; spy lab-cB 8099
ip -n lab-A route replace 10.1.3.2/32 dev awgpair
ip -n lab-A route replace 10.8.2.5/32 dev awgpair
syn_from 10.99.98.2 10.1.3.2 8099 >/dev/null
syn_from 10.99.98.2 10.8.2.5 8099 >/dev/null
syn_from 172.16.5.5 10.8.2.5 8099 >/dev/null
ip -n lab-A route del 10.1.3.2/32 dev awgpair 2>/dev/null; ip -n lab-A route del 10.8.2.5/32 dev awgpair 2>/dev/null
[ "$(spy_n lab-GR)" = 0 ] || pl_fail "ж: пакет с линка пары дошёл до сети за шлюзом B (10.1.3.2): $(spy_n lab-GR)"
[ "$(spy_n lab-cB)" = 0 ] || pl_fail "ж: пакет с линка пары (свой или подменённый источник) дошёл до пользователя B: $(spy_n lab-cB)"
ip netns exec lab-B curl -s -m 2 -o /dev/null "http://10.8.2.5:8099/" 2>/dev/null
[ "$(spy_n lab-cB)" -ge 1 ] || pl_fail "ж: и от самого B пакет до пользователя B не доходит — проверка пуста"
for n in lab-GR lab-cB; do ip netns exec "$n" nft delete table ip labspy 2>/dev/null; done
note "ж: линк пары — только канал пары и РФ-назначения"

# ── в. возврат на G1 ─────────────────────────────────────────────────────────────────────────────
ts_v="$(date +%s)"
T0=$ts_v
bash "$PL_HERE/net.sh" heal >/dev/null
rt_wait A 'r.get("active") == 1' 240 "A снова на G1" || exit 1
t_ret=$WT; since_sw=$(( $(date +%s) - sw_at ))
[ "$since_sw" -ge 58 ] || pl_fail "в: возврат на G1 через $since_sw с после переключения — раньше 1 мин"
for _ in $(seq 1 40); do [ "$(rt_tg B 'снова идёт через его шлюзы' "$ts_v")" -ge 1 ] && break; sleep 1; done
[ "$(rt_tg A '🟢 РФ-доступ снова через свой шлюз' "$ts_v" silent)" = 1 ] || pl_fail "в: Т-РТ7 админу A: $(rt_tg A '🟢 РФ-доступ снова через свой шлюз' "$ts_v")"
[ "$(rt_tg B 'через твои прошло' "$ts_v" silent)" = 1 ] || pl_fail "в: Т-РТ11 админу B с объёмом: $(rt_tg B 'снова идёт через его шлюзы' "$ts_v") без объёма или нет"
[ "$(rt_tg A 'снова в строю' "$ts_v")" = 0 ] || pl_fail "в: к Т-РТ7 ушло ещё и «снова в строю»"
[ "$(rt_tg A '🔁 РФ-доступ — через шлюзы' "$ts_b")" = 1 ] || pl_fail "в: Т-РТ6 повторился"
rt_wait A 'r.get("link_ok") is True' 30 "маркировка A на G1" || exit 1
seen_via 10.1.1.2 "$RT_RF_SITE" "в: РФ-адрес снова через G1"
note "в: возврат на G1 через ${t_ret} с после починки (${since_sw} с после переключения)"

# ── г. отзыв согласия B в эпизоде ────────────────────────────────────────────────────────────────
back_at=$(date +%s)
T0=$back_at
bash "$PL_HERE/net.sh" kill G1 >/dev/null
rt_wait A 'r.get("active") == 29' 180 "A снова на слоте 29" || exit 1
rt_wait B '(r.get("rfpeer") or {}).get("peer_use")' 40 "у B эпизод второй стороны" || exit 1
ts_g="$(date +%s)"
T0=$ts_g
rt_cb B rfmine_off
rt_wait A 'r.get("raw_active") != "29" and 29 not in (r.get("slots") or [])' 40 "A ушла со слота 29 после отзыва согласия" || exit 1
t_off=$WT
[ "$(rt_q A 'r.get("active")')" = 1 ] || pl_fail "г: A после отзыва согласия не на своём слоте: $(rt_q A 'r.get("active")')"
[ "$(rt_q B 'r.get("rule151")')" = "false" ] || pl_fail "г: правило 151 у B осталось после отзыва"
for _ in $(seq 1 15); do [ "$(rt_tg A '🔴 РФ-доступ через шлюзы' "$ts_g")" -ge 1 ] && break; sleep 1; done
[ "$(rt_tg A 'его админ выключил' "$ts_g" critical)" = 1 ] || pl_fail "г: Т-РТ8 со звуком с причиной «его админ выключил»: $(rt_tg A '🔴 РФ-доступ через шлюзы' "$ts_g")"
for _ in $(seq 1 20); do [ "$(rt_get cA "$RT_RF_SITE")" = 203.0.113.5 ] && break; sleep 1; done
seen_via 203.0.113.5 "$RT_RF_SITE" "г: РФ-адрес после отзыва — зарубежный адрес A"
rt_cb B rfmine
rt_wait B 'r.get("offer") == [True, ""]' 60 "тумблер B снова включён — B даёт шлюзы" || exit 1
note "г: уход со слота через ${t_off} с после отзыва"

# ── е. обрыв линка пары ──────────────────────────────────────────────────────────────────────────
T0=$(date +%s)
rt_wait A 'r.get("active") == 29' 180 "A снова на слоте 29 после включения тумблера" || exit 1
ts_e="$(date +%s)"
hs="$(ip netns exec lab-A awg show awgpair latest-handshakes | awk '{print $2}')"
T0=$ts_e
bash "$PL_HERE/net.sh" cut A B >/dev/null
rt_wait A '29 not in (r.get("slots") or []) and r.get("raw_active") != "29"' 360 "слот 29 пропал после обрыва линка пары" || exit 1
t_cut=$WT; hs_age=$(( $(date +%s) - hs ))
[ "$hs_age" -le 225 ] || pl_fail "е: слот пропал через $hs_age с после последнего рукопожатия — дольше 180 с + такт"
for _ in $(seq 1 15); do [ "$(rt_tg A '🔴 РФ-доступ через шлюзы' "$ts_e")" -ge 1 ] && break; sleep 1; done
[ "$(rt_tg A 'нет связи с' "$ts_e" critical)" = 1 ] || pl_fail "е: Т-РТ8 «нет связи»: $(rt_tg A '🔴 РФ-доступ через шлюзы' "$ts_e")"
bash "$PL_HERE/net.sh" heal >/dev/null
note "е: слот пропал через ${t_cut} с после обрыва (${hs_age} с после рукопожатия)"

# ── з. B сама на слоте 29 — согласия нет (peer) ──────────────────────────────────────────────────
T0=$(date +%s)
rt_wait A 'r.get("active") == 1 and r.get("offer") == [True, ""]' 240 "A здорова и даёт шлюзы" || exit 1
bash "$PL_HERE/net.sh" kill GR >/dev/null
rt_wait B 'r.get("active") == 29' 240 "B на слоте 29 через шлюзы A" || exit 1
rt_wait B 'r.get("offer") == [False, "peer"]' 40 "согласие B — peer" || exit 1
rt_wait A '(r.get("peer_rfp") or {}).get("why") == "peer" and 29 not in (r.get("slots") or [])' 60 "A: от B why=peer, слота 29 нет" || exit 1
note "з: B на слоте 29 — согласие peer"

# ── и. оба без шлюзов — петли нет ────────────────────────────────────────────────────────────────
bash "$PL_HERE/net.sh" kill G1 >/dev/null
T0=$(date +%s)
rt_wait B 'r.get("raw_active") != "29"' 120 "B ушла со слота 29, когда шлюзы A умерли" || exit 1
sleep 30
for n in A B; do
    [ "$(rt_q "$n" 'r.get("raw_active")')" != 29 ] || pl_fail "и: $n на слоте 29 при мёртвых шлюзах обоих"
    t100="$(ip -n "lab-$n" route show table 100)"
    case "$t100" in *"dev awglink"*) ;; *) pl_fail "и: таблица 100 у $n — не свой линк: $t100" ;; esac
done
note "и: оба без шлюзов — оба на своих линках"

# ── д. удаление пары в эпизоде ───────────────────────────────────────────────────────────────────
bash "$PL_HERE/net.sh" heal >/dev/null
bash "$PL_HERE/net.sh" kill G1 >/dev/null
T0=$(date +%s)
rt_wait A 'r.get("active") == 29' 300 "A на слоте 29 перед удалением пары" || exit 1
ts_d="$(date +%s)"
T0=$ts_d
rt_cb A unpair_yes
rt_wait A 'r.get("paired") is False' 60 "пара у A снята" || exit 1
rt_wait B 'r.get("paired") is False' 60 "пара у B снята (gone от A)" || exit 1
rt_wait A '29 not in (r.get("slots") or []) and r.get("active") == 1 and r.get("file") is None' 60 "у A ни слота 29, ни rfpeer.json" || exit 1
[ -z "$(ip -n lab-A route show table 129)" ] || pl_fail "д: таблица слота 29 у A осталась: $(ip -n lab-A route show table 129)"
ip netns exec lab-A ip rule show | grep -q 'lookup 129' && pl_fail "д: правило слота 29 у A осталось"
for _ in $(seq 1 60); do [ "$(rt_q B 'r.get("file") is None and r.get("rule151") is False')" = true ] && break; sleep 1; done
[ "$(rt_q B 'r.get("file") is None')" = true ] || pl_fail "д: rfpeer.json у B остался: $(rt_q B 'r.get("file")')"
[ "$(rt_q B 'r.get("rule151")')" = false ] || pl_fail "д: правило 151 у B осталось"
tr="$(ip netns exec lab-B nft list table inet awg_bot_guard | grep -E '10\.99\.98\.2 (masquerade|ct direction reply|oifname)|saddr 10\.99\.98\.2 oifname')"
[ -z "$tr" ] || pl_fail "д: правила транзита у B остались: $tr"
for _ in $(seq 1 20); do [ "$(rt_tg A 'резерв удалён' "$ts_d")" -ge 1 ] && break; sleep 1; done
[ "$(rt_tg A 'резерв удалён' "$ts_d" critical)" = 1 ] || pl_fail "д: Т-РТ8 «резерв удалён»: $(rt_tg A '🔴 РФ-доступ через шлюзы' "$ts_d")"
note "д: удаление пары в эпизоде — всё снято"
echo "ok (${SUMMARY#; })"
