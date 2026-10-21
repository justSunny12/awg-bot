# install/lib/update.sh — часть awg-bot.sh: обновление (`update`, вторая
# половина `__post_update`), сторож обновления (`__update_guard`), удержание,
# фиксация и откат набора обновлений (`__update_commit`, `__update_revert`).
#
# Не запускается сам: awg-bot.sh читает его при разборе
# (`. "$SELF_DIR/install/lib/update.sh"`), и пути, утилиты (log, warn, die,
# yaml_*, env_*), сборка окружения и юнита — оттуда. После подмены кода новый
# awg-bot.sh берёт и новые части (exec в cmd_update). Тесты склеивают части
# в один текст (tests/botscript.py).
#
# Шаги окружения новой версии (update_env_steps) — общие у второй половины
# update и у восстановления (`update_env_steps restore`). Первым из шагов 4.0.0
# стоит refuse_container_runtime: AmneziaWG в контейнере (раздел docker: со
# строкой runtime: docker) 4.0.0 не поддерживает — отказ до переименования
# раздела (adopt_awg_section), ловушка второй половины возвращает прежний код.
# Любой возврат кода (restore_prev_code: ловушка, сторож, откат набора) ниже
# 4.0.0 на шлюзе снимает постоянный линк к резерву и файл с долей ключа
# переезда (reserve_gw_drop_for_old): прежний агент о них не знает.

# ── update ───────────────────────────────────────────────────────────────────
locate_tgz() {  # locate_tgz DEFAULT_NAME EXPLICIT → печатает путь или пусто
    # Имя по умолчанию — как у ассета релиза (awg-bot.tgz): его человек и
    # скачивает руками; ищем в текущем каталоге и рядом со скриптом.
    local name="$1" explicit="${2:-}" d
    if [[ -n "$explicit" ]]; then [[ -f "$explicit" ]] && { echo "$explicit"; return; }; die "архив не найден: $explicit"; fi
    for d in "$(pwd)" "$SELF_DIR"; do
        [[ -f "$d/$name" ]] && { echo "$d/$name"; return; }
    done
    return 0
}
reserve_busy_guard() {  # reserve_busy_guard ДЕЙСТВИЕ — отказ, пока клиенты у резерва или клиенты резерва здесь
    declare -F bot_tool >/dev/null 2>&1 || return 0
    local why rc=0
    why="$(bot_tool tools.rsvbusy 2>/dev/null)" || rc=$?      # прочий код (нет инструмента, упал) — не помеха
    if [[ $rc -eq 3 ]]; then die "$1 сейчас нельзя: $why"; fi
    return 0
}

