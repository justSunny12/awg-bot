#!/bin/bash
#
# run.sh — прогон стенда: самопроверка зоны, подъём топологии, сценарии, затем
# существующий набор тестов в той же ВМ (GNU sed и coreutils — как на хостах).
#
#   sudo -E tests/lab/run.sh [--fast] [--no-suite | --no-scenarios] [--only "<сценарий> …"]
#
# --fast — быстрый уровень (LAB_FAST=1): цифры кворума и «здоров» сжаты настройками узлов
# (tests/lab/pairnode.LAB_FAST_DIGITS), пределы проверок сценариев — от них; без флага — умолчания
# продукта (полный уровень, перед предрелизной сборкой). --only — сценарий или список через пробел
# или запятую. --no-scenarios — только набор тестов (его и держит блокировка полных прогонов);
# сценарии на нескольких ВМ сразу — tests/lab/par.sh с Mac.
#
# Сценарии — tests/lab/scenarios/*.sh, по одному на случай аварии или перехода;
# каждый сам поднимает, что ему нужно, через lab.sh/net.sh и печатает
# `PASS <имя>` или `FAIL <имя>: <почему>`. Набор тестов гоняется из venv
# `~/venv` пользователя, под которым запущен sudo (SUDO_USER), с TMPDIR вне tmpfs.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SUITE=1; SCEN=1; ONLY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --fast) export LAB_FAST=1 ;;
        --no-suite) SUITE=0 ;;
        --no-scenarios) SCEN=0 ;;
        --only) ONLY=" ${2//,/ } "; shift ;;
        *) printf 'использование: %s [--fast] [--no-suite | --no-scenarios] [--only "<сценарий> …"]\n' "$0"; exit 2 ;;
    esac
    shift
done
[ "$(id -u)" -eq 0 ] || { printf 'нужен root (sudo -E)\n' >&2; exit 1; }

fail=0; passed=0
if [ "$SCEN" -eq 1 ]; then
    printf '=== уровень: %s\n' "$([ "${LAB_FAST:-}" = 1 ] && echo 'быстрый (LAB_FAST=1)' || echo 'полный (умолчания продукта)')"
    printf '=== самопроверка зоны\n'
    if bash "$ROOT/install/reserve-zone-setup.sh" --selftest; then passed=$((passed + 1)); else printf 'FAIL selftest\n'; fail=$((fail + 1)); fi

    printf '=== топология\n'
    bash "$HERE/lab.sh" up >/dev/null && bash "$HERE/lab.sh" status

    printf '=== сценарии\n'
    for s in "$HERE"/scenarios/*.sh; do
        [ -f "$s" ] || continue
        name="$(basename "$s" .sh)"
        [ -z "$ONLY" ] || case "$ONLY" in *" $name "*) ;; *) continue ;; esac
        bash "$HERE/net.sh" heal >/dev/null 2>&1
        # итог пройденного — последняя строка вывода сценария (замеры времени, «ok (…)»), без строк bash о снятых
        # фоновых процессах (trap снимает узлы kill -9)
        t_s=$(date +%s)
        if out="$(bash "$s" 2>&1)"; then printf 'PASS %s (%d мин): %s\n' "$name" $(( ($(date +%s) - t_s + 30) / 60 )) "$(printf '%s\n' "$out" | grep -v ' Killed  ' | tail -n1)"; passed=$((passed + 1))
        else printf 'FAIL %s (%d мин)\n%s\n' "$name" $(( ($(date +%s) - t_s + 30) / 60 )) "$out"; fail=$((fail + 1)); fi
    done
    bash "$HERE/net.sh" heal >/dev/null 2>&1
    bash "$HERE/lab.sh" down >/dev/null
fi

if [ "$SUITE" -eq 1 ]; then
    printf '=== набор тестов\n'
    user="${SUDO_USER:-$(id -un)}"
    home="$(getent passwd "$user" | cut -d: -f6)"
    mkdir -p /var/tmp/awglab-tmp && chown "$user" /var/tmp/awglab-tmp
    if ! su "$user" -c "cd '$ROOT' && TMPDIR=/var/tmp/awglab-tmp '$home/venv/bin/python' -m pytest -q -p no:cacheprovider" ; then
        fail=$((fail + 1))
    else
        passed=$((passed + 1))
    fi
fi
printf '=== итог: пройдено %s, провалено %s\n' "$passed" "$fail"
[ "$fail" -eq 0 ]
