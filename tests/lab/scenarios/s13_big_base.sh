#!/bin/bash
# s13_big_base — хранилище и память бота основы на резерве по размеру базы основы
# (tests/lab/pairlab.sh): база больше 30 МБ влезает в хранилище гостя, процесс открытия идёт под
# потолком памяти от размера базы (а не сжатого пакета) и не убит OOM.
#
# Проверяет:
#   1. база A раздута до 32 МБ (домены списка профиля админа до включения журнала — снимок их
#      несёт, событий нет); резерв принял пакет, размер базы — полем db_size слова pkg;
#   2. tmpfs хранилища гостя, смонтированный у хоста, перемонтированный изнутри песочницы
#      процесса открытия (те же свойства systemd-run, скрипт зоны --store с GUEST_STORE_MB),
#      хост видит с новым размером;
#   3. переезд кнопкой: MemoryMax юнита awg-bot-rsvopen — rsvzone.open_memory_mb(пакет, db_size)
#      (128 + 4 × пакет + 2 × база: база — двоичной частью пакета, распаковка потоком; снимка
#      rep.bin на этом шаге нет), процесс открытия доходит до done, в журнале ядра — ни одного
#      OOM юнита; пик памяти юнита — в итог;
#   4. хранилище гостя выросло: размер tmpfs /run/awg-bot-guest — rsvzone.store_mb(база после
#      журнала), больше прежних 32 МБ; MemoryMax юнита awg-bot-guest — rsvzone.guest_memory_mb(база);
#   5. бот основы на резерве с этой базой — держатель эпохи, A в отъезде и следует.
#
# Не покрыто здесь: память настоящего юнита гостя — на стенде гость идёт процессом
# tests/lab/guest_main.py вне юнита (у юнита только свойства).
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/pairlab.sh"
. "$HERE/quolab.sh"
trap pl_cleanup EXIT
RUN=/run/awg-bot-guest
CG=/sys/fs/cgroup/system.slice/awg-bot-rsvopen.service

mb_of() { df -m --output=size "$1" 2>/dev/null | tail -n1 | tr -d ' '; }
rsvz() {   # rsvz <выражение над rsvzone> — число из формул продукта
    (cd "$PL_ROOT" && "$PY" -c 'import sys; from awgbot.infra import rsvzone; print(eval(sys.argv[1], {"rsvzone": rsvzone}))' "$1")
}
oom_count() { dmesg 2>/dev/null | grep -c 'oom-kill.*awg-bot-rsvopen'; }

# ── 1. база больше 30 МБ, пакет у резерва ────────────────────────────────────────────────────────
pl_init
out="$(pl_py A -m tests.lab.pairnode bloat --mb 32 2>&1 | tail -n1)" || pl_fail "база A не раздута: $out"
pl_start A --poll
pl_start B --poll
pl_wait A 's.get("jrn_on") and s["jrn"]["capture"] == 1 and (s.get("pkg_sent") or {}).get("jrn")' 240 \
    "журнал включён, снимок A подтверждён резервом" || exit 1
pl_wait B '(s.get("pkg_in") or {}).get("db_size", 0) > 30 * 1024 * 1024' 120 "у B — пакет с размером базы > 30 МБ" || exit 1
db_size="$(pl_q B 's["pkg_in"]["db_size"]')"
pkg_size="$(stat -c %s "$PL/B/data/reserve/guest.pkg")"

