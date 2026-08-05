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
    run "systemctl disable awg-quick@$AWG_IF 2>/dev/null || true"
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

# ── ты сам не через туннель ли зашёл? ────────────────────────────────────────
# Шаг 5 гасит контейнер. Если админ пришёл по SSH ЧЕРЕЗ него, эта команда рвёт
# его собственную сессию — скрипт умирает на середине, контейнер уже погашен, а
# awg0 на хосте ещё не поднят. Сервис лежит целиком, и чинить его придётся,
# заново пробившись на машину. Проверяем ДО того, как что-либо тронули.
SSH_SRC="${SSH_CLIENT%% *}"
[ -z "$SSH_SRC" ] && SSH_SRC="${SSH_CONNECTION%% *}"
if [ -n "$SSH_SRC" ]; then
    TUNNEL_SRC=0
    # адреса контейнера (он маскарадит пиров в свой bridge-адрес) и их шлюзы
    for _a in $(docker inspect "$CONTAINER" \
                -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{.Gateway}} {{end}}' \
                2>/dev/null); do
        [ "$_a" = "$SSH_SRC" ] && TUNNEL_SRC=1
    done
    # и сама клиентская подсеть — на случай, если маскарада на пути не было
    CS_PREFIX="$(awk -F'"' '/^  subnet_prefix:/{print $2}' "$CONF_DIR/app.yaml" 2>/dev/null)"
    case "$SSH_SRC" in
        "${CS_PREFIX:-нет}".*) TUNNEL_SRC=1 ;;
    esac
    if [ "$TUNNEL_SRC" = "1" ]; then
        say ""
        say "СТОП: ты подключён с адреса $SSH_SRC — это ЧЕРЕЗ переносимый VPN."
        say ""
        say "  Шаг 5 гасит контейнер и оборвёт эту самую сессию. Скрипт умрёт на"
        say "  середине: контейнер уже остановлен, awg0 на хосте ещё не поднят,"
        say "  сервис лежит целиком — и чинить придётся, заново пробившись сюда."
        say ""
        say "  Зайди по SSH напрямую, минуя VPN, и повтори."
        exit 1
    fi
    say "  сессия SSH : $SSH_SRC (не через туннель — годится)"
else
    # Молчаливо пропустить проверку нельзя: отсутствие SSH_CLIENT (консоль
    # провайдера, sudo -i с очищенным окружением) неотличимо от «проверено и
    # чисто», а цена ошибки — лежащий сервис и потерянный доступ.
    say "  сессия SSH : ОПРЕДЕЛИТЬ НЕ УДАЛОСЬ (SSH_CLIENT пуст)."
    say "  Убедись сам, что ты НЕ через переносимый VPN: шаг 5 гасит контейнер"
    say "  и оборвёт такую сессию, оставив сервис лежащим."
fi

# Долгий шаг под разорванной сессией доделывать некому. Даже с прямым SSH связь
# может моргнуть, поэтому просим мультиплексор: цена — одна команда, а без него
# обрыв в середине оставляет сервис лежащим.
if [ "$MODE" = "apply" ] && [ -z "${TMUX:-}" ] && [ -z "${STY:-}" ]; then
    say ""
    say "  СОВЕТ: запусти под tmux/screen. Обрыв связи на шагах 5–6 оставит"
    say "  контейнер погашенным, а awg0 неподнятым — сервис ляжет целиком."
    say "  Продолжить всё равно? [y/N]"
    read -r _ans
    case "$_ans" in y|Y|yes|да) ;; *) say "Отменено."; exit 1 ;; esac
fi

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
# Читаем из КОНТЕЙНЕРА, а не из хостовой копии. В режиме показа копирования ещё
# не было, файла на хосте нет — и проверка, ради которой этот режим и
# запускают, молча докладывала «нечего переносить». Источник один и тот же, а
# контейнер на этом шаге ещё жив в обоих режимах.
HOOKS="$(docker exec "$CONTAINER" grep -nE '^[[:space:]]*(PostUp|PostDown)[[:space:]]*=' \
         "$CONF" 2>/dev/null || true)"
OURS="$(printf '%s\n' "$HOOKS" | grep AWGBOT_SSH || true)"
FOREIGN="$(printf '%s\n' "$HOOKS" | grep -v AWGBOT_SSH | grep -E '(PostUp|PostDown)' || true)"

if [ -n "$OURS" ]; then
    say "  Наш fail-closed AWGBOT_SSH — на месте, НЕ трогаем."
    say "  Он в контейнерной форме (цель — дефолтный шлюз, то есть хост, каким"
    say "  его видит контейнер). На хосте дефолтный шлюз — роутер провайдера, и"
    say "  правило встало бы, вернуло ноль и не закрыло ничего. Бот перепишет"
    say "  строку в хостовую форму при первом же реассерте после старта."
    say "  До этого момента окно закрывает ssh_reconcile — он идёт на старте."
