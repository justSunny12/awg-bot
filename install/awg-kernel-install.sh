#!/usr/bin/env bash
#
# awg-kernel-install.sh — AmneziaWG ровно той версии, что прибита к поставке
# (install/awg.lock). Модуль ядра и amneziawg-tools, ТОЛЬКО из исходников.
#
# ЗАЧЕМ НЕ PPA. Пакет из репозитория обновляется apt'ом вместе со всем
# остальным, и однажды ядро awg уезжает вперёд бота: клиенты прежнего
# поколения перестают ходить, а никто не понимает почему. Версия awg —
# часть поставки, и меняет её только поставка (docs/ROADMAP.md, п.8).
#
# DKMS-ДЕРЕВО ПОД ВЕРСИЕЙ. Апстримный dkms.conf объявляет PACKAGE_VERSION
# "1.0.0" для всех сборок, из-за чего дерево одно и новая установка затирает
# старую. Здесь дерево кладётся в /usr/src/amneziawg-<версия> с настоящей
# версией в dkms.conf: сборки сосуществуют, откат на прежний тег возможен, а
# при смене ядра через apt DKMS пересобирает модуль сам. Сборка — явным
# перебором ВСЕХ установленных ядер: `dkms install` без -k ставит только под
# текущее, и загрузка в другое ядро оставила бы хост без awg.
#
# ТОЖДЕСТВО ВЕДЁТСЯ ПО ТЕГУ. Апстрим не бампает version.h: теги v3.1.20260812,
# …0827, …0828, …0906 все рапортуют «3.1.20260812». Сверять установленное по
# `modinfo -F version` значит не отличать их совсем — и молча пропускать смену
# тега в манифесте, ради которой всё и затевалось. Поэтому собранный тег
# пишется в состояние хоста (/etc/awg-bot/awg.state) и сверяется с манифестом,
# а строка версии остаётся для показа.
#
# «Работает ли то, что собрано» — по srcversion: это хеш исходников, и у него
# разные значения там, где строка версии одинакова. Сравнивается загруженный
# (/sys/module/amneziawg/srcversion) с лежащим на диске (modinfo -F srcversion).
#
# ИДЕМПОТЕНТНО: тег совпал с манифестом — ничего не трогаем.
#
# Использование (root):
#   awg-kernel-install.sh status      что стоит, что в манифесте; код 3 — расходятся
#   awg-kernel-install.sh install     довести до манифеста (ставит зависимости apt)
#   awg-kernel-install.sh reload      загрузить установленный модуль вместо
#                                     работающего: ВСЕ awg-интерфейсы вниз и вверх
#   awg-kernel-install.sh prune       убрать сборки ПРЕЖНИХ тегов из DKMS и /usr/src;
#                                     отказывается, пока работает не то, что собрано
#   awg-kernel-install.sh plan        показать, что сделал бы install
#
# Окружение: AWG_LOCK — путь к манифесту (по умолчанию рядом со скриптом).
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AWG_LOCK="${AWG_LOCK:-$SELF_DIR/awg.lock}"
SRC_CACHE="${SRC_CACHE:-/var/lib/awg-bot/awg-src}"     # тарболы и распакованные тулзы
DKMS_SRC_ROOT="${DKMS_SRC_ROOT:-/usr/src}"
MODULE="amneziawg"

c_info=$'\033[0;36m'; c_ok=$'\033[0;32m'; c_warn=$'\033[0;33m'; c_err=$'\033[0;31m'; c_off=$'\033[0m'
log()  { printf '%s[awg]%s %s\n' "$c_info" "$c_off" "$*"; }
ok()   { printf '%s[awg]%s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '%s[awg:!]%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
die()  { printf '%s[awg:ОШИБКА]%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }

MODE="${1:-status}"
case "$MODE" in
    status|install|reload|plan|prune) ;;
    -h|--help|help) sed -n '2,34p' "$0"; exit 0 ;;
    *) die "неизвестная команда: $MODE (status | install | reload | prune | plan)" ;;
esac
PLAN=0; [[ "$MODE" == "plan" ]] && PLAN=1
run() {  # run CMD… — в plan только печатает
    # Трассировка идёт в stderr: fetch() возвращает путь к файлу через stdout,
    # и строка «$ curl …» попала бы в этот путь — tar получил бы мусор.
    if [[ "$PLAN" -eq 1 ]]; then printf '  would: %s\n' "$*" >&2; else printf '  $ %s\n' "$*" >&2; "$@"; fi
}

