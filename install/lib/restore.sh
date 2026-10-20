# install/lib/restore.sh — часть awg-bot.sh: бэкап сервера AWG (`backup`) и
# восстановление со слиянием (`restore`, вторая половина
# `__restore_after_update` при переходе на выпуск бэкапа).
#
# Не запускается сам: awg-bot.sh читает его при разборе
# (`. "$SELF_DIR/install/lib/restore.sh"`); пути, утилиты, is_gateway_role,
# bot_tool, шаги окружения (update_env_steps) и уборка (update_done,
# prune_old_kernel_builds — install/lib/update.sh) — оттуда. Тесты склеивают
# части в один текст (tests/botscript.py).

# ── backup / restore: бэкап сервера AWG — один шифрованный архив ─────────────
GW_NO_BACKUPS="на шлюзе бэкапов нет: настройки шлюза — в бэкапе сервера AWG. Новое устройство подключается командой из бота сервера AWG, остальное довезёт канал конфигурации"
ROOT_GW_DIR="${AWG_ROOT_GW_DIR:-/root}"      # выданные шлюзовые стороны линков: /root/gw-<линк>.conf
SYS_NET="${AWG_SYS_NET:-/sys/class/net}"     # поднятые интерфейсы хоста

cmd_backup() {
    require_root; require_installed
    is_gateway_role && die "$GW_NO_BACKUPS"
    mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
    local ts out err; ts="$(date +%Y%m%d_%H%M%S)"; out="$BACKUP_DIR/awg-bot-backup-main-$ts.tgz.enc"
    # Сборщик один на все случаи (tools/snapshot, тот же состав, что копия из
    # чата): БД — через backup API SQLite; шифрование — фразой из базы. Без
    # фразы бэкапа нет: открытых бэкапов нет, а шифровать без кода установки
    # нечем — при сломанном коде бэкапа нет вовсе.
    err="$(bot_tool tools.snapshot --out "$out" 2>&1 >/dev/null)" || die "${err:-бэкап не снялся}"
    ok "бэкап: $out"
    log "шифрованный ключевой фразой — восстановить: sudo awg-bot restore $out"
}

awg_conf_dir() {
    # где лежат конфиги awg-интерфейсов хоста: awg.conf_dir в app.yaml
    # (умолчание /etc/amnezia/amneziawg); у шлюза — gateway.conf_dir
    local d; d="$(yaml_get "$CONF_DIR/app.yaml" conf_dir)"
    printf '%s' "${d:-/etc/amnezia/amneziawg}"
}

# Замена файлов живого — переименованием в том же каталоге: <имя>.restore-new
# готовится рядом с целью, заменяемый уходит в <имя>.restore-old, новый встаёт
# на его место (mv в одном каталоге атомарен). Отказ посреди замены — ловушка
# возвращает .restore-old на место и поднимает сервис; после последней замены
# .restore-old удаляются: это не копия и не точка возврата, а временный дубль
# внутри операции.
RESTORE_TARGETS=()        # цели в порядке замены (без .restore-new — только убрать в дубль)
RESTORE_DONE_T=()         # уже тронутые замены
RESTORE_HAD_OLD=()        # 1 — у тронутой цели был прежний файл (.restore-old)
# Фазы операции — от них зависит, что делает ловушка при любом выходе:
#   ""        до остановки сервиса: живое не тронуто, сервис работает;
#   stopped   сервис остановлен, живые файлы прежние (слияние, подготовка,
#             первый DROP): .restore-new прочь, сервис вверх;
#   swapping  идёт замена: заменённое возвращается из .restore-old, сервис вверх;
#   done      точка невозврата пройдена (замена удалась): только сервис вверх.
RESTORE_PHASE=""
RESTORE_ERR=""
RESTORE_FAIL=""           # причина отказа — в маркер для бота (копия из чата)
RESTORE_YES=0
RESTORE_TMP=""
RESTORE_PENDING=""
RESTORE_PLAN_FILE=""

json_str() {  # json_str TEXT → "TEXT" с экранированием для JSON (без переводов строк)
    local s="${1//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="$(printf '%s' "$s" | tr -d '\000-\037')"
    printf '"%s"' "$s"
}

restore_die() {  # restore_die ПРИЧИНА — отказ с причиной для маркера бота
    RESTORE_FAIL="$*"
    die "$*"
}

