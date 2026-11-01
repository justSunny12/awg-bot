#!/bin/bash
# s12_relay_flush — пересылка хвоста (relay), уступившая новому открытию на резерве
# (tests/lab/pairlab.sh): правило DNAT хвоста снимается, а записи conntrack с DNAT на основу
# живут — рукопожатия пользователя по тому же 5-кортежу уходили бы на мёртвую основу; такт
# clone_up сбрасывает их (relay_flush), и пользователь сходится с новым клоном за секунды.
#
# Проверяет:
#   1. пользователь cA — свой AWG до awg0 A (пир в конфиге awg0 A — едет в пакете); переезд
#      кнопкой: cA переходит на адрес B (клиент резолвил запись, пока она была на B) — рукопожатие
#      с клоном;
#   2. возврат кнопкой: B в relay (пересылка хвоста на A), cA остаётся на адресе B — его трафик
#      DNAT на A, рукопожатие с A через пересылку; запись conntrack у B — с DNAT на A;
#   3. A гибнет (процесс остановлен, сеть убита) — рукопожатия cA уходят по записи conntrack на
#      мёртвую A; B открывает по аварийному коду, пересылка уступает открытию;
#   4. на шаге clone_up записи сброшены ровно один раз (relay_flush, строка в журнале B), записей
#      conntrack с DNAT на A у B нет (исходное назначение — адрес B, ответ — от A; собственный
#      поток B к awg0 A без NAT не в счёт), рукопожатие cA с новым клоном — не позже 10 с после
#      clone_up.
#
# На стенде нужен conntrack (пакет conntrack в ВМ — как ZONE_TOOLS на хостах): без него скрипт
# зоны сбросить записи не может, и пункт 4 провалится с этой причиной.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/pairlab.sh"
. "$HERE/quolab.sh"
trap 'kill "${PING:-}" 2>/dev/null; ip -n lab-cA link del awgc 2>/dev/null; pl_cleanup' EXIT
API_B="http://${PL_GW[B]}:8081"
OBF='Jc = 4
Jmin = 40
Jmax = 70
S1 = 10
S2 = 66
H1 = 5-500
H2 = 600-700
H3 = 800-900
H4 = 1000-1100'

user_init() {    # cA: пир на awg0 A (конфиг и ядро), клиент — на адрес A
    local cp spub
    cp="$(awg genkey)"; CPUB="$(printf '%s' "$cp" | awg pubkey)"
    spub="$(sed -n 's/^PrivateKey *= *//p' "$PL/A/awg/awg0.conf" | head -n1 | awg pubkey)"
    mkdir -p "$PL/cA"; awg genpsk > "$PL/cA/psk"
    printf '\n[Peer]\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = 10.8.1.5/32\n' "$CPUB" "$(cat "$PL/cA/psk")" >> "$PL/A/awg/awg0.conf"
    ip netns exec lab-A awg set awg0 peer "$CPUB" preshared-key "$PL/cA/psk" allowed-ips 10.8.1.5/32 || pl_fail "пир cA на awg0 A не заведён"
    cat > "$PL/cA/awgc.conf" <<EOF
[Interface]
PrivateKey = $cp
$OBF

[Peer]
PublicKey = $spub
PresharedKey = $(cat "$PL/cA/psk")
AllowedIPs = 10.8.1.0/24
Endpoint = 203.0.113.5:45871
EOF
    SPUB="$spub"
    ip -n lab-cA link del awgc 2>/dev/null
    ip -n lab-cA link add awgc type amneziawg || pl_fail "awgc в lab-cA не создан"
    ip netns exec lab-cA awg setconf awgc "$PL/cA/awgc.conf" || pl_fail "awg setconf awgc"
    ip -n lab-cA addr add 10.8.1.5/32 dev awgc
    ip -n lab-cA link set awgc up
    ip -n lab-cA route replace 10.8.1.0/24 dev awgc
}
hs() { ip netns exec lab-cA awg show awgc latest-handshakes | awk '{print $2}'; }       # рукопожатие cA, unix
hs_wait() {      # hs_wait <с ts> <срок> <что> — рукопожатие cA не раньше ts; в HS — его момент
    local i
    for i in $(seq 1 "$2"); do HS="$(hs)"; [ "${HS:-0}" -ge "$1" ] && [ "${HS:-0}" -gt 0 ] && return 0; sleep 1; done
    pl_fail "$3: рукопожатия cA с $(date -d "@$1" +%T) нет за $2 с (последнее $(date -d "@${HS:-0}" +%T))"
}
ct_dnat_a() {    # записи conntrack у B с DNAT хвоста: исходное назначение — адрес B, ответ — от A.
    # Собственный поток B к awg0 A (B → 203.0.113.5:45871, без NAT: пробы кворума и линка) под
    # --orig-port-dst 45871 тоже попадает и с мёртвой A живёт [UNREPLIED] — его не считать
    ip netns exec lab-B conntrack -L -p udp --orig-port-dst 45871 2>/dev/null | awk '
        { ns = 0; nd = 0; od = ""; rs = ""
          for (i = 1; i <= NF; i++) {
              if ($i ~ /^src=/ && ++ns == 2) rs = substr($i, 5)
              if ($i ~ /^dst=/ && ++nd == 1) od = substr($i, 5)
          }
          if (od == "198.51.100.7" && rs == "203.0.113.5") n++ }
        END { print n + 0 }'
}
FLUSHED='записи conntrack по портам клонов сброшены'    # строка исполнителя действий (INFO)
peer_hs() {      # peer_hs <пространство> — у awg0 там было рукопожатие с cA
    ip netns exec "$1" awg show all latest-handshakes 2>/dev/null | awk -v k="$CPUB" '$2 == k && $3 > 0 {f = 1} END {exit !f}'
}
endpoint() {     # клиент переподключился по новой записи: интерфейс заново (новый порт, без прежней сессии —
    # пакет прежнего сервера по старой сессии увёл бы Endpoint назад роумингом)
    sed -i "s|^Endpoint = .*|Endpoint = $1|" "$PL/cA/awgc.conf"
    ip -n lab-cA link del awgc 2>/dev/null
    ip -n lab-cA link add awgc type amneziawg || pl_fail "awgc в lab-cA не пересоздан"
    ip netns exec lab-cA awg setconf awgc "$PL/cA/awgc.conf" || pl_fail "awg setconf awgc (Endpoint $1)"
    ip -n lab-cA addr add 10.8.1.5/32 dev awgc
    ip -n lab-cA link set awgc up
    ip -n lab-cA route replace 10.8.1.0/24 dev awgc
}