# ── манифест ─────────────────────────────────────────────────────────────────
# Значения приезжают из awg.lock; объявляем их пустыми заранее, чтобы опечатка
# в манифесте давала внятный отказ ниже, а не пустую строку в середине сборки.
AWG_GENERATION=""; AWG_PROTOCOL_ID=""
AWG_MODULE_TAG=""; AWG_MODULE_VERSION=""; AWG_MODULE_URL=""; AWG_MODULE_SHA256=""
AWG_TOOLS_TAG=""; AWG_TOOLS_VERSION=""; AWG_TOOLS_URL=""; AWG_TOOLS_SHA256=""
[[ -f "$AWG_LOCK" ]] || die "нет манифеста $AWG_LOCK"
# shellcheck disable=SC1090
. "$AWG_LOCK"
for v in AWG_GENERATION AWG_MODULE_TAG AWG_MODULE_VERSION AWG_MODULE_URL \
         AWG_MODULE_SHA256 AWG_TOOLS_TAG AWG_TOOLS_VERSION AWG_TOOLS_URL \
         AWG_TOOLS_SHA256; do
    [[ -n "${!v:-}" ]] || die "в манифесте нет $v"
done

# ── что стоит ────────────────────────────────────────────────────────────────
AWG_STATE="${AWG_STATE:-/etc/awg-bot/awg.state}"
_KEY_MODULE_TAG="AWG_MODULE_TAG_BUILT"
_KEY_TOOLS_TAG="AWG_TOOLS_TAG_BUILT"

state_get() {  # state_get KEY → значение или пусто
    [[ -f "$AWG_STATE" ]] || return 0
    sed -nE "s/^$1=(.*)$/\1/p" "$AWG_STATE" | head -n1
}
state_set() {  # state_set KEY VALUE
    mkdir -p "$(dirname "$AWG_STATE")"
    [[ -f "$AWG_STATE" ]] || printf '%s\n%s\n' \
        "# awg-bot: состояние ядра AmneziaWG на этом хосте. Файл ведут установщик" \
        "# и финал переезда профилей; руками править незачем." > "$AWG_STATE"
    if grep -qE "^$1=" "$AWG_STATE"; then
        sed -i -E "s|^$1=.*|$1=$2|" "$AWG_STATE"
    else
        printf '%s=%s\n' "$1" "$2" >> "$AWG_STATE"
    fi
}