fi
if [ -n "$FOREIGN" ]; then
    say ""
    say "  ЧУЖИЕ хуки (не наши):"
    printf '%s\n' "$FOREIGN" | sed 's/^/    /'
    say ""
    say "  СТОП. Они писались под сеть КОНТЕЙНЕРА, и что они сделают на хосте —"
    say "  я не знаю. Разберись и убери или перепиши их вручную, потом повтори."
    say "  Автоматически комментировать не буду: среди них может быть то, без"
    say "  чего сервер не работает."
    [ "$MODE" = "apply" ] && exit 1
fi
if [ -z "$OURS" ] && [ -z "$FOREIGN" ]; then
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

# ── 6a. порт клиентов в файрволе ─────────────────────────────────────────────
# Пока порт публиковал docker, правила в файрволе не требовалось: публикация
# docker ставит DNAT и свои цепочки FORWARD, обходя INPUT и ufw целиком. На
# хосте awg слушает напрямую, и пакет идёт в INPUT — где при политике DROP его
# никто не ждёт. Клиенты просто не подключаются, а причина ни на что не похожа.
step "6a. Порт клиентов в файрволе"
LISTEN_PORT="$(awk -F'[ =]+' '/^[[:space:]]*ListenPort/{print $2; exit}' "$CONF" 2>/dev/null)"
if [ -z "$LISTEN_PORT" ]; then
    say "  ВНИМАНИЕ: не удалось прочитать ListenPort из $CONF — открой порт сам."
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    if ufw status | grep -q "^${LISTEN_PORT}/udp"; then
        say "  ufw: ${LISTEN_PORT}/udp уже разрешён"
    else
        run "ufw allow ${LISTEN_PORT}/udp comment 'awg клиенты'"
    fi
else
    say "  ufw неактивен — проверь, что ${LISTEN_PORT}/udp открыт в твоём файрволе."
fi

# ── 6b. чужой маршрут до клиентской подсети ──────────────────────────────────
# Маршрут через контейнер переживает переезд и перекрывает connected: наружу всё
# уходит, а ответы клиентам отправляются в мёртвую docker-сеть. Диагностика при
# этом зелёная — маршрут ведь существует.
step "6b. Лишний маршрут до клиентской подсети"
CS="$(awk -F'"' '/^  subnet_prefix:/{print $2}' "$CONF_DIR/app.yaml" 2>/dev/null)"
CS="${CS:-10.8.1}.0/24"
_stale="$(ip route show "$CS" 2>/dev/null | grep -v " dev $AWG_IF" || true)"
if [ -n "$_stale" ]; then
    say "  НАЙДЕН маршрут мимо $AWG_IF — снимаем:"
    say "    $_stale"
    run "ip route del $CS 2>/dev/null || true"
else
    say "  чисто"
fi

# ── 7. подъём после перезагрузки ─────────────────────────────────────────────
# Самый дорогой из возможных отказов: сервер переехал, работает, а после первой
# же перезагрузки не поднимается — и ложатся ВСЕ клиенты разом, без связи с
# каким-либо действием. Контейнер до сих пор поднимал сам docker; на хосте это
# больше некому делать, поэтому юнит проверяем и при нужде ставим свой.
step "7. Автоподъём $AWG_IF после перезагрузки"
UNIT_TPL="$(systemctl list-unit-files 'awg-quick@*' 2>/dev/null | grep -c 'awg-quick@' || true)"
AWGQ_UNIT="/etc/systemd/system/awg-quick@.service"
if [ "$UNIT_TPL" != "0" ]; then
    say "  Шаблон awg-quick@.service уже есть."
else
    say "  Шаблона awg-quick@.service нет — утилиты собраны без systemd-юнита."
    say "  Без него awg0 не поднимется после ребута, и это ляжет на всех сразу."
    if [ "$MODE" = "apply" ]; then
        cat > "$AWGQ_UNIT" <<'UNITEOF'
[Unit]
Description=AmneziaWG via awg-quick(8) for %I
After=network-online.target nss-lookup.target
Wants=network-online.target nss-lookup.target
PartOf=awg-quick.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/env awg-quick up %i
ExecStop=/usr/bin/env awg-quick down %i
Environment=WG_ENDPOINT_RESOLUTION_RETRIES=infinity

[Install]
WantedBy=multi-user.target
UNITEOF
        say "  Поставлен $AWGQ_UNIT"
        run "systemctl daemon-reload"
    else
        printf '  would: записать %s\n' "$AWGQ_UNIT"
    fi
fi
run "systemctl enable awg-quick@$AWG_IF"

# ── что дальше ───────────────────────────────────────────────────────────────
step "Дальше — вручную"
say ""
say "  1. Переключить бота:  $CONF_DIR/app.yaml → docker: runtime: \"host\""
say "  2. Пересобрать обвяз: sh install/routing-host-setup.sh --apply"
say "     (в host-режиме он не ждёт контейнер и не ставит маршрут через него)"
say "  3. Закрепить от ребута: sh install/routing-host-setup.sh --install-unit"
say "  4. Запустить бота: systemctl start $BOT_SERVICE"
say "  5. Проверить: awg-bot routing-doctor"
say ""
say "  Откат в любой момент: sh $0 --rollback (и runtime обратно в docker)"