restore_stage() {  # restore_stage SRC TARGET MODE — подготовить TARGET.restore-new, если отличается
    cmp -s "$1" "$2" 2>/dev/null && return 0
    mkdir -p "$(dirname "$2")" || return 1
    cp "$1" "$2.restore-new" && chmod "$3" "$2.restore-new" || { rm -f "$2.restore-new"; return 1; }
    RESTORE_TARGETS+=("$2")
}

restore_unstage() {  # .restore-new прочь (отказ до замены)
    local t
    for t in ${RESTORE_TARGETS[@]+"${RESTORE_TARGETS[@]}"}; do rm -f -- "$t.restore-new"; done
}

restore_rollback() {  # вернуть заменённое: .restore-old на место, новое прочь
    local i t
    i=${#RESTORE_DONE_T[@]}
    while [[ "$i" -gt 0 ]]; do
        i=$(( i - 1 ))
        t="${RESTORE_DONE_T[$i]}"
        if [[ "${RESTORE_HAD_OLD[$i]}" == "1" ]]; then
            mv -f -- "$t.restore-old" "$t" 2>/dev/null || warn "не вернулся $t — прежний лежит в $t.restore-old"
        else
            rm -f -- "$t"
        fi
    done
    for t in ${RESTORE_TARGETS[@]+"${RESTORE_TARGETS[@]}"}; do rm -f -- "$t.restore-new"; done
    RESTORE_DONE_T=(); RESTORE_HAD_OLD=()
}

restore_mv() {  # restore_mv SRC DST — переименование; отказ — причина в RESTORE_ERR
    local errf="${RESTORE_TMP:-/tmp}/.mv-err"
    mv -f -- "$1" "$2" 2>"$errf" && return 0
    RESTORE_ERR="$2: $(cat "$errf" 2>/dev/null)"
    return 1
}

restore_swap() {  # restore_swap → 0 — всё заменено; 1 — отказ (причина в RESTORE_ERR)
    local t had
    RESTORE_PHASE=swapping
    for t in ${RESTORE_TARGETS[@]+"${RESTORE_TARGETS[@]}"}; do
        had=0
        if [[ -e "$t" ]]; then
            restore_mv "$t" "$t.restore-old" || return 1
            had=1
        fi
        RESTORE_DONE_T+=("$t"); RESTORE_HAD_OLD+=("$had")
        [[ -e "$t.restore-new" ]] || continue          # только убрать (журналы заменяемой базы)
        restore_mv "$t.restore-new" "$t" || return 1
    done
    return 0
}

restore_finish() {  # замена удалась — точка невозврата: временные дубли прочь
    local t
    for t in ${RESTORE_TARGETS[@]+"${RESTORE_TARGETS[@]}"}; do rm -f -- "$t.restore-old"; done
    RESTORE_PHASE=done
}

restore_prune_mark() {  # DROP первого наложения при откате остаются: старт бота снимет лишние
    : > "$DATA_DIR/.restore-prune" 2>/dev/null || true
}

restore_fail_marker() {  # отказ восстановления из чата — новый процесс бота скажет админу
    [[ "$RESTORE_YES" -eq 1 && -n "$RESTORE_FAIL" ]] || return 0
    mkdir -p "$DATA_DIR" 2>/dev/null || true
    printf '{"failed": %s, "release": %s, "from": %s}\n' "$(json_str "$RESTORE_FAIL")" \
        "$(json_str "${RESTORE_TO:-}")" "$(json_str "${RESTORE_FROM:-}")" > "$DATA_DIR/restore-failed.json" 2>/dev/null || true
}

restore_code_back() {  # переход на выпуск бэкапа не доведён: прежние код и окружение на место
    [[ "${RESTORE_CODE:-0}" -eq 1 ]] || return 0
    venv_rollback
    # ссылка — на окружение, с которым работал прежний код (restore_code_swap)
    if [[ -n "${RESTORE_VENV:-}" && -d "$RESTORE_VENV" ]]; then
        ln -sfn "$RESTORE_VENV" "$INSTALL_DIR/venv"
    fi
    [[ -n "${AWG_UPDATE_PREV:-}" && -d "$AWG_UPDATE_PREV" ]] && restore_prev_code "$AWG_UPDATE_PREV"
    warn "Переход на v${RESTORE_TO:-?} не удался: ${RESTORE_FAIL:-причина не названа} — ничего не изменилось, работает v${RESTORE_FROM:-?}"
    return 0
}

restore_kernel_back() {  # ядро выпуска бэкапа поставлено, а переход не доведён — ядро прежней версии назад
    [[ "${RESTORE_KERNEL:-0}" -eq 1 ]] || return 0
    RESTORE_KERNEL=0
    local lock="$INSTALL_DIR/install/awg.lock"
    if [[ -f "$lock" && -f "$INSTALL_DIR/install/awg-kernel-install.sh" ]] \
       && AWG_LOCK="$lock" bash "$INSTALL_DIR/install/awg-kernel-install.sh" install >/dev/null 2>&1; then
        log "AmneziaWG: ядро прежней версии v${RESTORE_FROM:-?} возвращено"
    else
        warn "AmneziaWG: на хосте осталось ядро выпуска v${RESTORE_TO:-?} — верни прежнее: sudo awg-bot awg install"
    fi
}

restore_exit() {  # ловушка операции: по фазе — откат, сервис вверх, временное прочь
    local rc=$?
    # голый die или errexit после «RESTORE_FAIL=» — причина для маркера всё равно есть
    [[ "$rc" -ne 0 && -z "$RESTORE_FAIL" && "$RESTORE_PHASE" != "done" ]] && RESTORE_FAIL="операция прервана (код $rc)"
    case "$RESTORE_PHASE" in
        stopped)
            restore_unstage
            restore_code_back
            restore_kernel_back
            restore_prune_mark
            restore_fail_marker
            systemctl start "$SERVICE" 2>/dev/null || true ;;
        swapping)
            restore_rollback
            restore_code_back
            restore_kernel_back
            restore_prune_mark
            restore_fail_marker
            systemctl start "$SERVICE" 2>/dev/null || true ;;
        done)
            systemctl is-active --quiet "$SERVICE" 2>/dev/null || systemctl start "$SERVICE" 2>/dev/null || true ;;
        *)
            # сервис не останавливали: бот работает и об отказе не знает —
            # перезапуск, чтобы новый процесс доложил его из маркера
            [[ "$rc" -ne 0 ]] && restore_kernel_back
            if [[ "$rc" -ne 0 && "$RESTORE_YES" -eq 1 && -n "$RESTORE_FAIL" ]]; then
                restore_fail_marker
                systemctl restart "$SERVICE" 2>/dev/null || true
            fi ;;
    esac
    RESTORE_PHASE=""
    [[ -n "$RESTORE_TMP" ]] && rm -rf -- "$RESTORE_TMP"
    [[ -n "$RESTORE_PENDING" ]] && rm -f -- "$RESTORE_PENDING"
    [[ -n "$RESTORE_PLAN_FILE" ]] && rm -f -- "$RESTORE_PLAN_FILE"
    [[ -n "${RESTORE_RELEASE:-}" ]] && rm -f -- "$RESTORE_RELEASE"
    return "$rc"
}

