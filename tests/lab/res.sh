#!/bin/bash
#
# res.sh — проба ресурсов ВМ стенда: раз в 5 с строка «время · занято памяти, МБ (MemTotal −
# MemAvailable) · занятость CPU, % всех ядер за 5 с» в файл; par.sh запускает её на время прогона
# и сводит пик памяти, средний и 95-й процентиль CPU — по ним подбирается размер ВМ.
#
#   tests/lab/res.sh <файл>          (до kill)
#   tests/lab/res.sh --sum <файл>    сводка строкой
set -u
if [ "${1:-}" = --sum ]; then
    sort -k3 -n "$2" 2>/dev/null | awk -v c="$(nproc)" -v m="$(awk '/^MemTotal:/{print int($2 / 1024)}' /proc/meminfo)" '
        { a[NR] = $3; s += $3; if ($2 > pk) pk = $2 }
        END { if (!NR) exit; k = int(NR * 0.95); if (k < 1) k = 1
              printf "ресурсы: память пик %d из %d МБ, CPU среднее %d%%, 95%% — %d%% (из %d ядер)\n", pk, m, s / NR, a[k], c }'
    # проба раз в 5 с по часам ВМ: разрыв дольше 30 с — ВМ стояла на паузе (сон Mac), сроки сценариев поплыли
    awk 'NR > 1 && $1 - p > 30 { n++; t += $1 - p } { p = $1 } END { if (n) printf "ВНИМАНИЕ: ВМ стояла на паузе %d раз, всего %d с (сон Mac?) — результат недостоверен\n", n, t }' "$2" 2>/dev/null
    exit 0
fi
out="$1"; : > "$out"
read -r _ u n s i w q sq st _ < /proc/stat; prev_busy=$((u + n + s + q + sq + st)); prev_all=$((prev_busy + i + w))
while sleep 5; do
    read -r _ u n s i w q sq st _ < /proc/stat; busy=$((u + n + s + q + sq + st)); all=$((busy + i + w))
    mem=$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{print int((t - a) / 1024)}' /proc/meminfo)
    cpu=$(( (busy - prev_busy) * 100 / ((all - prev_all) > 0 ? (all - prev_all) : 1) ))
    printf '%s %s %s\n' "$(date +%s)" "$mem" "$cpu" >> "$out"
    prev_busy=$busy; prev_all=$all
done
