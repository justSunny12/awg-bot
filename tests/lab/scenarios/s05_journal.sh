#!/bin/bash
# s05_journal — журнал данных резерва на паре серверов стенда (tests/lab/pairlab.sh): правки
# основы уходят резерву событиями по настоящему каналу пары, после гибели основы резерв
# открывает пакет-снимок + события по доле Шлюза 1, а при переезде кнопкой правки бота основы
# на резерве приходят в базу основы через секунды.
#
# Проверяет:
#   1. пара с jrn1 у обеих сторон: захват включён, резерв принял снимок (pkg3);
#   2. нагрузка правками на A (40 профилей с устройствами подряд) — все события подтверждены
#      резервом (`jack`), его хранилище reserve/jrn/ — по последний номер основы;
#   3. A отрезана от B и делает ещё 5 правок — они не подтверждены; затем A «гибнет»
#      (пространство lab-A без сети);
#   4. B открывает по своей доле и доле Шлюза 1 (как такт голосования, получивший долю
#      шлюза) — процесс открытия в песочнице доходит до done, клон поднят;
#   5. база гостя равна базе A на момент последнего подтверждённого события — по классам
#      журнала (tests/jrnequiv): ни одной правки после него, ни одной потерянной до него;
#      строка эпохи гостя — эпоха A + 1, держатель, точка расхождения — последний
#      подтверждённый номер;
#   6. новая пара; переезд кнопкой: A собирает свежий снимок, резерв поднимает гостя
#      следующим, A передаёт бота (released) и сама следует; гость берёт эпоху и ставит
#      вебхук; правка в боте основы на резерве — в базе A не дольше чем через 10 с.
#
# Не покрыто здесь (нет обвязки стенда): сессия пользователя, оставшаяся на A, вместе с
# трафиком клона на B — лимит по сумме и DROP у обоих; нужны конфиги пользователя в cA,
# трафик через оба туннеля и опрос трафика у основы в следовании.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/pairlab.sh"
trap pl_cleanup EXIT
GUEST_DB=/run/awg-bot-guest/data/bot.db

# ── 1–2. нагрузка ────────────────────────────────────────────────────────────────────────────────
pl_pair_up
pl_cmd A share "{\"slot\": 1, \"out\": \"$PL/shared/share-1.json\"}" >/dev/null || pl_fail "доля Шлюза 1 не получена"
res="$(pl_cmd A load '{"n": 40, "prefix": "Нагрузка", "pause": 0.05}' 120)" || pl_fail "нагрузка: $res"
case "$res" in *'"error"'*) pl_fail "правка основы отвергнута: $res" ;; esac
pl_wait A 's["jrn"]["acked"] == s["last_i"] and s["last_i"] >= 40' 60 "все правки A подтверждены резервом" || exit 1
last="$(pl_q A 's["last_i"]')"
pl_wait B "(s.get('keep_last') or [0, 0])[1] == $last" 20 "хранилище B по номер $last" || exit 1
pl_cmd A backup "{\"out\": \"$PL/shared/a-acked.db\"}" >/dev/null || pl_fail "копия базы A не снята"

# ── 3. отрезана, ещё правки, гибель ──────────────────────────────────────────────────────────────
bash "$PL_HERE/net.sh" cut A B >/dev/null
res="$(pl_cmd A load '{"n": 5, "prefix": "Без-подтверждения", "pause": 0.1, "base": 100}' 60)" || pl_fail "правки A после разреза: $res"
sleep 3
[ "$(pl_q A 's["jrn"]["acked"]')" = "$last" ] || pl_fail "правки после разреза подтверждены — разрез не сработал: $(pl_q A 's["jrn"]')"
ip -n lab-A link set v-A down
pl_wait B 's.get("seen") == "down"' 150 "B заметил, что A не на связи (линк пары молчит)" || exit 1

# ── 4. открытие по доле Шлюза 1 ──────────────────────────────────────────────────────────────────
# оболочку зоны на хостах держат стороны линков шлюзов (их стенд не поднимает) — ставим её сами
nsenter --net=/run/netns/lab-B bash "$PL_Z" --shell >/dev/null || pl_fail "оболочка зоны в lab-B не поднялась"
res="$(pl_cmd B open_share "{\"share\": \"$PL/shared/share-1.json\"}" 60)" || pl_fail "открытие по доле: $res"
st="$(pl_open_wait 150)"
case "$st" in *'"step": "done"'*) ;; *) pl_fail "процесс открытия не дошёл до done: $st · $(tail -n3 /run/awg-bot/rsv/rsvopen.err 2>/dev/null)" ;; esac
systemctl stop awg-bot-guest.service 2>/dev/null          # гость здесь не нужен: проверяется база, которую положил процесс
ip netns exec awg-guest ip link show awg0 >/dev/null 2>&1 || pl_fail "клона awg0 в зоне нет"