# ── 1. пользователь на клоне ─────────────────────────────────────────────────────────────────────
pl_init
user_init
pl_start A --poll
pl_start B --poll
pl_wait A 's.get("jrn_on") and s["jrn"]["capture"] == 1 and (s.get("pkg_sent") or {}).get("jrn")' 90 \
    "журнал включён, снимок A подтверждён резервом" || exit 1
pl_wait B '(s.get("pkg_in") or {}).get("gen")' 30 "у B — снимок A (pkg3)" || exit 1
( while :; do ip netns exec lab-cA ping -c1 -W1 10.8.1.1 >/dev/null 2>&1; sleep 0.5; done ) & PING=$!
hs_wait 0 30 "cA до A"
pl_cmd A code "{\"out\": \"$PL/shared/code\"}" >/dev/null || pl_fail "аварийный код A не получен"
nsenter --net=/run/netns/lab-B bash "$PL_Z" --shell >/dev/null || pl_fail "оболочка зоны в lab-B не поднялась"
q_move_button
t=$(date +%s); endpoint 198.51.100.7:45871
hs_wait "$t" 40 "cA до клона на B"
peer_hs awg-guest || pl_fail "клон на B рукопожатия cA ($CPUB) не видел: $(ip netns exec awg-guest awg show all latest-handshakes 2>&1 | head -n5) · у A: $(ip netns exec lab-A awg show awg0 latest-handshakes 2>&1 | grep -F "$CPUB") · cA: $(ip netns exec lab-cA awg show awgc endpoints) $(hs) после $t · DNAT B: $(ip netns exec lab-B nft list ruleset 2>/dev/null | grep -i dnat | head -n5)"

# ── 2. возврат: пересылка хвоста на A ────────────────────────────────────────────────────────────
res="$(pl_cmd A return '{}')" || pl_fail "возврат не начат: $res"
dns_set 203.0.113.5
pl_wait A 's["out"].get("state") == "home" and s["jrn"]["role"] == "holder"' 300 "возврат: пользователи и эпоха у A" || exit 1
pl_wait B 's["in"].get("state") == "relay"' 120 "B пересылает хвост на A (relay)" || exit 1
NOCT=""; command -v conntrack >/dev/null || NOCT=1     # без conntrack — провал в конце, с замером
t=$(date +%s)
# без conntrack живой поток cA (запись conntrack без NAT — к клону) пересылки не получит (скрипт зоны:
# «потоки пользователей, идущие сейчас, пересылки не получат») — клиент переподключается, новый поток
[ -z "$NOCT" ] || endpoint 198.51.100.7:45871
hs_wait "$t" 60 "cA (адрес B) до A через пересылку"
peer_hs lab-A || pl_fail "A рукопожатия cA через пересылку не видела: $(ip netns exec lab-A awg show awg0 latest-handshakes 2>&1 | head -n5)"
[ -n "$NOCT" ] || [ "$(ct_dnat_a)" -ge 1 ] || pl_fail "у B нет записи conntrack с DNAT на A: $(ip netns exec lab-B conntrack -L -p udp 2>/dev/null | head -n5)"