# ── 2. перемонтирование хранилища изнутри песочницы видно хосту ──────────────────────────────────
nsenter --net=/run/netns/lab-B bash "$PL_Z" --shell >/dev/null || pl_fail "оболочка зоны в lab-B не поднялась"
GUEST_STORE_MB=32 bash "$PL_Z" --store >/dev/null || pl_fail "хранилище гостя не смонтировано у хоста"
[ "$(mb_of "$RUN")" = 32 ] || pl_fail "хранилище у хоста: $(mb_of "$RUN") МБ, ожидалось 32"
mapfile -t PROPS < <(cd "$PL_ROOT" && AWG_BOT_DATA_DIR="$PL/B/data" "$PY" -c '
import sys
from awgbot.domain.services.reserve import openproc
print("\n".join(openproc.rsvopen_sandbox_props(sys.argv[1], db_size=int(sys.argv[2]))))' "$PL/B/base" "$db_size")
res="$(systemd-run --wait --pipe --collect --quiet "${PROPS[@]}" -p ProtectHome=tmpfs -p "BindReadOnlyPaths=$PL_VENV" \
    --setenv=GUEST_STORE_MB=200 /bin/bash "$PL_Z" --store 2>&1)" || pl_fail "скрипт зоны --store в песочнице отказал: $res"
[ "$(mb_of "$RUN")" = 200 ] || pl_fail "перемонтирование в песочнице хосту не видно: $(mb_of "$RUN") МБ, ожидалось 200 ($res)"
bash "$PL_Z" --wipe >/dev/null
mountpoint -q "$RUN" && pl_fail "хранилище не снято перед переездом"

# ── 3. переезд кнопкой: потолок процесса открытия, без OOM ──────────────────────────────────────
oom0="$(oom_count)"
: > "$PL/open.mem"
( mx=""; peak=0
  for _ in $(seq 1 1800); do
      v="$(systemctl show -p MemoryMax --value awg-bot-rsvopen.service 2>/dev/null)"
      case "$v" in ''|infinity) ;; *) mx="$v" ;; esac
      p="$(cat "$CG/memory.peak" 2>/dev/null || echo 0)"; [ "${p:-0}" -gt "$peak" ] && peak="$p"
      printf '%s %s\n' "${mx:-none}" "$peak" > "$PL/open.mem"
      case "$(cat /run/awg-bot/rsv/open.status 2>/dev/null)" in *'"step": "done"'*|*'"step": "fail"'*) [ -n "$mx" ] && break ;; esac
      sleep 0.2
  done ) & MEMW=$!
res="$(pl_cmd A move '{}')" || pl_fail "переезд кнопкой не начат: $res"
pl_wait A 's["out"].get("state") != "leaving" or s["out"].get("step") != "pkg"' 240 "A вышла из шага pkg" || exit 1
[ "$(pl_q A 's["out"].get("state")')" = leaving ] || pl_fail "переезд сорван на шаге pkg: $(pl_q A 's["out"]')"
st="$(pl_open_wait 300)"
wait "$MEMW" 2>/dev/null
case "$st" in *'"step": "done"'*) ;; *) pl_fail "процесс открытия с базой $db_size байт не дошёл до done: $st · $(tail -n3 /run/awg-bot/rsv/rsvopen.err 2>/dev/null)" ;; esac
[ "$(oom_count)" = "$oom0" ] || pl_fail "процесс открытия убит OOM: $(dmesg | grep 'oom-kill.*awg-bot-rsvopen' | tail -n1)"
read -r mx peak < "$PL/open.mem"
want="$(rsvz "rsvzone.open_memory_mb($pkg_size, $db_size)")"
[ "$mx" = "$(( want * 1024 * 1024 ))" ] \
    || pl_fail "MemoryMax процесса открытия $mx, а по формуле (пакет $pkg_size, база $db_size) — $want МБ"

# ── 4. хранилище и потолок гостя по базе ─────────────────────────────────────────────────────────
systemctl stop awg-bot-guest.service 2>/dev/null
gdb="$(stat -c %s "$RUN/data/bot.db")"
store_want="$(rsvz "rsvzone.store_mb($gdb)")"
store_got="$(mb_of "$RUN")"
[ "$store_got" = "$store_want" ] || pl_fail "хранилище гостя $store_got МБ, по формуле (база $gdb) — $store_want МБ"
[ "$store_got" -gt 32 ] || pl_fail "хранилище гостя не выросло: $store_got МБ"
gmx="$(systemctl show -p MemoryMax --value awg-bot-guest.service)"
gwant="$(rsvz "rsvzone.guest_memory_mb($gdb)")"
[ "$gmx" = "$(( gwant * 1024 * 1024 ))" ] || pl_fail "MemoryMax юнита гостя $gmx, по формуле (база $gdb) — $gwant МБ"

# ── 5. гость с большой базой ─────────────────────────────────────────────────────────────────────
pl_guest_start
pl_wait A 's["out"].get("step") in ("record", "release", "released") or s["out"].get("state") == "away"' 150 "A на шаге записи" || exit 1
dns_set 198.51.100.7
pl_wait A 's["out"].get("state") == "away" and s["jrn"]["role"] == "follower"' 240 "A в отъезде и следует" || exit 1
pl_wait G 's.get("role") == "holder" and s.get("holder") == "self"' 180 "гость с большой базой — держатель эпохи" || exit 1
echo "ok (база $((db_size / 1048576)) МБ, пакет $((pkg_size / 1048576)) МБ; открытие MemoryMax ${want} МБ, пик $((peak / 1048576)) МБ; хранилище ${store_got} МБ, гость MemoryMax ${gwant} МБ)"