restore_blocks() {  # DROP заблокированным из итога слияния — тем же входом, что у бота
    bot_tool tools.restore_merge --apply-blocks "$1"
}

restore_provision() {  # новый сервер, обвязка в бэкапе развёрнута, на хосте — нет: развернуть до линков
    local base="$INSTALL_DIR/install"
    if ! systemctl list-unit-files dnsmasq.service 2>/dev/null | grep -q dnsmasq.service; then
        apt-get install -y --no-install-recommends dnsmasq >/dev/null 2>&1 \
            || { warn "dnsmasq не поставился — обвязку РФ-доступа разверни из чата бота"; return 1; }
    fi
    sh "$base/routing-host-setup.sh" --apply >/dev/null \
        && sh "$base/routing-host-setup.sh" --install-unit >/dev/null \
        || { warn "обвязка хоста не развернулась — разверни её из чата бота"; return 1; }
}

cmd_restore() {
    require_root
    is_gateway_role && die "$GW_NO_BACKUPS"
    # --yes: без вопросов (из чата: бот уже показал план и спросил админа);
    # итог и отказ — маркером для бота. --release <архив> — выпуск бэкапа,
    # уже скачанный ботом (sha256 = digest метки); без него терминал скачает сам.
    local yes=0 release=""
    while [[ "${1:-}" == --* ]]; do
        case "$1" in
            --yes) yes=1; shift ;;
            --release) release="${2:-}"; shift 2 || shift ;;
            *) break ;;
        esac
    done
    local src="${1:-}"
    if [[ -n "$src" ]]; then
        [[ -f "$src" ]] || die "не найден: $src"
    else
        [[ -d "$BACKUP_DIR" ]] || die "нет каталога бэкапов $BACKUP_DIR — укажи путь: awg-bot restore <файл>"
        src="$(ls -1t "$BACKUP_DIR"/awg-bot-backup-*.tgz.enc 2>/dev/null | head -n1 || true)"
        [[ -n "$src" ]] || die "бэкапов не найдено в $BACKUP_DIR — укажи путь: awg-bot restore <файл>"
    fi
    RESTORE_TARGETS=(); RESTORE_DONE_T=(); RESTORE_HAD_OLD=(); RESTORE_PHASE=""; RESTORE_FAIL=""
    RESTORE_YES="$yes"; RESTORE_CODE=0; RESTORE_KERNEL=0; RESTORE_TO=""; RESTORE_FROM="$(tree_version "$INSTALL_DIR")"
    RESTORE_RELEASE="$release"
    RESTORE_TMP="$(mktemp -d)"; chmod 700 "$RESTORE_TMP"
    # Файл из чата бот кладёт шифрованным (restore-pending.tgz.enc) и рядом —
    # план из вопроса: при любом исходе оба удаляются, расшифрованное живёт
    # только во временном каталоге.
    RESTORE_PENDING=""; RESTORE_PLAN_FILE=""
    if [[ "$(basename "$src")" == "restore-pending.tgz.enc" ]]; then
        RESTORE_PENDING="$src"; RESTORE_PLAN_FILE="$(dirname "$src")/restore-pending.plan.json"
    fi
    trap restore_exit EXIT
    # тот же замок, что у update, сторожа, фиксации и отката набора: переход на
    # выпуск бэкапа меняет код; __restore_after_update наследует замок через exec
    update_lock -n || restore_die "уже идёт обновление, фиксация или откат — ничего не изменилось"
    local tmp="$RESTORE_TMP" copy="$RESTORE_TMP/copy" err expect="$RESTORE_TMP/plan.json" relsrc=""

    # ── всё, что может отказать, — до остановки сервиса ──
    [[ "$(head -c 6 "$src" 2>/dev/null)" == "AWGBK1" ]] \
        || restore_die "бэкап не зашифрован — восстанавливаются только шифрованные бэкапы (.tgz.enc)"
    if [[ -n "$RESTORE_PENDING" ]]; then
        bot_tool tools.restore_backup --phrase-from-db --out "$tmp/backup.tgz" "$src" >/dev/null 2>&1 \
            || restore_die "не расшифровался — ключевая фраза в базе некорректна или файл повреждён"
    else
        log "шифрованный бэкап — нужна ключевая фраза"
        bot_tool tools.restore_backup --out "$tmp/backup.tgz" "$src" >/dev/null \
            || restore_die "не расшифровался — ключевая фраза некорректна или файл повреждён"
    fi
    mkdir -p "$copy"
    tar xf "$tmp/backup.tgz" -C "$copy" --no-same-owner || restore_die "не удалось распаковать бэкап"
    rm -f "$tmp/backup.tgz"
    [[ -f "$copy/state/backup-meta.json" ]] || restore_die "указанный файл не похож на бэкап"
    err="$(bot_tool tools.check_backup --meta "$copy/state/backup-meta.json" 2>&1 >/dev/null)" \
        || restore_die "${err:-метка бэкапа не прошла проверку}"
    # Версия бэкапа другая — переход на его выпуск (domain/fleet/restore.py):
    # выпуск с GitHub, sha256 = digest метки; всё — до остановки сервиса
    local rel_out
    rel_out="$(bot_tool tools.restore_release --meta "$copy/state/backup-meta.json" --out "$tmp/release.tgz" \
               ${release:+--archive "$release"} 2>"$tmp/release.err")" \
        || restore_die "$(cat "$tmp/release.err" 2>/dev/null || echo "выпуск бэкапа не проверен")"
    local rel_first rel_lines=""; rel_first="$(printf '%s\n' "$rel_out" | head -n1)"
    if [[ -n "$rel_first" && "$rel_first" != "same" ]]; then
        RESTORE_TO="$rel_first"
        RESTORE_DIGEST="$(sed -n 's/.*"digest": *"sha256:\([0-9a-f]*\)".*/\1/p' "$copy/state/backup-meta.json" | head -n1)"
        relsrc="$(restore_release_tree "$tmp/release.tgz" "$tmp/release")" || restore_die "${relsrc:-архив выпуска не распаковался}"
        # повышение через версию совместимости выпуска: его код данных этой
        # версии не принимает — сначала обычное обновление по цепочке минимумов
        local vfloor; vfloor="$(compat_floor "$relsrc")"
        if [[ -n "$vfloor" && -n "$RESTORE_FROM" ]] && version_lt "$RESTORE_FROM" "$vfloor"; then
            restore_die "выпуск v$RESTORE_TO принимает переход только с v$vfloor и новее, а здесь v$RESTORE_FROM — сначала обнови сервер AWG до v$vfloor кнопкой «⬆️ Обновить», затем восстанови бэкап"
        fi
        rel_lines="$(printf '%s\n' "$rel_out" | tail -n +2)"    # строка версии — в план, после заголовка
    fi
    local plan
    plan="$(bot_tool tools.restore_merge --plan "$copy" --save "$expect" 2>&1)" || restore_die "$plan"
    # из чата человек видел план в вопросе бота — сверяем с ним
    [[ -n "$RESTORE_PLAN_FILE" && -f "$RESTORE_PLAN_FILE" ]] && cp "$RESTORE_PLAN_FILE" "$expect"
    if [[ -n "$rel_lines" ]]; then
        # как в вопросе бота: заголовок плана, строка версии, остальное
        printf '%s\n%s\n' "$(printf '%s\n' "$plan" | head -n1)" "$rel_lines"
        printf '%s\n\n' "$(printf '%s\n' "$plan" | tail -n +2)"
    else
        printf '%s\n\n' "$plan"
    fi
    [[ "$yes" -eq 1 ]] || confirm "Продолжить?" n || die "отменено"
    if [[ -n "$RESTORE_TO" && -f "$relsrc/install/awg.lock" ]]; then
        # ядро выпуска бэкапа — до остановки, как у update: не собралось — ничего не тронуто
        log "AmneziaWG: версия выпуска v$RESTORE_TO — собираю до остановки сервиса…"
        RESTORE_KERNEL=1; export RESTORE_KERNEL       # с этого места отказ — ядро прежней версии назад
        AWG_LOCK="$relsrc/install/awg.lock" bash "$relsrc/install/awg-kernel-install.sh" install \
            || restore_die "ядро AmneziaWG выпуска v$RESTORE_TO не собралось — ничего не изменилось"
    fi
    local bk_at
    bk_at="$(grep -o '"created_at": *"[^"]*"' "$copy/state/backup-meta.json" | sed 's/.*"\([^"]*\)"$/\1/')"
    # Каталог конфигов интерфейсов и ключ timezone — по ТЕКУЩЕМУ app.yaml, до
    # его подмены: иначе бэкап сам выбирал бы, куда положить свои конфиги.
    local awgdir; awgdir="$(awg_conf_dir)"
    local tz_had=0
    grep -qE '^timezone:' "$CONF_DIR/app.yaml" 2>/dev/null && tz_had=1

    [[ -n "$RESTORE_TO" ]] && update_guard_disarm       # сторож прежнего обновления — код сменится
    log "останавливаю ${SERVICE}…"; systemctl stop "$SERVICE" 2>/dev/null || true
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        restore_die "$SERVICE не остановился — ничего не изменилось"
    fi
    RESTORE_PHASE=stopped
    RESTORE_COPY="$copy"; RESTORE_EXPECT="$expect"; RESTORE_BK_AT="$bk_at"; RESTORE_AWGDIR="$awgdir"
    RESTORE_TZ_HAD="$tz_had"; RESTORE_SRC="$src"
    if [[ -n "$RESTORE_TO" ]]; then
        # переход на выпуск бэкапа: первая половина update без старта сервиса,
        # дальше — слияние и замена руками скрипта выпуска бэкапа (новый код
        # читает живую базу и сливает её своим кодом); отказ — прежний код назад
        restore_code_swap "$relsrc"
        # Состояние операции — второй половине через окружение: договор между
        # версиями (скрипт vC передаёт, скрипт vV читает), имена не меняются.
        # RESTORE_YES/TMP/PENDING/PLAN_FILE скрипт обнуляет при загрузке — им
        # отдельные имена AWG_RESTORE_*
        export AWG_RESTORE_YES="$RESTORE_YES" AWG_RESTORE_TMP="$RESTORE_TMP" AWG_RESTORE_PENDING="$RESTORE_PENDING"
        export AWG_RESTORE_PLAN_FILE="$RESTORE_PLAN_FILE"
        export RESTORE_TO RESTORE_FROM RESTORE_DIGEST
        export RESTORE_COPY RESTORE_EXPECT RESTORE_BK_AT RESTORE_AWGDIR RESTORE_TZ_HAD RESTORE_SRC RESTORE_RELEASE
        export AWG_UPDATE_PREV="$INSTALL_DIR/.prev-code"
        RESTORE_FAIL="скрипт выпуска v$RESTORE_TO не запустился"    # exec не прошёл — причина для маркера
        # execfail: неудачный exec не обрывает процесс мимо ловушки — дальше
        # restore_die, и ловушка вернёт прежние код, окружение и ядро
        shopt -s execfail
        exec "$INSTALL_DIR/awg-bot.sh" __restore_after_update
        restore_die "$RESTORE_FAIL"
    fi
    restore_apply
}