cmd_update() {
    require_root; require_installed
    # одно обновление за раз: второй апдейтер (повтор шага набора, ручной поверх
    # кнопки) не начинает, пока первый не довёл; замок переходит в __post_update
    update_lock -n || die "уже идёт обновление, фиксация или откат — дождись их конца"
    local tgz; tgz="$(locate_tgz "awg-bot.tgz" "${1:-}")"
    [[ -n "$tgz" ]] || die "не найден awg-bot.tgz (в текущем каталоге или рядом со скриптом); укажи путь: awg-bot update <tgz>"
    log "обновление из: $tgz"

    # Всё, что может отказать, — ДО остановки сервиса: распаковка, проверка
    # состава, сборка ядра. Пока это идёт, работает прежний код, и любой отказ
    # ничего не ломает — сервис даже не трогали. Версия поставки проверяется
    # раньше вопросов об удалении данных: отказ «откат невозможен» не должен
    # идти после «Удалить данные?»
    local tmp; tmp="$(mktemp -d)"
    UPDATE_TMP="$tmp"                           # ловушке нужна глобальная: EXIT срабатывает уже вне функции
    trap 'rm -rf "${UPDATE_TMP:-}"' EXIT        # отказ до остановки сервиса — распакованное прочь
    log "распаковываю новый код…"
    # --no-same-owner: архив несёт uid машины, где его собрали; код в /opt — только root
    tar xzf "$tgz" -C "$tmp" --no-same-owner || { rm -rf "$tmp"; die "архив не распаковался: $tgz"; }
    local main; main="$(find "$tmp" -maxdepth 3 -type f -path '*/awgbot/__main__.py' | head -n1)"
    [[ -n "$main" ]] || { rm -rf "$tmp"; die "в архиве нет awgbot/ — не та поставка?"; }
    local src; src="$(dirname "$(dirname "$main")")"
    chown -R root:root "$src"
    # Версия совместимости — граница, ниже которой этот код не возвращается:
    # копии ниже неё не применяются, а откат — только чистой установкой.
    local floor newver
    floor="$(compat_floor)"; newver="$(tree_version "$src")"
    if [[ -n "$floor" && -n "$newver" ]] && version_lt "$newver" "$floor"; then
        rm -rf "$tmp"
        die "откат ниже $floor невозможен — только чистая установка"
    fi
    note_installed_digest "$tgz" "$newver"     # архив из выпуска — бэкапы будут знать выпуск
    if declare -F reserve_busy_guard >/dev/null 2>&1; then reserve_busy_guard "обновление"; fi

    local wipe=0
    # AWG_UPDATE_KEEP=1 — установщик уже выбрал «обновить, сохранив данные»:
    # второй раз про удаление не спрашиваем
    if [[ "${AWG_UPDATE_KEEP:-0}" != "1" ]] \
       && confirm "Удалить пользовательские данные (БД в $DATA_DIR + секреты $ENV_FILE + конфиг)?" n; then
        confirm "Точно удалить ВСЕ данные и настройки? Это НЕОБРАТИМО." n && wipe=1 || log "данные оставлены."
    fi

    ensure_python

    # ЯДРО — ДО подмены кода. Версия AmneziaWG прибита к поставке, значит её
    # надо собрать; а собирать надо ПЕРВЫМ делом, пока на диске рабочая
    # установка: не собралось (нет заголовков, апстрим переименовал цель) —
    # ничего не подменяем и сообщаем причину. Иначе хост остался бы с новым
    # кодом и старым ядром, то есть в состоянии, которого мы нигде не проверяем.
    if [[ -f "$src/install/awg.lock" ]]; then
        log "AmneziaWG: версия из поставки — собираю до подмены кода…"
        if ! AWG_LOCK="$src/install/awg.lock" bash "$src/install/awg-kernel-install.sh" install; then
            rm -rf "$tmp"
            die "ядро AmneziaWG из поставки не собралось — код НЕ подменён, версия прежняя, сервис не трогали"
        fi
    fi

    # Сторож прошлого обновления, если ещё стоит, — прочь: его прежний код
    # уходит ниже, откат теперь — на версию, работающую сейчас
    update_guard_disarm
    log "останавливаю ${SERVICE}…"; systemctl stop "$SERVICE" 2>/dev/null || true
    lanmap_drop_for_older "$src"
    # Старый код — в сторону, а не под нож: отказ копирования (кончилось место,
    # битый архив) возвращает его на место и поднимает сервис. Ловушка — сразу
    # после остановки: с этого момента любой выход не по плану = откат.
    UPDATE_PREV="$INSTALL_DIR/.prev-code"; UPDATE_TMP="$tmp"
    rm -rf "$UPDATE_PREV"; mkdir -p "$UPDATE_PREV"
    trap update_rescue EXIT
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name venv ! -name .venvs ! -name .prev-code ! -name .hold-code \
        -exec mv -t "$UPDATE_PREV" {} + \
        || die "прежний код не отодвинулся — обновление отменено"
    cp -a "$src"/. "$INSTALL_DIR"/ || die "новый код не лёг целиком — возвращаю прежний"
    chmod +x "$INSTALL_DIR/awg-bot.sh" 2>/dev/null || true
    [[ -x "$INSTALL_DIR/awg-bot.sh" && -f "$INSTALL_DIR/awgbot/__main__.py" ]] || die "новый код лёг не полностью — возвращаю прежний"
    trap - EXIT
    # прежний код остаётся в .prev-code до конца второй половины: её отказ
    # вернёт и его, и окружение (post_update_rescue)
    rm -rf "$tmp"
    export AWG_UPDATE_PREV="$UPDATE_PREV"

    # Дальше — руками НОВОГО скрипта, а не этого.
    #
    # Bash держит функции в памяти процесса с момента разбора файла. Мы только
    # что подменили awg-bot.sh на диске, но install_unit/seed_conf/build_venv в
    # этом процессе — по-прежнему из СТАРОЙ версии. То есть любая их правка не
    # может применить сама себя: она доезжает до сервера и молчит ещё одно
    # обновление. Ровно так юнит однажды остался с устаревшей зависимостью
    # на сервере, уже её не требовавшем.
    #
    # exec, а не вызов: старому процессу после подмены кода делать нечего, а
    # продолжать читать с диска файл, который сменился под ним, — отдельный
    # способ получить мусор в разборе оставшегося хвоста.
    exec "$INSTALL_DIR/awg-bot.sh" __post_update "$wipe"
}

INSTALLED_DIGEST_FILE="$DATA_DIR/installed-digest"   # «<версия> <sha256>» архива из выпуска — бот перенесёт в базу

release_sha256s() {  # release_sha256s VER → sha256 ассетов выпуска vVER на GitHub (hex, по строке); пусто — нет сети или выпуска
    local repo
    repo="$(yaml_get "$CONF_DIR/updates.yaml" repo)"
    [[ -n "$repo" ]] || repo="$(yaml_get "$INSTALL_DIR/conf/updates.yaml" repo)"
    [[ -n "$repo" && -n "$1" ]] || return 0
    command -v curl >/dev/null 2>&1 || return 0
    { curl -fsS --max-time 10 "https://api.github.com/repos/${repo// /}/releases/tags/v$1" 2>/dev/null \
        | grep -oE '"digest" *: *"sha256:[0-9a-f]{64}"' | grep -oE '[0-9a-f]{64}'; } || true
}

note_installed_digest() {  # note_installed_digest TGZ VER — архив совпал с выпуском vVER → installed-digest
    # Метка бэкапа берёт digest выпуска из базы (update_installed_digest), и
    # без него переход на версию бэкапа недоступен. Самообновление кнопкой
    # кладёт его само, ручной `update` — этим файлом: один запрос к GitHub,
    # sha256 архива против ассетов выпуска той же версии. Нет сети или не
    # совпало (своя сборка) — файла нет, прежний прочь: чужой версии он не нужен.
    local tgz="$1" ver="$2" have sums
    rm -f "$INSTALLED_DIGEST_FILE"
    [[ -n "$ver" && -f "$tgz" ]] || return 0
    command -v sha256sum >/dev/null 2>&1 || return 0
    have="$(sha256sum "$tgz" | awk '{print $1}')"
    if [[ -n "${AWG_BOT_VERIFIED_SHA256:-}" && "$have" == "${AWG_BOT_VERIFIED_SHA256:-}" ]]; then
        sums="$have"                       # установщик из трубы уже сверил этот архив с выпуском
    else
        sums="$(release_sha256s "$ver")"
    fi
    if [[ -n "$have" ]] && grep -qxF "$have" <<<"$sums"; then
        mkdir -p "$DATA_DIR"
        ( umask 077; printf '%s %s\n' "$ver" "$have" > "$INSTALLED_DIGEST_FILE" )
        log "целостность: sha256 архива совпал с выпуском v$ver"
    else
        log "целостность: архив не сверен с выпуском v$ver (нет ответа GitHub или своя сборка)"
    fi
    return 0
}