# ── 5. база гостя = база A по последнему подтверждённому ─────────────────────────────────────────
out="$(cd "$PL_ROOT" && "$PY" - "$GUEST_DB" "$PL/shared/a-acked.db" "$PL/A/data/bot.db" "$last" <<'PYEOF'
import sqlite3, sys
from tests import jrnequiv
guest, acked, final = (sqlite3.connect(f"file:{p}?mode=ro", uri=True) for p in sys.argv[1:4])
last = int(sys.argv[4])
d = jrnequiv.diff(acked, guest)
if d:
    print("база гостя не равна базе A по последнему подтверждённому:", "; ".join(d)[:900]); sys.exit(1)
if not jrnequiv.diff(final, guest):
    print("база A после разреза не отличается от базы гостя — правок без подтверждения не было, проверка пуста"); sys.exit(1)
late = guest.execute("SELECT count(*) FROM clients WHERE name LIKE 'Без-подтверждения-%'").fetchone()[0]
if late:
    print(f"в базе гостя {late} правок, которых резерв не подтверждал"); sys.exit(1)
n = guest.execute("SELECT count(*) FROM clients WHERE name LIKE 'Нагрузка-%'").fetchone()[0]
if n != 40:
    print(f"в базе гостя {n} профилей нагрузки из 40"); sys.exit(1)
ae = acked.execute("SELECT epoch FROM jrn_meta").fetchone()[0]
e, holder, role, fork = guest.execute("SELECT epoch, holder, role, fork_i FROM jrn_meta").fetchone()
if (e, holder, role) != (ae + 1, "self", "holder"):
    print(f"эпоха гостя после аварии {e}/{holder}/{role}, ожидалось {ae + 1}/self/holder"); sys.exit(1)
if fork != last:
    print(f"точка расхождения гостя {fork}, а последнее подтверждённое — {last}"); sys.exit(1)
print("ok")
PYEOF
)" || pl_fail "$out"

# ── 6. переезд кнопкой: правки гостя — в базе A через секунды ────────────────────────────────────
pl_cleanup_quiet
pl_pair_up
res="$(pl_cmd A load '{"n": 10, "prefix": "До-переезда", "pause": 0.05}' 60)" || pl_fail "нагрузка перед переездом: $res"
pl_wait A 's["jrn"]["acked"] == s["last_i"]' 60 "правки перед переездом подтверждены" || exit 1
nsenter --net=/run/netns/lab-B bash "$PL_Z" --shell >/dev/null || pl_fail "оболочка зоны в lab-B не поднялась"
res="$(pl_cmd A move '{}')" || pl_fail "переезд кнопкой не начат: $res"
# шаг pkg: свежий снимок вне очереди (PKG_BATCH_SECONDS + такт пакета) — и просьба о разовом ключе
pl_wait A 's["out"].get("state") != "leaving" or s["out"].get("step") != "pkg"' 150 \
    "A вышла из шага pkg (свежий снимок подтверждён резервом)" || exit 1
[ "$(pl_q A 's["out"].get("state")')" = "leaving" ] || pl_fail "переезд сорван на шаге pkg: $(pl_q A 's["out"]')"
st="$(pl_open_wait 120)"
case "$st" in *'"step": "done"'*) ;; *) pl_fail "процесс открытия при переезде не дошёл до done: $st" ;; esac
pl_guest_start
pl_wait A 's["out"].get("step") == "record"' 120 "A на шаге записи" || exit 1
"$PY" -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d[sys.argv[2]]="198.51.100.7"; json.dump(d, open(p, "w"))' \
    "$PL/shared/dns.json" "$RECORD"                       # запись меняет человек (провайдер не задан)
pl_wait A 's["out"].get("state") == "away" and s["jrn"]["role"] == "follower"' 240 "A в отъезде и следует" || exit 1
pl_wait G 's.get("role") == "holder" and s.get("holder") == "self"' 60 "гость — держатель эпохи" || exit 1
t0=$(date +%s%N)
printf '{"name": "Правка-на-резерве"}' > "$PL/G/edit_one"
for _ in $(seq 1 40); do [ -e "$PL/G/edit_one.done" ] && break; sleep 0.25; done
[ -e "$PL/G/edit_one.done" ] || pl_fail "бот основы на резерве не выполнил правку за 10 с"
grep -q '"error"' "$PL/G/edit_one.done" && pl_fail "правка гостя отвергнута: $(cat "$PL/G/edit_one.done")"
seen=""
for _ in $(seq 1 40); do
    n="$("$PY" -c 'import sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True)
print(c.execute("SELECT count(*) FROM clients WHERE name = ?", ("Правка-на-резерве",)).fetchone()[0])' "$PL/A/data/bot.db")"
    [ "$n" = 1 ] && { seen=1; break; }
    sleep 0.25
done
ms=$(( ($(date +%s%N) - t0) / 1000000 ))
[ -n "$seen" ] || pl_fail "правка бота на резерве не дошла до базы A за 10 с: A $(pl_q A 's["jrn"]'), гость $(cat "$PL/G/status.json")"
echo "ok (правка гостя у A через $ms мс)"