restore_release_tree() {  # restore_release_tree TGZ DIR → печатает каталог поставки (или причину, код 1)
    mkdir -p "$2"
    tar xzf "$1" -C "$2" --no-same-owner 2>/dev/null || { echo "архив выпуска не распаковался"; return 1; }
    local main; main="$(find "$2" -maxdepth 3 -type f -path '*/awgbot/__main__.py' | head -n1)"
    [[ -n "$main" ]] || { echo "в архиве выпуска нет awgbot/ — не та поставка"; return 1; }
    local src; src="$(dirname "$(dirname "$main")")"
    [[ "$(tree_version "$src")" == "$RESTORE_TO" ]] \
        || { echo "архив выпуска v$RESTORE_TO несёт другую версию — переходить не буду"; return 1; }
    # вторая половина — скриптом выпуска: без неё exec оставил бы сервис стоять
    grep -q '__restore_after_update)' "$src/awg-bot.sh" 2>/dev/null \
        || { echo "выпуск v$RESTORE_TO не умеет восстанавливать с переходом — переходить не буду"; return 1; }
    chown -R root:root "$src" 2>/dev/null || true
    printf '%s' "$src"
}

restore_code_swap() {  # restore_code_swap SRC — код выпуска бэкапа на место (прежний — в .prev-code)
    local prev="$INSTALL_DIR/.prev-code"
    rm -rf "$prev"; mkdir -p "$prev"
    RESTORE_CODE=1; AWG_UPDATE_PREV="$prev"                 # с этого места отказ — прежний код назад
    # Окружение кода vC — явно: .venvs/.prev мог остаться от прежнего
    # обновления (сторож снят выше) и указывать на окружение vB — откат
    # поставил бы код vC на чужое окружение. Вторая половина (скрипт vV)
    # читает RESTORE_VENV из окружения и возвращает ссылку на него
    RESTORE_VENV=""
    [[ -L "$INSTALL_DIR/venv" ]] && RESTORE_VENV="$(readlink -f "$INSTALL_DIR/venv")"
    mkdir -p "$INSTALL_DIR/.venvs"
    if [[ -n "$RESTORE_VENV" ]]; then
        printf '%s\n' "$RESTORE_VENV" > "$INSTALL_DIR/.venvs/.prev"
    else
        rm -f "$INSTALL_DIR/.venvs/.prev"
    fi
    export RESTORE_VENV
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name venv ! -name .venvs ! -name .prev-code ! -name .hold-code \
        -exec mv -t "$prev" {} + || restore_die "прежний код не отодвинулся"
    cp -a "$1"/. "$INSTALL_DIR"/ || restore_die "код выпуска v$RESTORE_TO не лёг целиком"
    chmod +x "$INSTALL_DIR/awg-bot.sh" 2>/dev/null || true
    [[ -x "$INSTALL_DIR/awg-bot.sh" && -f "$INSTALL_DIR/awgbot/__main__.py" ]] \
        || restore_die "код выпуска v$RESTORE_TO лёг не полностью"
}

