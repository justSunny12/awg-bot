#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# awg-host-migrate.sh — перенос сервера AmneziaWG из контейнера на хост.
# См. docs/ROADMAP.md, шаг 2.
#
# ЗАЧЕМ. Контейнер работает в своём сетевом namespace, и из этого растёт вся
# конструкция «два плеча» условной маршрутизации: различать клиентов можно
# только ДО чужого MASQUERADE, поэтому исключения из него живут в контейнере, а
# всё остальное на хосте. На хосте MASQUERADE один и наш — механизм исчезает
# целиком вместе с половиной способов молча сломаться.
#
# ЧТО СОХРАНЯЕТСЯ БЕЗ ИЗМЕНЕНИЙ: ключи, обфускация, порт, адреса пиров. Клиенты
# ничего не заметят и переустанавливать ничего не будут. Ключи — обычные
# Curve25519 и PSK, поколения AmneziaWG их не касаются; параметры переносятся
# дословно тем же файлом.
#
# ПОРЯДОК (важен): бот останавливается ПЕРВЫМ. Он пишет в awg0.conf, и копия,
# снятая под его запись, потеряла бы последнего добавленного пира — молча.
#
# ЗАПУСК:
#   sudo sh awg-host-migrate.sh              # показать план, ничего не менять
#   sudo sh awg-host-migrate.sh --apply      # перенести
#   sudo sh awg-host-migrate.sh --rollback   # вернуть контейнер
#
# ПОСЛЕ --apply нужно вручную: runtime: host в app.yaml, обвяз и старт бота —
# скрипт печатает точные команды. Сам он их не делает намеренно: конфиг бота и
# его сервис — не его зона, а переключение режима должно быть осознанным.
# ─────────────────────────────────────────────────────────────────────────────

set -e

CONF_DIR="${CONF_DIR:-/etc/awg-bot/conf}"
AWG_DIR="${AWG_DIR:-/opt/amnezia/awg}"
AWG_IF="${AWG_IF:-awg0}"
QUICK_DIR="${QUICK_DIR:-/etc/amnezia/amneziawg}"
BOT_SERVICE="${BOT_SERVICE:-awg-bot}"
CONTAINER="${CONTAINER:-$(awk -F'"' '/^  container:/{print $2}' "$CONF_DIR/app.yaml" 2>/dev/null)}"
CONTAINER="${CONTAINER:-amnezia-awg2}"

MODE="plan"
case "${1:-}" in
    --apply)    MODE="apply" ;;
    --rollback) MODE="rollback" ;;
    ""|--plan)  MODE="plan" ;;
    -h|--help)  sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
esac

