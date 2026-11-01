#!/bin/bash
#
# par.sh — сценарии стенда на нескольких ВМ сразу (запуск с Mac): каждая ВМ — свой
# стенд целиком (топология, пары, /var/lib/awglab-*), сценарии делятся по весу —
# тяжёлый первым на наименее загруженную ВМ. Сценарии почти всё время ждут сроков
# протокола, ядра ВМ простаивают — две ВМ вдвое короче одной.
#
#   tests/lab/par.sh [--fast] [<сценарий> …]
#
# --fast — быстрый уровень (run.sh --fast). Без сценариев — все. ВМ — LAB_VMS (по
# умолчанию все ВМ Lima с именем awg-lab*), каталог репозитория смонтирован в них по
# тому же пути. Вывод ВМ — /var/tmp/lab-par.txt в ней; здесь — строки PASS/FAIL с
# именем ВМ и итог. Набор тестов не гоняется: он — run.sh --no-scenarios (под
# блокировкой полных прогонов, в одной ВМ). LAB_PAR_DRY=1 — только раскладка по ВМ.
#
# Остановленные ВМ par.sh поднимает сам и после прогона гасит (память Mac — назад; уже
# запущенные не трогает; LAB_KEEP_VMS=1 — не гасить). На время прогона в каждой ВМ —
# проба ресурсов (res.sh, /var/tmp/lab-res.txt): в конце — пик памяти, средний и 95-й
# процентиль CPU по ВМ, по ним подбирается размер ВМ. Время по шагам сценариев — в ВМ,
# /var/tmp/lab-steps-<сценарий>.txt (pairlab.sh, pl_mark). На время прогона Mac не засыпает
# (caffeinate -i): сон посреди сценария сдвигает сроки и даёт ложные провалы.
set -u
# Mac не засыпает, пока идёт прогон: сон посреди сценария — поплывшие сроки и ложные провалы
if [ -z "${LAB_PAR_AWAKE:-}" ] && command -v caffeinate >/dev/null 2>&1; then
    LAB_PAR_AWAKE=1 exec caffeinate -i "$0" "$@"
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
FAST=""
[ "${1:-}" = "--fast" ] && { FAST="--fast"; shift; }
read -r -a VMS <<< "${LAB_VMS:-$(limactl list -q 2>/dev/null | grep '^awg-lab' | tr '\n' ' ')}"
[ "${#VMS[@]}" -gt 0 ] || { printf 'ВМ стенда нет (LAB_VMS или limactl list: awg-lab*)\n' >&2; exit 2; }

# вес — минуты сценария на уровне: полный / быстрый (быстрый — замер 01.11.2026, полный — прогоны на одной
# ВМ до деления s06; неизвестный — 5)
weight() {
    local f
    case "$1" in
        s06_quorum_move) f="18 15" ;; s06b_quorum_move) f="15 14" ;; s07_quorum_tie) f="25 11" ;;
        s08_rf_transit) f="10 11" ;; s09_tg_split) f="10 5" ;; s11_c9_wait) f="10 4" ;; s10_force_live_guest) f="10 3" ;;
        s06_return_tail) f="8 4" ;; s12_relay_flush) f="8 3" ;; s05_quorum_probes) f="6 2" ;; s05_journal) f="5 4" ;;
        s13_big_base) f="5 1" ;; s03_move|s04_open_sandbox) f="2 1" ;; s00_topology|s01_zone|s02_guest) f="1 1" ;; *) f="5 5" ;;
    esac
    set -- $f
    [ -n "$FAST" ] && echo "$2" || echo "$1"
}

if [ $# -gt 0 ]; then names=("$@")
else names=(); for s in "$HERE"/scenarios/*.sh; do names+=("$(basename "$s" .sh)"); done; fi
load=(); part=()                                          # по индексу ВМ: bash 3.2 Mac без ассоциативных массивов
for i in "${!VMS[@]}"; do load[i]=0; part[i]=""; done
while read -r w n; do
    best=0; for i in "${!VMS[@]}"; do [ "${load[i]}" -lt "${load[best]}" ] && best=$i; done
    load[best]=$(( load[best] + w )); part[best]="${part[best]} $n"
done < <(for n in "${names[@]}"; do echo "$(weight "$n") $n"; done | sort -rn)

for i in "${!VMS[@]}"; do
    [ -z "${part[i]}" ] || printf '%s (~%s мин):%s\n' "${VMS[i]}" "${load[i]}" "${part[i]}"
done
[ -z "${LAB_PAR_DRY:-}" ] || exit 0

started=()
for i in "${!VMS[@]}"; do
    [ -n "${part[i]}" ] || continue
    st="$(limactl list --format '{{.Status}}' "${VMS[i]}" 2>/dev/null)"
    [ "$st" = Running ] && continue
    limactl start "${VMS[i]}" >/dev/null 2>&1 || { printf 'ВМ %s не поднялась\n' "${VMS[i]}" >&2; exit 2; }
    started+=("${VMS[i]}")
done
pids=()
for i in "${!VMS[@]}"; do
    [ -n "${part[i]}" ] || continue
    limactl shell "${VMS[i]}" -- bash -c "sudo rm -rf /var/lib/awglab-pair /var/lib/awglab-quo; cd '$ROOT' && \
        { tests/lab/res.sh /var/tmp/lab-res.txt & r=\$!; \
          sudo -E tests/lab/run.sh $FAST --no-suite --only '${part[i]# }' > /var/tmp/lab-par.txt 2>&1; rc=\$?; kill \$r; exit \$rc; }" &
    pids+=("$!")
done
rc=0
for p in "${pids[@]}"; do wait "$p" || rc=1; done
for i in "${!VMS[@]}"; do
    [ -n "${part[i]}" ] || continue
    limactl shell "${VMS[i]}" -- bash -c 'grep -E "^(PASS|FAIL)|итог" /var/tmp/lab-par.txt' | LC_ALL=C cut -c1-200 | sed "s/^/[${VMS[i]}] /"
    limactl shell "${VMS[i]}" -- bash "$ROOT/tests/lab/res.sh" --sum /var/tmp/lab-res.txt | sed "s/^/[${VMS[i]}] /"
done
if [ "${LAB_KEEP_VMS:-}" != 1 ]; then
    for vm in ${started[@]+"${started[@]}"}; do limactl stop "$vm" >/dev/null 2>&1; done
fi
exit "$rc"
