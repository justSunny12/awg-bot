#!/bin/bash
#
# run.sh — прогон стенда: самопроверка зоны, подъём топологии, сценарии, затем
# существующий набор тестов в той же ВМ (GNU sed и coreutils — как на хостах).
#
#   sudo -E tests/lab/run.sh [--no-suite] [--only <сценарий>]
#
# Сценарии — tests/lab/scenarios/*.sh, по одному на случай аварии или перехода;
# каждый сам поднимает, что ему нужно, через lab.sh/net.sh и печатает
# `PASS <имя>` или `FAIL <имя>: <почему>`. Набор тестов гоняется из venv
# `~/venv` пользователя, под которым запущен sudo (SUDO_USER), с TMPDIR вне tmpfs.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SUITE=1; ONLY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-suite) SUITE=0 ;;
        --only) ONLY="$2"; shift ;;
        *) printf 'использование: %s [--no-suite] [--only <сценарий>]\n' "$0"; exit 2 ;;
    esac
    shift
done
[ "$(id -u)" -eq 0 ] || { printf 'нужен root (sudo -E)\n' >&2; exit 1; }

fail=0; passed=0
printf '=== самопроверка зоны\n'
if bash "$ROOT/install/reserve-zone-setup.sh" --selftest; then passed=$((passed + 1)); else printf 'FAIL selftest\n'; fail=$((fail + 1)); fi

printf '=== топология\n'
bash "$HERE/lab.sh" up >/dev/null && bash "$HERE/lab.sh" status

printf '=== сценарии\n'
for s in "$HERE"/scenarios/*.sh; do
    [ -f "$s" ] || continue
    name="$(basename "$s" .sh)"
    [ -z "$ONLY" ] || [ "$name" = "$ONLY" ] || continue
    bash "$HERE/net.sh" heal >/dev/null 2>&1
    # итог пройденного — последняя строка вывода сценария (замеры времени, «ok (…)»), без строк bash о снятых
    # фоновых процессах (trap снимает узлы kill -9)
    if out="$(bash "$s" 2>&1)"; then printf 'PASS %s: %s\n' "$name" "$(printf '%s\n' "$out" | grep -v ' Killed  ' | tail -n1)"; passed=$((passed + 1))
    else printf 'FAIL %s\n%s\n' "$name" "$out"; fail=$((fail + 1)); fi
done
bash "$HERE/net.sh" heal >/dev/null 2>&1
bash "$HERE/lab.sh" down >/dev/null

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