compat_floor() {  # compat_floor [DIR] — версия совместимости кода в DIR (по умолчанию — установленного); пусто — не прочитана
    sed -n 's/^COMPAT_FLOOR *= *"\([0-9][0-9.]*\)".*/\1/p' "${1:-$INSTALL_DIR}/awgbot/core/compat.py" 2>/dev/null | head -n1
}

tree_version() {  # tree_version DIR — версия поставки в каталоге (awgbot/__version__.py); пусто — не прочитана
    sed -n 's/^__version__ *= *"\([0-9][0-9.]*\)".*/\1/p' "$1/awgbot/__version__.py" 2>/dev/null | head -n1
}

version_lt() {  # version_lt A B — A строго старше B (X.Y.Z или X.Y.Z.P)
    [[ -n "$1" && -n "$2" && "$1" != "$2" ]] \
        && [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" == "$1" ]]
}

lanmap_drop_for_older() {  # lanmap_drop_for_older DIR — поставка без адресов в туннеле: таблицу прочь
    # Таблицу inet awg_lan_map ставит бот 3.3.0 и только он; файла в
    # /etc/nftables.d у неё нет. Код без неё (выпуск старше 3.3.0) о таблице
    # не знает, а оставшись в ядре до ребута, она ломает его трафик: цепочка
    # slot_zone кладёт ответы по реальным адресам сетей шлюзов в другую зону
    # conntrack (invalid → drop файервола), twin_snat переводит источник.
    # Снимаем при остановке сервиса, пока исполняется этот (новый) скрипт.
    # Решает версия поставки ниже 3.3.0 И отсутствие в ней кода таблицы
    # (awgbot/infra/routing/lanmap.py): предрелизная сборка 3.2.x.N с этим
    # кодом — та же функция, таблицу не трогаем. Таблицы и правила слотов
    # (fwmark → lookup 10N) прежний бот ставит сам и держит для зонда резерва
    # — их не трогаем. Маршруты настоящих подсетей прежний бот возвращает сам:
    # при перезагрузке ВПС, на первом такте живости при задействованном
    # РФ-доступе, иначе — при сохранении «🗺 Подсети» слота.
    local ver; ver="$(tree_version "$1")"
    version_lt "$ver" "3.3.0" || return 0
    [[ -f "$1/awgbot/infra/routing/lanmap.py" ]] && return 0
    command -v nft >/dev/null 2>&1 || return 0
    if nft delete table inet awg_lan_map 2>/dev/null; then
        ok "поставка без адресов в туннеле (v$ver): таблица awg_lan_map снята — текущая версия её не поддерживает"
    fi
    return 0
}

update_rescue() {  # ловушка первой половины обновления: прежний код назад, сервис вверх
    local rc=$?
    [[ "$rc" -eq 0 ]] && return 0
    warn "обновление прервалось (код $rc) — возвращаю прежний код и поднимаю сервис"
    if [[ -n "${UPDATE_PREV:-}" && -d "$UPDATE_PREV" ]]; then
        restore_prev_code "$UPDATE_PREV"
    fi
    [[ -n "${UPDATE_TMP:-}" ]] && rm -rf "$UPDATE_TMP"
    systemctl start "$SERVICE" 2>/dev/null || true
}
restore_prev_code() {  # restore_prev_code DIR — прежний код из DIR на место нового (окружение не трогает); 1 — неполон
    # Единая точка всех откатов кода (ловушка второй половины, сторож, откат
    # набора, несостоявшийся переход на выпуск копии): app.yaml приводится
    # к раскладке возвращённого кода (adopt_awg_section_for_old)
    local prev="$1" _rc=0
    # текущий код удаляется только под полный прежний: иначе хост остался бы без кода
    if [[ ! -f "$prev/awg-bot.sh" || ! -f "$prev/awgbot/__main__.py" ]]; then
        warn "прежний код в $prev неполон — текущий оставлен на месте"
        return 1
    fi
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name venv ! -name .venvs ! -name .prev-code ! -name .hold-code \
        -exec rm -rf {} + 2>/dev/null || true
    ( shopt -s dotglob; mv "$prev"/* "$INSTALL_DIR"/ 2>/dev/null ) || true
    rm -rf "$prev"
    adopt_awg_section_for_old
    reserve_gw_drop_for_old
    # Копия обвязки шлюза — тоже код: ensure_host_autostart уже могла положить
    # скрипт новой версии, и прежний агент остался бы при чужой обвязке
    gw_plumbing_sync || _rc=$?
    [[ "$_rc" -eq 1 ]] && warn "не удалось вернуть прежнюю копию обвязки в /usr/local/sbin/routing-gw-setup.sh"
    return 0
}
# Возвращённый код ниже 4.0.0 о резерве не знает: линк awgrsv, его таблица,
# юнит и файл с долей ключа переезда остались бы на шлюзе без хозяина —
# снять, пока скрипт ещё на месте (сервер выдаст долю заново при обновлении).
reserve_gw_drop_for_old() {
    [[ -x /usr/local/sbin/reserve-gw-setup.sh && -f "$INSTALL_DIR/awgbot/__version__.py" ]] || return 0
    local v; v="$(sed -nE 's/^__version__ = "([0-9.]+)".*/\1/p' "$INSTALL_DIR/awgbot/__version__.py" 2>/dev/null | head -n1 || true)"
    [[ -n "$v" ]] || return 0
    [[ "$(printf '%s\n%s\n' "$v" "4.0.0" | sort -V | head -n1)" == "$v" && "$v" != "4.0.0" ]] || return 0
    sh /usr/local/sbin/reserve-gw-setup.sh --remove >/dev/null 2>&1 || true
    rm -f /var/lib/awg-gw/reserve.json
    log "резерв: линк к резерву и доля ключа переезда сняты — возвращённый код $v о них не знает"
}
# Копия routing-gw-setup.sh в /usr/local/sbin — по поставке в $INSTALL_DIR,
# если копия уже есть (нет — обвязку тут не разворачивали, класть нечего).
# 0 — копия переписана, 1 — не удалось, 2 — менять нечего.
gw_plumbing_sync() {
    local _rgs="/usr/local/sbin/routing-gw-setup.sh" _src="$INSTALL_DIR/install/routing-gw-setup.sh"
    [[ -f "$_rgs" && -f "$_src" ]] || return 2
    cmp -s "$_src" "$_rgs" && return 2
    install -m 0755 "$_src" "$_rgs" || return 1
}

post_update_rescue() {  # ловушка второй половины обновления
    # cmd_update остановил сервис и передал управление сюда через exec. Любой
    # отказ ниже (нет сети для pip, битый конфиг, systemd) оставлял бы НОВЫЙ код
    # на диске и ОСТАНОВЛЕННЫЙ сервис, причём молча: апдейтер запущен из
    # systemd-run с выводом в /dev/null. Поэтому: поднять сервис на том, что
    # есть, и сказать админу в чат.
    local rc=$? back=0
    [[ "$rc" -eq 0 ]] && return 0
    venv_rollback
    if [[ -n "${AWG_UPDATE_PREV:-}" && -d "$AWG_UPDATE_PREV" ]]; then
        warn "обновление прервалось (код $rc) — возвращаю прежнюю версию и поднимаю сервис"
        restore_prev_code "$AWG_UPDATE_PREV" && back=1
        adopt_timezone_undo                # ключ снял этот прогон — прежний код получает его назад
        unit_guard_strip_for_old           # прежний скрипт сторожа не знает — его шаг из юнита прочь
    else
        warn "обновление прервалось (код $rc) — поднимаю сервис на прежнем состоянии"
    fi
    update_guard_disarm                    # сторож этого прогона, если успел встать: откат уже здесь
    rm -f "$DATA_DIR/update-running"
    systemctl start "$SERVICE" 2>/dev/null || true
    local what='Код уже подменён, но довести установку не удалось. Сервис поднят на том, что есть.'
    [[ "$back" -eq 1 ]] && what='Довести установку не удалось — вернул прежнюю версию: и код, и окружение. Сервис поднят.'
    ( cd "$INSTALL_DIR" 2>/dev/null \
        && export AWG_BOT_ENV="$ENV_FILE" AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" AWG_WHAT="$what" \
        && ./venv/bin/python -c "
import os
from awgbot.infra import tgsend
tgsend.send('⚠️ <b>Обновление прервалось.</b>\n\n' + os.environ['AWG_WHAT'] + '\n\n'
            'Посмотреть причину: <code>journalctl -u awg-bot -e -o short-iso</code>, '
            'повторить: <code>sudo awg-bot update &lt;архив&gt;</code>.')
" >/dev/null 2>&1 ) || true
}
unit_guard_strip_for_old() {  # возвращённый код не знает __update_guard: ExecStartPre из юнита — прочь
    # Юнит переписал новый скрипт (install_unit) до отказа; прежний скрипт на
    # глагол __update_guard отвечал бы «неизвестная команда» на каждом старте
    # (строка с «-» старт не держит, но сорит в журнал)
    local unit="$UNIT_PATH" tmp
    grep -q '__update_guard' "$INSTALL_DIR/awg-bot.sh" 2>/dev/null && return 0
    [[ -f "$unit" ]] && grep -q '__update_guard' "$unit" || return 0
    tmp="$(mktemp)" || return 0
    grep -v '__update_guard' "$unit" > "$tmp" && cat "$tmp" > "$unit"
    rm -f "$tmp"
    systemctl daemon-reload 2>/dev/null || true
}
update_done() {  # обновление доведено: прежние код и окружение больше не нужны
    rm -f "$DATA_DIR/update-running"       # апдейтер отработал — pid в файле больше ничего не значит
    [[ -n "${AWG_UPDATE_PREV:-}" ]] && rm -rf "$AWG_UPDATE_PREV"
    venv_prune
}

# ── сторож обновления: прежняя версия живёт, пока новая не вышла на связь ─────
# После старта нового сервиса прежние код (.prev-code), окружение (.venvs/.prev)
# и сборка ядра не снимаются сразу: сторож —
# таймер systemd через UPDATE_GUARD_MIN минут — смотрит отметку жизни, которую
# пишет новый процесс бота (основной бот — первый ответ Telegram на getUpdates,
# агент — он же или первое подписанное слово сервера AWG в сессии канала).
# Отметка этой версии — фиксация (update_done, prune ядра); нет — откат в той
# же установке: прежние код и окружение на место, перезапуск, метка отката
# для прежнего процесса. Переход через COMPAT_FLOOR (прежняя версия ниже
# версии совместимости нового кода) сторожем не прикрывается: откат ниже неё
# запрещён тем же правилом, что `awg-bot update`.
UPDATE_GUARD="$DATA_DIR/update-guard"            # сторож взведён: VERSION= и PREV= (пишет post_update)
UPDATE_OK="$DATA_DIR/update-ok"                  # отметка жизни «<версия> <время>» (пишет бот)
UPDATE_ROLLED="$DATA_DIR/update-rolled-back"     # метка отката (JSON) — прочтёт прежний процесс
UPDATE_GUARD_UNIT="awg-bot-update-guard"
UPDATE_GUARD_MIN=10                             # окно сторожа, минут
UPDATE_GUARD_BOOT_MIN=5                         # после загрузки в окне — не меньше стольких минут
UPDATE_LOCK="$UPDATE_GUARD.lock"                 # update, сторож, фиксация и откат набора — по одному

update_lock() {  # update_lock -n | -w СЕК — замок установки на fd 9 (живёт до конца процесса и через exec)
    # нет flock — без замка, как раньше; замок не взят — 1.
    # Взят — pid в update-running: бот видит, что апдейтер ещё жив (шаг набора
    # ждёт его факта, а не запускает второй); exec сохраняет pid
    mkdir -p "$DATA_DIR" 2>/dev/null || true
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$UPDATE_LOCK"
        flock "$@" 9 || return 1
    fi
    printf '%s\n' "$$" > "$DATA_DIR/update-running" 2>/dev/null || true
    return 0
}

guard_get() { [[ -f "$UPDATE_GUARD" ]] && sed -nE "s/^$1=(.*)$/\1/p" "$UPDATE_GUARD" | head -n1 || true; }

update_guard_due() {  # 0 — обновление прикрыть сторожем: прежний код на месте и в линии совместимости
    [[ -n "${AWG_UPDATE_PREV:-}" && -d "$AWG_UPDATE_PREV" ]] || return 1
    command -v systemd-run >/dev/null 2>&1 || return 1
    local prev floor
    prev="$(tree_version "$AWG_UPDATE_PREV")"; floor="$(compat_floor)"
    [[ -n "$prev" && -n "$floor" ]] || return 1
    version_lt "$prev" "$floor" && return 1      # переход через COMPAT_FLOOR: назад дороги нет
    return 0
}

update_guard_write() {  # update_guard_write — до старта сервиса: что сторожить
    local ver prev
    ver="$(tree_version "$INSTALL_DIR")"; prev="$(tree_version "$AWG_UPDATE_PREV")"
    rm -f "$UPDATE_OK"                     # отметка прошлых запусков не в счёт — только новый процесс
    mkdir -p "$DATA_DIR"
    # AT — секунды эпохи: по ним проверка на старте сервиса считает, истекло ли окно
    # HOLD — id хода или команды набора (0 — не шаг набора)
    ( umask 077; printf 'VERSION=%s\nPREV=%s\nAT=%s\nHOLD=%s\n' "$ver" "$prev" "$(date +%s)" \
        "$(hold_id)" > "$UPDATE_GUARD" )
}

update_guard_arm() {  # update_guard_arm [МИНУТ] — таймер сторожа; отказ systemd-run — 1
    # RemainAfterElapse=no и --collect: отработавший таймер и его служба
    # выгружаются — следующее обновление взводит сторожа под тем же именем
    systemd-run --quiet --collect --unit="$UPDATE_GUARD_UNIT" --on-active="${1:-$UPDATE_GUARD_MIN}min" \
        --timer-property=AccuracySec=10s --timer-property=RemainAfterElapse=no \
        --description="awg-bot: сторож обновления" \
        "$INSTALL_DIR/awg-bot.sh" __update_guard >/dev/null 2>&1
}

update_guard_disarm() {  # снять сторожа: таймер и его состояние
    systemctl stop "$UPDATE_GUARD_UNIT.timer" 2>/dev/null || true
    systemctl reset-failed "$UPDATE_GUARD_UNIT.timer" "$UPDATE_GUARD_UNIT.service" 2>/dev/null || true
    rm -f "$UPDATE_GUARD"
}

update_guard_rollback() {  # update_guard_rollback VERSION PREV [nostart] — новая не вышла на связь: прежняя на место
    # nostart — из ExecStartPre юнита: сервис ещё не запущен, а systemctl для
    # него из его же запуска ждал бы сам себя; запустится он сам, уже прежним кодом
    local ver="$1" prev="$2" how="${3:-}"
    if [[ ! -d "$AWG_UPDATE_PREV" ]]; then
        warn "v$ver не вышла на связь за $UPDATE_GUARD_MIN минут, но прежнего кода нет — откатывать некуда"
        rm -f "$UPDATE_GUARD"
        return 1
    fi
    warn "v$ver не вышла на связь за $UPDATE_GUARD_MIN минут — возвращаю v$prev"
    [[ "$how" == "nostart" ]] || systemctl stop "$SERVICE" 2>/dev/null || true
    if ! restore_prev_code "$AWG_UPDATE_PREV"; then
        rm -f "$UPDATE_GUARD"
        [[ "$how" == "nostart" ]] || systemctl start "$SERVICE" 2>/dev/null || true
        return 1
    fi
    venv_rollback
    # Метка отката — сразу за кодом: из ExecStartPre (таймаут 90 с) до
    # удаления окружения дело может не дойти, а прежнему процессу она нужна
    ( umask 077; printf '{"version": %s, "prev": %s, "at": %s}\n' "$(json_str "$ver")" "$(json_str "$prev")" \
        "$(json_str "$(date -Iseconds)")" > "$UPDATE_ROLLED" ) || true
    rm -f "$UPDATE_GUARD" "$UPDATE_OK"
    # окружение новой версии — прочь; из ExecStartPre — потом (следующая уборка)
    [[ "$how" == "nostart" ]] && return 0
    venv_prune
    systemctl start "$SERVICE" || warn "$SERVICE не запустился — journalctl -u $SERVICE -e -o short-iso"
    return 0
}

cmd_update_guard() {  # __update_guard [--alive|--start] — фиксация по отметке жизни или откат
    # без ключа — таймер сторожа: отметка этой версии — фиксация, нет — откат;
    # --alive — бот, поставив отметку: фиксация сразу, без отметки — ничего
    #   (решает таймер);
    # --start — ExecStartPre юнита, перед каждым стартом сервиса: таймера нет
    #   (перезагрузка в окне) — окно не истекло — сторож заново на остаток, истекло
    #   без отметки — откат до старта; таймер есть или сторожа нет — ничего.
    require_root
    local mode="${1:-}"
    [[ -f "$UPDATE_GUARD" ]] || return 0
    # одно решение за раз; откат держит замок и запускает сервис — проверка
    # его старта не ждёт замка (ждала бы сама себя), решает тот, кто держит
    if [[ "$mode" == "--start" ]]; then update_lock -n || return 0; else update_lock -w 120 || return 0; fi
    [[ -f "$UPDATE_GUARD" ]] || return 0
    local ver prev at cur mark left
    ver="$(guard_get VERSION)"; prev="$(guard_get PREV)"; at="$(guard_get AT)"; cur="$(tree_version "$INSTALL_DIR")"
    if [[ -z "$ver" || "$ver" != "$cur" ]]; then
        # установку сменили мимо сторожа (новое обновление, чистая установка) — сторожить нечего
        rm -f "$UPDATE_GUARD"
        return 0
    fi
    AWG_UPDATE_PREV="$INSTALL_DIR/.prev-code"
    mark="$(awk 'NR == 1 { print $1 }' "$UPDATE_OK" 2>/dev/null || true)"
    if [[ "$mark" == "$ver" ]]; then
        systemctl stop "$UPDATE_GUARD_UNIT.timer" 2>/dev/null || true
        local hold; hold="$(guard_get HOLD)"
        if [[ -n "$hold" && "$hold" != "0" ]]; then
            # шаг набора: прежнее ждёт фиксации или отката всего набора
            log "v$ver вышла на связь — прежняя версия держится до фиксации набора"
            update_hold_take "$hold"
            rm -f "$UPDATE_GUARD" "$UPDATE_OK"
            return 0
        fi
        log "v$ver вышла на связь — прежняя версия v$prev больше не нужна"
        rm -f "$UPDATE_GUARD" "$UPDATE_OK"
        prune_old_kernel_builds
        update_done
        return 0
    fi
    case "$mode" in
        --alive) return 0 ;;
        --start)
            systemctl is-active --quiet "$UPDATE_GUARD_UNIT.timer" && return 0
            [[ "$at" =~ ^[0-9]+$ ]] || at=0
            left=$(( at + UPDATE_GUARD_MIN * 60 - $(date +%s) ))
            # окно по стенным часам включает простой хоста: после загрузки новой
            # версии — не меньше UPDATE_GUARD_BOOT_MIN минут выйти на связь
            (( left < UPDATE_GUARD_BOOT_MIN * 60 )) && left=$(( UPDATE_GUARD_BOOT_MIN * 60 ))
            log "сторож обновления потерян (перезагрузка) — взвожу заново на $(( (left + 59) / 60 )) мин"
            update_guard_arm $(( (left + 59) / 60 )) && return 0
            warn "сторож обновления не взведён заново (systemd-run) — решаю сейчас"
            update_guard_rollback "$ver" "$prev" nostart || true
            return 0 ;;
    esac
    update_guard_rollback "$ver" "$prev"
}

# ── шаг набора: прежнее держится до фиксации или отката всего набора ──────────
# Шаг (сервер или шлюз под командой набора) вышел на связь — прежние код
# (.hold-code) и окружение (ENV в update-hold) не снимаются: откат набора
# (__update_revert) — переименованием на месте, без
# сети. Цепочка ступеней под той же командой держит самое первое прежнее:
# промежуточные ступени снимаются, когда следующая вышла на связь. Фиксация
# набора (__update_commit) — прежнее и сборки ядра прочь.
UPDATE_HOLD_FILE="$DATA_DIR/update-hold"         # PREV= (версия), ENV= (окружение), AT=, RUN= (id хода/команды)
UPDATE_HOLD_CODE="$INSTALL_DIR/.hold-code"

hold_get() { [[ -f "${UPDATE_HOLD_FILE:-}" ]] && sed -nE "s/^$1=(.*)$/\1/p" "$UPDATE_HOLD_FILE" | head -n1 || true; }

hold_id() {  # id шага набора из AWG_UPDATE_HOLD; не шаг набора — 0
    local id="${AWG_UPDATE_HOLD:-0}"
    [[ "$id" =~ ^[0-9A-Za-z]{1,16}$ ]] || id=0
    printf '%s' "$id"
}

update_hold_drop() {  # удержание набора прочь (ручной update, wipe, переход через COMPAT_FLOOR, другой ход)
    [[ -f "$UPDATE_HOLD_FILE" || -d "$UPDATE_HOLD_CODE" ]] || return 0
    log "удержание прежней версии v$(hold_get PREV) снято"
    rm -rf "$UPDATE_HOLD_CODE"
    rm -f "$UPDATE_HOLD_FILE"
}

update_crosses_floor() {  # 0 — прежняя версия (AWG_UPDATE_PREV) ниже версии совместимости нового кода
    [[ -n "${AWG_UPDATE_PREV:-}" && -d "$AWG_UPDATE_PREV" ]] || return 1
    local prev floor
    prev="$(tree_version "$AWG_UPDATE_PREV")"; floor="$(compat_floor)"
    [[ -n "$prev" && -n "$floor" ]] && version_lt "$prev" "$floor"
}

update_hold_take() {  # update_hold_take ID — шаг вышел на связь: его прежнее — в удержание набора (первое — остаётся)
    local run="${1:-}" prev_env=""
    prev_env="$(cat "$INSTALL_DIR/.venvs/.prev" 2>/dev/null)" || prev_env=""
    if [[ -f "$UPDATE_HOLD_FILE" && "$(hold_get RUN)" != "$run" ]]; then
        update_hold_drop                   # удержание другого хода или команды — не наше, прочь
    fi
    if [[ -f "$UPDATE_HOLD_FILE" && -d "$UPDATE_HOLD_CODE" ]]; then
        # следующая ступень той же команды: промежуточное прочь, первое прежнее держится
        rm -rf "$INSTALL_DIR/.prev-code"
        [[ -n "$prev_env" && "$prev_env" != "$(hold_get ENV)" ]] && rm -rf "$prev_env"
        rm -f "$INSTALL_DIR/.venvs/.prev"
        return 0
    fi
    [[ -d "$INSTALL_DIR/.prev-code" ]] || return 0
    rm -rf "$UPDATE_HOLD_CODE"
    mv "$INSTALL_DIR/.prev-code" "$UPDATE_HOLD_CODE"
    rm -f "$INSTALL_DIR/.venvs/.prev"
    ( umask 077; printf 'PREV=%s\nENV=%s\nAT=%s\nRUN=%s\n' "$(tree_version "$UPDATE_HOLD_CODE")" "$prev_env" \
        "$(date +%s)" "$run" > "$UPDATE_HOLD_FILE" )
}

cmd_update_commit() {  # __update_commit — набор зафиксирован: удержанное прежнее и сборки ядра прочь
    require_root
    [[ -f "$UPDATE_HOLD_FILE" ]] || return 0
    update_lock -w 120 || { warn "замок установки занят — фиксация набора не выполнена"; return 1; }
    [[ -f "$UPDATE_HOLD_FILE" ]] || return 0
    log "набор зафиксирован — прежняя версия v$(hold_get PREV) больше не нужна"
    rm -rf "$UPDATE_HOLD_CODE"
    rm -f "$UPDATE_HOLD_FILE"
    prune_old_kernel_builds
    venv_prune
}

REV_SRC=""; REV_ENV=""; REV_PREV=""
revert_source() {  # источник отката набора → REV_*; нет (или он ниже версии совместимости) — 1
    # Удержанное шагом набора (.hold-code) или прежнее ещё не подтверждённого
    # шага (.prev-code под сторожем). Ниже COMPAT_FLOOR назад нельзя — как у update.
    REV_SRC=""; REV_ENV=""; REV_PREV=""
    if [[ -f "$UPDATE_HOLD_FILE" && -d "$UPDATE_HOLD_CODE" ]]; then
        REV_SRC="$UPDATE_HOLD_CODE"; REV_ENV="$(hold_get ENV)"; REV_PREV="$(hold_get PREV)"
    elif [[ -f "$UPDATE_GUARD" && -d "$INSTALL_DIR/.prev-code" ]]; then
        REV_SRC="$INSTALL_DIR/.prev-code"; REV_ENV="$(cat "$INSTALL_DIR/.venvs/.prev" 2>/dev/null || true)"
        REV_PREV="$(guard_get PREV)"
    fi
    [[ -n "$REV_SRC" ]] || return 1
    local ver floor
    ver="$(tree_version "$REV_SRC")"; floor="$(compat_floor)"
    if [[ -n "$ver" && -n "$floor" ]] && version_lt "$ver" "$floor"; then
        REV_SRC=""
        return 1
    fi
    return 0
}

cmd_update_revert() {  # __update_revert [--check] — откат набора: прежние код и окружение на место
    # Источника нет — код 2: откатываться не на что, понижение — с GitHub.
    # --check — только ответ. Сам откат — под замком установки, источник
    # выбирается заново после остановки сервиса (сторож или --alive могли
    # решить, пока ждали замок).
    require_root
    if ! revert_source; then
        [[ "${1:-}" == "--check" ]] || warn "прежней версии нет — откатывать некуда"
        return 2
    fi
    [[ "${1:-}" == "--check" ]] && return 0
    update_lock -w 120 || { warn "замок установки занят — откат набора не выполнен"; return 1; }
    systemctl stop "$UPDATE_GUARD_UNIT.timer" 2>/dev/null || true
    systemctl stop "$SERVICE" 2>/dev/null || true
    if ! revert_source; then
        warn "прежней версии нет — откатывать некуда"
        systemctl start "$SERVICE" 2>/dev/null || true
        return 2
    fi
    local src="$REV_SRC" env="$REV_ENV" prev="$REV_PREV"
    warn "откат набора — возвращаю v${prev:-?}"
    if ! restore_prev_code "$src"; then
        systemctl start "$SERVICE" 2>/dev/null || true
        return 1
    fi
    rm -rf "$INSTALL_DIR/.prev-code" "$UPDATE_HOLD_CODE"     # источник перенесён — второй (если был) прочь
    [[ -n "$env" ]] && printf '%s\n' "$env" > "$INSTALL_DIR/.venvs/.prev"
    venv_rollback
    rm -f "$UPDATE_HOLD_FILE"
    venv_prune
    rm -f "$UPDATE_GUARD" "$UPDATE_OK"
    systemctl start "$SERVICE" || warn "$SERVICE не запустился — journalctl -u $SERVICE -e -o short-iso"
    return 0
}

update_env_steps() {  # update_env_steps [restore] — окружение новой версии: вторая половина update и восстановления
    # Одна функция на оба пути: восстановление с переходом на выпуск бэкапа
    # раньше копировало шаги руками и теряло их (ping, резолвер, проверка conf)
    ensure_python
    ensure_ping
    build_venv
    install_unit
    ensure_host_autostart
    ensure_awg_module_loaded
    ensure_awg_generation
    adopt_resolver                         # dns1 уже приватный — резолвер под опеку бота
    # ключ timezone снимается один раз — на переходе с выпуска, где его писали
    # шаблон и установщик (ниже 3.4.0): с 3.4.0 ключ в app.yaml — переопределение
    # человека, обновление его не трогает; восстановление решает пояс после
    # замены conf (restore_apply)
    if [[ "${1:-}" != "restore" ]] && update_adopts_timezone; then
        adopt_timezone
    fi
    refuse_container_runtime               # AmneziaWG в контейнере: 4.0.0 не поддерживает — отказ до переименования
    adopt_awg_section                      # раздел параметров AmneziaWG — под именем awg (с 4.0.0)
    adopt_link_keepalive                   # keepalive стороны сервера AWG на линках слотов (с 4.0.0)
    seed_conf                              # досеять НОВЫЕ conf-файлы этой версии
                                           # (существующие не трогаем — idempotent)
    validate_config
}

refuse_container_runtime() {  # установка с AmneziaWG в контейнере (runtime: "docker") — остаётся на прежних выпусках
    # С 4.0.0 бот ходит только к awg/awg-quick хоста; у контейнерной установки
    # интерфейсов на хосте нет — обновление сделало бы клиентов без обслуживания.
    # Ловушка второй половины вернёт прежний код и скажет в чат.
    local app="$CONF_DIR/app.yaml"
    [[ -f "$app" ]] || return 0
    if awk '/^docker:$/{inside=1; next} /^[^ #]/{inside=0} inside && /^  runtime:[[:space:]]*"?docker"?/{found=1} END{exit !found}' "$app"; then
        die "AmneziaWG в контейнере (docker.runtime: docker) — выпуск 4.0.0 такие установки не поддерживает; переведи AmneziaWG на хост прежним выпуском (awg-host-migrate) и обнови снова"
    fi
}

cmd_post_update() {
    # Вторая половина обновления, исполняется УЖЕ НОВЫМ скриптом (см. cmd_update).
    require_root
    trap post_update_rescue EXIT
    local wipe="${1:-0}"
    export AWG_IN_UPDATE=1                 # build_venv держит прежнее окружение до конца
    update_env_steps

    # удержание прошлого хода набора: ручной update, wipe и переход через
    # версию совместимости его снимают (откатываться туда больше нельзя или незачем)
    if [[ "$(hold_id)" == "0" || "$wipe" -eq 1 ]] || update_crosses_floor; then
        update_hold_drop
    fi

    if [[ "$wipe" -eq 1 ]]; then
        warn "удаляю данные и настройки по твоему запросу…"
        rm -rf "$DATA_DIR"/*.db "$DATA_DIR"/*.db-* "$DATA_DIR/.tz-adopted" 2>/dev/null || true
        rm -f "$ENV_FILE" 2>/dev/null || true
        rm -rf "$CONF_DIR" 2>/dev/null || true
        warn "данные удалены — потребуется reconfigure перед стартом."
        update_done
        trap - EXIT                            # это итог по запросу, не отказ: ловушке нечего спасать
        die "запусти: sudo awg-bot reconfigure"
    fi

    # Сторож — до старта: отметка жизни прошлых запусков стирается, таймер
    # взведён раньше, чем новый процесс может её написать. Не взвёлся — как
    # раньше: прежняя версия снимается сразу после старта.
    local guarded=0
    if update_guard_due; then
        update_guard_disarm                # сторож прошлого обновления, если ещё стоит
        update_guard_write
        if update_guard_arm; then
            guarded=1
        else
            warn "сторож обновления не взведён (systemd-run) — прежняя версия снимается сразу"
            rm -f "$UPDATE_GUARD"
        fi
    fi

    systemctl start "$SERVICE"; sleep 1
    if systemctl is-active --quiet "$SERVICE"; then
        ok "$SERVICE перезапущен."
        [[ "$guarded" -eq 1 ]] || prune_old_kernel_builds
    else
        warn "$SERVICE не активен — journalctl -u $SERVICE -e -o short-iso"
    fi
    if [[ "$guarded" -eq 1 ]]; then
        # прежние код, окружение и сборка ядра ждут отметку жизни нового процесса
        ok "прежняя версия хранится, пока новая не выйдет на связь: сторож посмотрит через $UPDATE_GUARD_MIN минут"
    else
        # без сторожа (переход через версию совместимости) назад дороги нет:
        # новый код мог уже тронуть базу
        update_done
    fi
    trap - EXIT
    ok "Обновление завершено."
    # временный каталог с архивом от установщика — только из /tmp, чужое не трогаем
    case "${AWG_UPDATE_CLEANUP:-}" in /tmp/*) rm -rf "$AWG_UPDATE_CLEANUP" ;; esac
    # Шлюз, подключаемый командой поверх стоящего агента другой версии
    # (установщик --join): подключение — теперь, новым кодом
    if [[ -n "${AWG_UPDATE_THEN_JOIN:-}" ]]; then
        AWG_JOIN_TOKEN="$AWG_UPDATE_THEN_JOIN" exec "$SELF_PATH" join
    fi
}

prune_old_kernel_builds() {
    # Прежние сборки ядра — долой, но ровно тогда, когда откат уже не нужен:
    #  • совместимая смена тега — после успешного старта на новом модуле, то
    #    есть здесь;
    #  • смена поколения — только на финале переезда профилей: до него старый
    #    интерфейс ещё обслуживает людей, и путь назад должен оставаться.
    [[ -f "$AWG_LOCK_FILE" ]] || return 0
    local want applied target
    want="$(lock_get AWG_GENERATION)"; applied="$(awg_state_get AWG_GENERATION_APPLIED)"
    target="$(awg_state_get AWG_GENERATION_TARGET)"
    if [[ -n "$target" || ( -n "$want" && -n "$applied" && "$want" -gt "$applied" ) ]]; then
        log "прежняя сборка ядра оставлена до финала переезда профилей"
        return 0
    fi
    AWG_LOCK="$AWG_LOCK_FILE" bash "$INSTALL_DIR/install/awg-kernel-install.sh" prune \
        || log "прежние сборки ядра не убраны — позже: awg-bot awg prune"
}