# ── 3. A гибнет, новое открытие по коду ──────────────────────────────────────────────────────────
# после возврата пакет у B израсходован — открыть нечем, пока A не пришлёт свежий (pkg_minutes)
pl_wait B '(s.get("pkg_in") or {}).get("gen")' 240 "у B — свежий пакет A после возврата" || exit 1
pl_cmd A code "{\"out\": \"$PL/shared/code\"}" >/dev/null || pl_fail "аварийный код A после возврата не получен"
# окно кода, открытое до гибели A (после возврата проба B линии A может промахнуться — «не вижу» на
# полминуты), закрывается её «вижу» — код, введённый в него, уйдёт в закрывшееся окно: сначала его нет
pl_wait B 'not (s.get("code_window") or {}).get("open")' 120 "у B закрыто окно кода до гибели A" || exit 1
pl_stop A; bash "$PL_HERE/net.sh" kill A >/dev/null
pl_wait B '(s.get("code_window") or {}).get("open")' 240 "у B открыт приём аварийного кода (Н4) в relay" || exit 1
[ -n "$NOCT" ] || [ "$(ct_dnat_a)" -ge 1 ] || pl_fail "запись conntrack хвоста пропала сама до открытия — проверка пуста"
OS0="$(cat /run/awg-bot/rsv/open.status 2>/dev/null)"    # итог прошлого открытия (кнопкой) — не clone_up этого
cbdata="$(cd "$PL_ROOT" && "$PY" -c 'from awgbot.bot.callbacks import PeerCB; print(PeerCB(action="code").pack())')"
ib() { ip netns exec lab-B curl -s -m5 -X POST "$API_B/inject/$TOKEN_B" -H 'Content-Type: application/json' -d "$1" >/dev/null; }
ib "{\"callback_query\": {\"id\": \"lab-1\", \"chat_instance\": \"lab\", \"data\": \"$cbdata\",
  \"from\": {\"id\": $ADMIN_A, \"is_bot\": false, \"first_name\": \"Админ A\"},
  \"message\": {\"message_id\": 7, \"date\": 1, \"chat\": {\"id\": $ADMIN_A, \"type\": \"private\"}, \"text\": \"роль\"}}}"
sleep 3
ib "{\"message\": {\"message_id\": 8, \"date\": 1, \"chat\": {\"id\": $ADMIN_A, \"type\": \"private\"},
  \"from\": {\"id\": $ADMIN_A, \"is_bot\": false, \"first_name\": \"Админ A\"}, \"text\": \"$(cat "$PL/shared/code")\"}}"

# ── 4. clone_up: записи сброшены, рукопожатие с новым клоном ≤ 10 с ─────────────────────────────
n_flush0="$(grep -c "$FLUSHED" "$PL/B/node.log")"
t_up=""
for _ in $(seq 1 900); do
    os="$(cat /run/awg-bot/rsv/open.status 2>/dev/null)"
    [ "$os" != "$OS0" ] || { sleep 0.2; continue; }
    case "$os" in
        *'"step": "clone_up"'*|*'"step": "record'*|*'"step": "done"'*) t_up=$(date +%s); break ;;
        *'"step": "fail"'*) pl_fail "новое открытие сорвалось: $(cat /run/awg-bot/rsv/open.status)" ;;
    esac
    sleep 0.2
done
[ -n "$t_up" ] || pl_fail "новое открытие не дошло до clone_up за 180 с: $(cat /run/awg-bot/rsv/open.status 2>/dev/null) · $(pl_q B 's["in"]')"
for _ in $(seq 1 30); do [ "$(grep -c "$FLUSHED" "$PL/B/node.log")" -gt "$n_flush0" ] && break; sleep 1; done
flushed="$(( $(grep -c "$FLUSHED" "$PL/B/node.log") - n_flush0 ))"
HS=0; for _ in $(seq 1 60); do HS="$(hs)"; [ "${HS:-0}" -ge "$((t_up - 1))" ] && break; sleep 1; done
dt=$(( HS - t_up ))
[ -z "$NOCT" ] || pl_fail "на стенде нет conntrack (пакет conntrack, ZONE_TOOLS хостов) — сброс записей хвоста не проверить (скрипт зоны --relay-flush на clone_up звался: $flushed раз); рукопожатие cA с новым клоном: $( [ "$HS" -ge "$((t_up - 1))" ] && echo "через $dt с после clone_up" || echo "нет за 60 с")"
[ "$HS" -ge "$((t_up - 1))" ] || pl_fail "рукопожатия cA с новым клоном нет за 60 с после clone_up"
[ "$flushed" = 1 ] || pl_fail "записи conntrack на clone_up сбрасывались $flushed раз, а не один (строка «$FLUSHED» в журнале B)"
n_ct="$(ct_dnat_a)"
[ "$n_ct" = 0 ] || pl_fail "после clone_up у B записей conntrack с DNAT на A: $n_ct — relay_flush не сбросил: $(ip netns exec lab-B conntrack -L -p udp --orig-port-dst 45871 2>&1 | head -n8) · DNAT B: $(ip netns exec lab-B nft list ruleset 2>/dev/null | grep -i dnat | head -n8) · endpoints B: $(ip netns exec lab-B awg show all endpoints 2>&1 | head -n8)"
[ "$dt" -le 10 ] || pl_fail "рукопожатие cA с новым клоном через $dt с после clone_up — дольше 10 с"
grep -q "relay-flush\|пересылка хвоста снята" "$PL/B/node.log" || pl_fail "в журнале B нет снятия пересылки при новом открытии"
echo "ok (рукопожатие cA с новым клоном через ${dt} с после clone_up, записей хвоста после сброса — 0)"