[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n── %s\n' "$*"; }
run()  {
    if [ "$MODE" = "plan" ]; then
        printf '  would: %s\n' "$*"
    else
        printf '  $ %s\n' "$*"
        sh -c "$*"
    fi
}

CONF="$AWG_DIR/$AWG_IF.conf"
LINK="$QUICK_DIR/$AWG_IF.conf"

# ── откат ────────────────────────────────────────────────────────────────────
if [ "$MODE" = "rollback" ]; then
    step "Откат: интерфейс с хоста снимаем, контейнер поднимаем"
    run "awg-quick down $AWG_IF 2>/dev/null || true"
    run "rm -f $LINK"
    run "docker start $CONTAINER"
    say ""
    say "Готово. Верни в $CONF_DIR/app.yaml:  runtime: \"docker\""
    say "и перезапусти бота:  systemctl restart $BOT_SERVICE"
    exit 0
fi

# ── предусловия ──────────────────────────────────────────────────────────────
step "Предусловия"

for t in awg awg-quick; do
    command -v "$t" >/dev/null 2>&1 || {
        say "ОШИБКА: $t не найден на ХОСТЕ."
        say "  Собери amneziawg-tools того же поколения, что модуль ядра."
        exit 1; }
done
say "  awg на хосте: $(awg --version 2>/dev/null | head -1)"

# Версия из строки НЕ читается: amneziawg-tools наследуют базовую версию
# wireguard-tools (v1.0.20210914) и о поколении не говорят ничего. Поколение
# проверяется делом — при подъёме интерфейса, ниже.
if ! lsmod 2>/dev/null | grep -q '^amneziawg'; then
    say "  ВНИМАНИЕ: модуль amneziawg не загружен."
    say "  Без него awg-quick уйдёт в userspace (amneziawg-go), а это заметная"
    say "  задержка и её скачки на каждом пакете. Проверь: modprobe amneziawg"
fi

docker inspect "$CONTAINER" >/dev/null 2>&1 || {
    say "ОШИБКА: контейнер $CONTAINER не найден — переносить нечего."; exit 1; }

if [ -e "$CONF" ] && [ ! -L "$CONF" ]; then
    say "  ВНИМАНИЕ: $CONF уже есть на хосте и будет перезаписан копией из"
    say "  контейнера. Если ты уже переезжал — сначала --rollback."
fi

say "  контейнер : $CONTAINER"
say "  каталог   : $AWG_DIR (тот же путь, что читает бот)"
say "  интерфейс : $AWG_IF"
[ "$MODE" = "plan" ] && { say ""; say "(режим показа: ничего не меняется, добавь --apply)"; }

# ── 1. бот ───────────────────────────────────────────────────────────────────
step "1. Останавливаем бота"
say "  Он пишет в $AWG_IF.conf. Копия, снятая под его запись, потеряла бы"
say "  последнего добавленного пира — без единой ошибки."
run "systemctl stop $BOT_SERVICE"

# ── 2. вынимаем файлы ────────────────────────────────────────────────────────
step "2. Файлы из контейнера → $AWG_DIR"
say "  Путь СОВПАДАЕТ с контейнерным намеренно: бот читает config.AWG_DIR, и"
say "  совпадение оставляет его конфиг нетронутым."
run "mkdir -p $(dirname "$AWG_DIR")"
run "docker cp $CONTAINER:$AWG_DIR $(dirname "$AWG_DIR")/"
run "chmod 700 $AWG_DIR"
run "find $AWG_DIR -type f -name '*.key' -exec chmod 600 {} +"
run "chmod 600 $CONF"

# ── 3. PostUp/PostDown ───────────────────────────────────────────────────────
step "3. Хуки PostUp/PostDown"
if [ -f "$CONF" ]; then
    HOOKS="$(grep -nE '^[[:space:]]*(PostUp|PostDown)[[:space:]]*=' "$CONF" || true)"
else
    HOOKS=""
fi
if [ -n "$HOOKS" ]; then
    say "  Найдено в конфиге:"
    printf '%s\n' "$HOOKS" | sed 's/^/    /'
    say ""
    say "  Эти правила писались под сеть КОНТЕЙНЕРА (его eth0/eth1 и его"
    say "  таблицы). На хосте те же команды затронут боевой NAT сервера, а"
    say "  MASQUERADE и FORWARD для клиентской подсети здесь уже ставит"
    say "  routing-host-setup.sh. Поэтому хуки отключаем, а не переносим."
    run "sed -i -E 's/^([[:space:]]*(PostUp|PostDown)[[:space:]]*=)/# ПЕРЕЕЗД НА ХОСТ: \\1/' $CONF"
    say "  Отключены (закомментированы, не удалены — видно, что было)."
else
    say "  нет — переносить нечего"
fi

# ── 4. ссылка для awg-quick ──────────────────────────────────────────────────
step "4. $LINK → $CONF"
say "  Симлинк, а не копия: файл должен быть ОДИН. Две копии разошлись бы в"
say "  первый же раз, когда бот добавит пира, — и awg-quick поднял бы старую."
run "mkdir -p $QUICK_DIR"
run "ln -sfn $CONF $LINK"

# ── 5. гасим контейнер ───────────────────────────────────────────────────────
step "5. Останавливаем контейнер"
say "  Он держит порт: пока жив, хостовой awg0 на него не сядет."
run "docker stop $CONTAINER"

# ── 6. поднимаем на хосте ────────────────────────────────────────────────────
step "6. Поднимаем $AWG_IF на хосте"
run "awg-quick up $AWG_IF"

if [ "$MODE" = "apply" ]; then
    # Проверяем ДЕЛОМ, а не по коду возврата: при расхождении поколений
    # awg-quick создаёт интерфейс, спотыкается на setconf и молча удаляет его
    # обратно — завершаясь успешно. Снаружи это «скрипт отработал», а сервера
    # нет, и все клиенты отвалились.
    if ! ip link show "$AWG_IF" >/dev/null 2>&1; then
        say ""
        say "ОШИБКА: интерфейс $AWG_IF не поднялся — КЛИЕНТЫ СЕЙЧАС БЕЗ СВЯЗИ."
        say "  Почти всегда это расхождение поколений: модуль ядра и утилиты"
        say "  из разных. Начиная с v3 параметры H1..H4 передаются 64-битными"
        say "  диапазонами, утилиты v1 шлют 32 бита — netlink отвергает."
        say ""
        say "  ВЕРНИ КОНТЕЙНЕР ПРЯМО СЕЙЧАС:"
        say "    sh $0 --rollback"
        exit 1
    fi
    PEERS="$(awg show "$AWG_IF" peers 2>/dev/null | grep -c . || true)"
    say "  Поднят. Пиров: $PEERS"
    if [ "$PEERS" = "0" ]; then
        say ""
        say "  ВНИМАНИЕ: ни одного пира. Интерфейс есть, но конфиг не применён —"
        say "  клиенты не подключатся. Откатывайся: sh $0 --rollback"
    fi
fi

# ── что дальше ───────────────────────────────────────────────────────────────
step "Дальше — вручную"
say ""
say "  1. Переключить бота:  $CONF_DIR/app.yaml → docker: runtime: \"host\""
say "  2. Пересобрать обвяз: sh install/routing-host-setup.sh --apply"
say "     (в host-режиме он не ждёт контейнер и не ставит маршрут через него)"
say "  3. Закрепить от ребута: sh install/routing-host-setup.sh --install-unit"
say "  4. Поднять awg0 после ребута: systemctl enable awg-quick@$AWG_IF"
say "  5. Запустить бота: systemctl start $BOT_SERVICE"
say "  6. Проверить: awg-bot routing-doctor"
say ""
say "  Откат в любой момент: sh $0 --rollback (и runtime обратно в docker)"