cmd_restore_after_update() {  # __restore_after_update — вторая половина: скрипт выпуска бэкапа
    # Окружение, юнит и досев conf — как во второй половине update, без старта;
    # затем слияние и замена этим кодом. Отказ до точки невозврата — прежние
    # код и окружение назад (restore_exit), сервис поднят на прежней версии.
    require_root
    RESTORE_TARGETS=(); RESTORE_DONE_T=(); RESTORE_HAD_OLD=(); RESTORE_FAIL=""; RESTORE_CODE=1
    RESTORE_YES="${AWG_RESTORE_YES:-0}"; RESTORE_TMP="${AWG_RESTORE_TMP:-}"
    RESTORE_PENDING="${AWG_RESTORE_PENDING:-}"; RESTORE_PLAN_FILE="${AWG_RESTORE_PLAN_FILE:-}"
    [[ -n "${RESTORE_TO:-}" && -n "${AWG_UPDATE_PREV:-}" ]] || die "__restore_after_update — только из awg-bot restore"
    RESTORE_PHASE=stopped
    trap restore_exit EXIT
    [[ -n "${RESTORE_COPY:-}" && -d "$RESTORE_COPY" ]] || restore_die "распакованный бэкап потерялся по дороге"
    export AWG_IN_UPDATE=1
    RESTORE_FAIL="окружение выпуска v$RESTORE_TO не собралось"   # die посреди шагов — причина для маркера
    update_env_steps restore                    # те же шаги, что у второй половины update
    RESTORE_FAIL=""
    restore_apply
}