installed_module() {   # версия модуля, доступного ТЕКУЩЕМУ ядру (на диске)
    modinfo -F version "$MODULE" 2>/dev/null | head -n1 || true
}
loaded_module() {      # версия работающего модуля (пусто — не загружен)
    cat "/sys/module/$MODULE/version" 2>/dev/null || true
}
installed_src() {      # srcversion модуля на диске — хеш ИСХОДНИКОВ
    modinfo -F srcversion "$MODULE" 2>/dev/null | head -n1 || true
}
loaded_src() {         # srcversion работающего модуля
    cat "/sys/module/$MODULE/srcversion" 2>/dev/null || true
}
built_tag() {          # тег, из которого собран модуль на этом хосте
    state_get "$_KEY_MODULE_TAG"
}
installed_tools() {    # "amneziawg-tools v3.1.20260812 - …" → 3.1.20260812
    command -v awg >/dev/null 2>&1 || { printf ''; return 0; }
    awg --version 2>/dev/null | sed -nE 's/^amneziawg-tools v?([^ ]+).*/\1/p' | head -n1
}
kernels_with_headers() {  # все установленные ядра, у которых есть каталог сборки
    local d
    for d in /lib/modules/*/; do
        d="${d%/}"; [[ -e "$d/build/Makefile" ]] && basename "$d"
    done
}

cur_mod="$(installed_module)"; cur_loaded="$(loaded_module)"; cur_tools="$(installed_tools)"
cur_tag="$(built_tag)"; cur_tools_tag="$(state_get "$_KEY_TOOLS_TAG")"
need_mod=0; need_tools=0
# Модуль: сверяем ТЕГ. Нет записи о собранном теге (хост собирали руками или до
# появления состояния) — считаем, что стоит не то, и пересобираем: другого
# способа узнать правду нет, а version.h у всех тегов одинаков.
[[ -n "$cur_mod" && "$cur_tag" == "$AWG_MODULE_TAG" ]] || need_mod=1
# Тулзы печатают свою версию честно, но тег записываем тем же образом.
if [[ -n "$cur_tools_tag" ]]; then
    [[ "$cur_tools_tag" == "$AWG_TOOLS_TAG" ]] || need_tools=1
else
    [[ "$cur_tools" == "$AWG_TOOLS_VERSION" ]] || need_tools=1
fi

if [[ "$MODE" == "status" ]]; then
    printf 'манифест          : модуль %s, тулзы %s, поколение %s\n' \
        "$AWG_MODULE_TAG" "$AWG_TOOLS_TAG" "$AWG_GENERATION"
    printf 'собран из тега    : %s\n' "${cur_tag:-неизвестно (собирали не мы)}"
    printf 'модуль на диске   : %s (srcversion %s)\n' "${cur_mod:-нет}" "$(installed_src)"
    printf 'модуль загружен   : %s (srcversion %s)\n' "${cur_loaded:-нет}" "$(loaded_src)"
    printf 'amneziawg-tools   : %s\n' "${cur_tools:-нет}"
    printf 'ядра с заголовками: %s\n' "$(kernels_with_headers | tr '\n' ' ')"
    printf 'примечание        : апстрим не бампает version.h — строка версии у разных тегов одна\n'
    if [[ "$need_mod" -eq 0 && "$need_tools" -eq 0 ]]; then
        # Работает ли то, что собрано, — по srcversion: строки версий совпали
        # бы и у разных исходников.
        if [[ -n "$(loaded_src)" && "$(loaded_src)" != "$(installed_src)" ]]; then
            printf 'итог              : собрано по манифесту, но работает модуль других исходников — нужен reload или перезагрузка\n'
            exit 3
        fi
        printf 'итог              : совпадает с манифестом\n'; exit 0
    fi
    printf 'итог              : РАСХОДИТСЯ с манифестом — awg-bot awg install\n'; exit 3
fi

# plan только печатает — root ему не нужен: посмотреть, что будет сделано,
# полезно и до того, как решаешься это делать.
[[ "$PLAN" -eq 1 || "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root"

# ── reload: подменить работающий модуль установленным ────────────────────────
if [[ "$MODE" == "reload" ]]; then
    [[ "$need_mod" -eq 0 ]] || die "на диске не тег манифеста (собран из ${cur_tag:-неизвестного}) — сначала install"
    if [[ -n "$(loaded_src)" && "$(loaded_src)" == "$(installed_src)" ]]; then
        ok "работают те же исходники (srcversion $(loaded_src)) — reload не нужен"; exit 0
    fi
    ifaces="$(awg show interfaces 2>/dev/null | tr ' ' '\n' | grep -v '^$' || true)"
    # Все интерфейсы вниз — иначе rmmod откажет. Порядок подъёма — тот же, что
    # был: у клиентского и линка свои PostUp, и они друг от друга не зависят.
    for i in $ifaces; do run awg-quick down "$i" || warn "$i: не опущен"; done
    if [[ -n "$cur_loaded" ]]; then run rmmod "$MODULE" || die "rmmod: модуль занят — что-то держит интерфейс"; fi
    run modprobe "$MODULE" || die "modprobe $MODULE не прошёл"
    for i in $ifaces; do run awg-quick up "$i" || warn "$i: не поднят — awg-quick up $i"; done
    ok "работает $(loaded_module); интерфейсы: ${ifaces:-нет}"
    exit 0
fi

# ── prune: прежние сборки долой ──────────────────────────────────────────────
# Дерево прежнего тега держим ровно до тех пор, пока оно нужно для отката: пока
# новый модуль не заработал (совместимая смена) или пока не завершён переезд
# профилей (смена поколения). Дальше это мусор в /usr/src и в DKMS, который
# ещё и пересобирается при каждом обновлении ядра системы.
if [[ "$MODE" == "prune" ]]; then
    [[ -n "$cur_tag" ]] || die "неизвестно, что собрано (нет записи о теге) — сначала install"
    [[ "$need_mod" -eq 0 ]] || die "на диске не тег манифеста — сначала install"
    if [[ -n "$(loaded_src)" && "$(loaded_src)" != "$(installed_src)" ]]; then
        die "работает модуль прежних исходников — сначала reload (или перезагрузка), потом prune"
    fi
    keep="${AWG_MODULE_TAG#v}"
    removed=0
    # Версии DKMS: все, кроме текущего тега. Сюда попадает и апстримное
    # «1.0.0» с хостов, которые собирали руками.
    for v in $(dkms status -m "$MODULE" 2>/dev/null | sed -nE "s/^$MODULE[/, ]+([^,: ]+).*/\1/p" | sort -u); do
        [[ "$v" == "$keep" ]] && continue
        run dkms remove -m "$MODULE" -v "$v" --all || warn "dkms remove $v не прошёл"
        removed=$((removed + 1))
    done
    for d in "$DKMS_SRC_ROOT/$MODULE"-*; do
        [[ -d "$d" ]] || continue
        [[ "$d" == "$DKMS_SRC_ROOT/$MODULE-$keep" ]] && continue
        run rm -rf "$d"
        removed=$((removed + 1))
    done
    # Кэш тарболов прежних тегов — тоже
    for f in "$SRC_CACHE"/amneziawg-linux-kernel-module-*.tar.gz; do
        [[ -f "$f" ]] || continue
        [[ "$f" == *"-$keep.tar.gz" ]] && continue
        run rm -f "$f"
    done
    [[ "$removed" -gt 0 ]] && run depmod -a
    ok "прежних сборок убрано: $removed; остаётся $AWG_MODULE_TAG"
    exit 0
fi

# ── install / plan ───────────────────────────────────────────────────────────
if [[ "$need_mod" -eq 0 && "$need_tools" -eq 0 ]]; then
    ok "AmneziaWG $AWG_MODULE_TAG уже собран — ничего не делаю"
    if [[ -n "$(loaded_src)" && "$(loaded_src)" != "$(installed_src)" ]]; then
        warn "работает модуль других исходников: применить — awg-bot awg reload (интерфейсы лягут на секунды) или перезагрузка"
    fi
    exit 0
fi
log "манифест: модуль $AWG_MODULE_TAG, тулзы $AWG_TOOLS_TAG; собрано из: ${cur_tag:-неизвестно}"

# 1) зависимости сборки — только apt-семейство; на другом дистрибутиве
#    честный отказ со списком, а не полусобранное состояние
step_deps() {
    if ! command -v apt-get >/dev/null 2>&1; then
        # plan обязан показывать план, а не отказ: «что будет сделано» спрашивают
        # в том числе там, где делать не собираются.
        [[ "$PLAN" -eq 1 ]] && { printf '  would: поставить dkms, build-essential, заголовки ядра, curl, tar\n' >&2; return 0; }
        die "не apt-дистрибутив: поставь вручную dkms, build-essential, заголовки ядра, curl, tar и повтори"
    fi
    local pkgs=(dkms build-essential curl ca-certificates tar make gcc pkg-config)
    local krel; krel="$(uname -r)"
    if [[ ! -e "/lib/modules/$krel/build/Makefile" ]]; then
        # Ubuntu/Debian: linux-headers-<release>; Raspberry Pi OS: заголовки
        # идут пакетом на «вкус» ядра (…+rpt-rpi-v8 → linux-headers-rpi-v8),
        # у старых образов — raspberrypi-kernel-headers.
        local flavour="${krel##*+rpt-}"
        local cand
        for cand in "linux-headers-$krel" "linux-headers-$flavour" raspberrypi-kernel-headers; do
            if apt-cache show "$cand" >/dev/null 2>&1; then pkgs+=("$cand"); break; fi
        done
    fi
    log "зависимости: ${pkgs[*]}"
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${pkgs[@]}"
    [[ "$PLAN" -eq 1 ]] || [[ -e "/lib/modules/$krel/build/Makefile" ]] \
        || die "нет заголовков для ядра $krel (/lib/modules/$krel/build) — модуль не собрать; поставь их и повтори"
}

# 2) тарбол по манифесту, sha256 обязателен
fetch() {   # fetch NAME URL SHA → путь к файлу
    local name="$1" url="$2" sha="$3" f="$SRC_CACHE/$1.tar.gz"
    mkdir -p "$SRC_CACHE"; chmod 700 "$SRC_CACHE"
    if [[ ! -f "$f" ]] || ! printf '%s  %s\n' "$sha" "$f" | sha256sum -c --quiet - >/dev/null 2>&1; then
        run curl -fsSL --retry 3 -o "$f.part" "$url"
        [[ "$PLAN" -eq 1 ]] && { printf '%s' "$f"; return 0; }
        printf '%s  %s\n' "$sha" "$f.part" | sha256sum -c --quiet - \
            || { rm -f "$f.part"; die "$name: sha256 не совпал с манифестом — файл не тот, ставить не буду"; }
        mv -f "$f.part" "$f"
    fi
    printf '%s' "$f"
}

# 3) amneziawg-tools: make из src, установка в /usr (awg, awg-quick, юнит awg-quick@)
step_tools() {
    local f d
    f="$(fetch "amneziawg-tools-${AWG_TOOLS_TAG#v}" "$AWG_TOOLS_URL" "$AWG_TOOLS_SHA256")"
    d="$SRC_CACHE/amneziawg-tools-${AWG_TOOLS_TAG#v}"
    run rm -rf "$d"; run mkdir -p "$d"
    run tar xzf "$f" -C "$d" --strip-components=1
    run make -C "$d/src" -j"$(nproc)"
    run make -C "$d/src" install
    [[ "$PLAN" -eq 1 ]] || hash -r
    [[ "$PLAN" -eq 1 ]] || [[ "$(installed_tools)" == "$AWG_TOOLS_VERSION" ]] \
        || die "после установки awg --version даёт '$(installed_tools)', ждали $AWG_TOOLS_VERSION"
    [[ "$PLAN" -eq 1 ]] || state_set "$_KEY_TOOLS_TAG" "$AWG_TOOLS_TAG"
    run systemctl daemon-reload
}

# 4) модуль: DKMS-дерево под версией, сборка под каждое ядро
step_module() {
    # Каталог дерева — по ТЕГУ: у разных тегов одинаковый version.h, и дерево
    # «по версии» они делили бы между собой, затирая друг друга.
    local mtag="${AWG_MODULE_TAG#v}"
    local f tmp tree="$DKMS_SRC_ROOT/$MODULE-$mtag" k
    f="$(fetch "amneziawg-linux-kernel-module-$mtag" "$AWG_MODULE_URL" "$AWG_MODULE_SHA256")"
    tmp="$SRC_CACHE/module-$mtag.unpack"
    run rm -rf "$tmp"; run mkdir -p "$tmp"
    run tar xzf "$f" -C "$tmp" --strip-components=1
    # Дерево DKMS — апстримной целью (кладёт только исходники, без тестов),
    # но в СВОЙ каталог; PACKAGE_VERSION — настоящая версия, а не 1.0.0.
    run rm -rf "$tree"
    run make -C "$tmp/src" dkms-install DKMSDIR="$tree"
    if [[ "$PLAN" -eq 0 ]]; then
        sed -i -E "s|^PACKAGE_VERSION=.*|PACKAGE_VERSION=\"$mtag\"|" "$tree/dkms.conf"
        grep -q "^PACKAGE_VERSION=\"$mtag\"" "$tree/dkms.conf" || die "dkms.conf: версия не выставлена"
    fi
    run rm -rf "$tmp"
    if ! dkms status -m "$MODULE" -v "$mtag" 2>/dev/null | grep -q .; then
        run dkms add -m "$MODULE" -v "$mtag"
    fi
    local built=0
    for k in $(kernels_with_headers); do
        # Прежняя версия под этим ядром снимается (uninstall, не remove: её
        # дерево остаётся для отката), иначе два .ko претендуют на одно место.
        local old
        for old in $(dkms status -m "$MODULE" -k "$k" 2>/dev/null | sed -nE "s/^$MODULE[/, ]+([^,: ]+).*installed.*/\1/p"); do
            [[ "$old" == "$mtag" ]] && continue
            run dkms uninstall -m "$MODULE" -v "$old" -k "$k" || warn "$old под $k не снят"
        done
        run dkms build -m "$MODULE" -v "$mtag" -k "$k" || die "сборка модуля под ядро $k не прошла — смотри /var/lib/dkms/$MODULE/$mtag/build/make.log"
        run dkms install -m "$MODULE" -v "$mtag" -k "$k" --force || die "установка модуля под ядро $k не прошла"
        built=$((built + 1))
    done
    [[ "$built" -gt 0 || "$PLAN" -eq 1 ]] || die "ни одного ядра с заголовками — собирать не под что"
    run depmod -a
    [[ "$PLAN" -eq 1 ]] || [[ -n "$(installed_module)" ]] \
        || die "после установки modinfo не видит модуль вовсе"
    # Тег собранного — наш единственный честный след: version.h у тегов общий.
    [[ "$PLAN" -eq 1 ]] || state_set "$_KEY_MODULE_TAG" "$AWG_MODULE_TAG"
}

step_deps
[[ "$need_tools" -eq 1 ]] && step_tools
[[ "$need_mod"   -eq 1 ]] && step_module
[[ "$PLAN" -eq 1 ]] && { log "план показан, ничего не менялось"; exit 0; }

cur_loaded="$(loaded_module)"
if [[ -z "$cur_loaded" ]]; then
    # ничего не работает (чистый хост) — проверяем, что модуль вообще грузится
    modprobe "$MODULE" || die "modprobe $MODULE не прошёл после сборки"
    ok "AmneziaWG $AWG_MODULE_TAG установлен и загружен; тулзы $AWG_TOOLS_TAG"
elif [[ "$(loaded_src)" != "$(installed_src)" ]]; then
    ok "AmneziaWG $AWG_MODULE_TAG установлен; работает пока модуль прежних исходников"
    warn "применить: awg-bot awg reload (все awg-интерфейсы лягут на секунды) или перезагрузка"
else
    ok "AmneziaWG $AWG_MODULE_TAG установлен; он же и работает"
fi