restore_apply() {  # после остановки: слияние, подготовка, DROP, замена, подъём, маркер, старт
    local copy="$RESTORE_COPY" expect="$RESTORE_EXPECT" bk_at="$RESTORE_BK_AT" awgdir="$RESTORE_AWGDIR"
    local tz_had="$RESTORE_TZ_HAD" src="$RESTORE_SRC" err
    # Слияние — после остановки: читает живую базу неподвижной, пишет только в
    # распакованный бэкап и сверяет план с вопросом. Отказ — живое не тронуто.
    if ! err="$(bot_tool tools.restore_merge --copy "$copy" --expect "$expect" 2>&1 >/dev/null)"; then
        restore_die "${err:-слияние не прошло} — изменения отменены, сервис поднят"
    fi

    # ── подготовка: каждый заменяемый файл — рядом с целью ──
    local f i c changed="" hooked="" prep=0
    mkdir -p "$DATA_DIR" "$CONF_DIR"
    # Журналы WAL/SHM принадлежат ЗАМЕНЯЕМОЙ базе: рядом с чужой SQLite в
    # лучшем случае их отбросит, в худшем доложит в неё чужие страницы — они
    # уходят в дубль первыми (и возвращаются при откате последними).
    for f in "$DATA_DIR/bot.db-wal" "$DATA_DIR/bot.db-shm"; do
        [[ -e "$f" ]] && RESTORE_TARGETS+=("$f")
    done
    if [[ -f "$copy/state/bot.db" ]]; then
        restore_stage "$copy/state/bot.db" "$DATA_DIR/bot.db" 600 || prep=1
    fi
    for f in "$copy/state/conf"/*.yaml; do
        [[ -f "$f" ]] || continue
        restore_stage "$f" "$CONF_DIR/$(basename "$f")" 644 || prep=1
    done
    if [[ -f "$copy/state/env" ]]; then
        restore_stage "$copy/state/env" "$ENV_FILE" 600 || prep=1
    fi
    for c in "$copy/awg"/*.conf; do
        [[ -f "$c" ]] || continue
        i="$(basename "$c" .conf)"
        cmp -s "$c" "$awgdir/$i.conf" 2>/dev/null && continue
        changed="$changed $i"
        # Конфиг с ДРУГИМИ хуками (PreUp/PostUp/PreDown/PostDown) сам не
        # поднимаем: awg-quick исполняет их от root. Класть — кладём.
        if ! cmp -s <(grep -E '^(Pre|Post)(Up|Down) *=' "$c" 2>/dev/null) \
                    <(grep -E '^(Pre|Post)(Up|Down) *=' "$awgdir/$i.conf" 2>/dev/null); then
            hooked="$hooked $i"
        fi
        restore_stage "$c" "$awgdir/$i.conf" 600 || prep=1
    done
    for c in "$copy/root"/gw-*.conf; do
        [[ -f "$c" ]] || continue
        restore_stage "$c" "$ROOT_GW_DIR/$(basename "$c")" 600 || prep=1
    done
    [[ "$prep" -eq 0 ]] || restore_die "подготовка не прошла — изменения не применены, сервис поднят"

    # ── DROP заблокированным — до подъёма интерфейсов; не встал — замены нет ──
    restore_blocks "$copy/restore-merge.json" \
        || restore_die "DROP заблокированным не наложен — ничего не изменилось, сервис поднят"

    # ── замена ──
    if ! restore_swap; then
        restore_die "замена не прошла — вернул прежнее, сервис поднят: ${RESTORE_ERR:-причина не названа}"
    fi
    [[ "$tz_had" -eq 1 ]] || adopt_timezone quiet
    restore_finish                                      # точка невозврата: дальше — только вперёд
    if [[ "${RESTORE_CODE:-0}" -eq 1 ]]; then
        # переход на выпуск бэкапа доведён: digest установки — digest метки
        # (бэкапы снова со ссылкой на выпуск); прежние код, окружение и ядро
        # снимаются только после старта сервиса (ниже)
        update_hold_drop                                # удержание прошлого хода — о другом коде
        if [[ -n "${RESTORE_DIGEST:-}" ]]; then
            ( umask 077; printf '%s %s\n' "$RESTORE_TO" "$RESTORE_DIGEST" > "$DATA_DIR/installed-digest" )
        fi
    fi

    # ── новый сервер: обвязка РФ-доступа из бэкапа — до линков ──
    if grep -q '"provision": *true' "$copy/restore-merge.json" 2>/dev/null; then
        restore_provision || true
    fi
    # ── подъём изменившихся интерфейсов, линки пустого сервера — юнитом ──
    for i in $changed; do
        case " $hooked " in *" $i "*)
            warn "$i: в бэкапе другие хуки Pre/Post — положен, но не поднят: проверь и подними руками (awg-quick up $i)"
            continue ;;
        esac
        if [[ -d "$SYS_NET/$i" ]]; then
            awg-quick down "$i" >/dev/null 2>&1 || true
            awg-quick up "$i" >/dev/null 2>&1 || warn "$i не поднялся"
        fi
    done
    local adopt
    adopt="$(sed -n 's/.*"adopt": *\[\([^]]*\)\].*/\1/p' "$copy/restore-merge.json" | tr -d '" ' | tr ',' ' ')"
    for i in $adopt; do
        [[ -n "$i" ]] || continue
        LINK_IF="$i" sh "$INSTALL_DIR/install/routing-link-setup.sh" --adopt >/dev/null \
            || warn "линк $i не поднялся — бот повторит попытку на старте"
    done
    # линк пары серверов (резерв): конфиг из копии — юнит и подъём
    if [[ -f "$awgdir/awgpair.conf" && -f "$INSTALL_DIR/install/reserve-pair-setup.sh" ]]; then
        PAIR_IF=awgpair sh "$INSTALL_DIR/install/reserve-pair-setup.sh" --adopt >/dev/null \
            || warn "линк пары не поднялся — бот повторит попытку на старте"
    fi
    # ── после подъёма, до старта сервиса — то же наложение ещё раз ──
    restore_blocks "$copy/restore-merge.json" || warn "не все DROP наложены — сверка бота на старте поправит"
    [[ -n "$changed" ]] && ok "изменившиеся интерфейсы восстановлены:$changed"

    # маркер для бота: новый процесс доложит итог по каждому шлюзу
    { printf '{"created_at": %s, "source": %s, "version": %s, "merge": ' "$(json_str "$bk_at")" \
          "$(json_str "$(basename "$src")")" "$(json_str "${RESTORE_TO:-}")"
      cat "$copy/restore-merge.json"; printf '}\n'; } > "$DATA_DIR/restore-done.json"
    systemctl start "$SERVICE" 2>/dev/null || true; sleep 1
    if systemctl is-active --quiet "$SERVICE"; then
        ok "восстановлено, $SERVICE запущен."
        if [[ "${RESTORE_CODE:-0}" -eq 1 ]]; then
            update_done                                 # сервис на выпуске бэкапа жив: прежнее прочь
            prune_old_kernel_builds
        fi
    else
        warn "$SERVICE не активен — journalctl -u $SERVICE -e -o short-iso"
        if [[ "${RESTORE_CODE:-0}" -eq 1 ]]; then
            warn "прежний код v${RESTORE_FROM:-?} оставлен в $INSTALL_DIR/.prev-code — для ручного разбора"
        fi
    fi
}

