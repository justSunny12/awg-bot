"""preflight.py — самопроверка окружения на старте.

Две градации:
  • FATAL   — стоп-факторы (битая БД/нет записи в data-dir; отвергнутый токен
    проверяется в main через getMe, причём fatal только на 401 — сетевые сбои
    переживаемы). Бегут ДО создания бота; при провале поднимаем PreflightError
    с человекочитаемым текстом в stderr — его видно в `journalctl -u awg-bot`,
    потому что бот ещё не готов слать в чат.
  • WARNING — не блокируют старт (мало места, контейнер молчит, конфиг с
    дефолтами). Собираются в список и уходят админу ПЕРВЫМ сообщением после
    успешного подъёма (см. main: send_startup_warnings).

Каждая проверка обёрнута в try: сам preflight не должен добавлять хрупкости —
если проверка не смогла отработать (docker временно недоступен и т.п.), это
максимум WARNING, но не падение бота на ровном месте.

config.validate() (обязательные секреты/топология) остаётся и зовётся отдельно
раньше — preflight его не дублирует, а дополняет проверками рантайма.
"""
from __future__ import annotations

import logging

import shutil
import sqlite3

from awgbot.core import config

log = logging.getLogger("awgbot.preflight")

# порог свободного места под data-dir, ниже которого — предупреждение
_DISK_WARN_MB = 200


class PreflightError(RuntimeError):
    """Fatal-провал preflight. Текст рассчитан на чтение человеком в journalctl."""


# ── FATAL ────────────────────────────────────────────────────────────────────
def check_fatal() -> None:
    """Стоп-факторы. Любой провал → PreflightError (бот не стартует).
    Токен getMe проверяется отдельно (async, в main) — здесь только локальное,
    что можно проверить синхронно и без сети."""
    problems: list[str] = []

    # data-dir существует и РЕАЛЬНО доступен на запись. os.access(W_OK) под root
    # бесполезен (root игнорит режим-биты), поэтому пробуем записать файл —
    # это ловит read-only mount / immutable / переполнение, где не пишет и root.
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = config.DATA_DIR / ".preflight_write_test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            problems.append(f"нет записи в data-dir {config.DATA_DIR} "
                            f"(ro-mount/переполнение?): {e}")
    except OSError as e:
        problems.append(f"data-dir недоступен ({config.DATA_DIR}): {e}")

    # БД открывается, не повреждена (тяжёлый integrity_check — по согласованию
    # включён: БД маленькая, а битую базу лучше поймать на старте, чем в бою)
    if config.DB_PATH.exists():
        try:
            con = sqlite3.connect(str(config.DB_PATH))
            try:
                res = con.execute("PRAGMA integrity_check").fetchone()
                if not res or str(res[0]).lower() != "ok":
                    problems.append(f"БД повреждена (integrity_check: {res[0] if res else '?'})")
            finally:
                con.close()
        except sqlite3.Error as e:
            problems.append(f"БД не открывается ({config.DB_PATH}): {e}")
    # если файла БД нет — это первый запуск, init_schema создаст; не проблема

    # Здесь стоял заслон на host-режим, пока перенос не был доделан. Снят: статус
    # сервиса, вотчдог, SSH-цели, рестарт и контейнерное плечо маршрутизации
    # портированы (docs/ROADMAP.md, шаг 2).

    if problems:
        raise PreflightError(
            "Проверка окружения не пройдена — бот не запущен:\n  • "
            + "\n  • ".join(problems)
            + "\nИсправьте и перезапустите (systemctl restart awg-bot).")


# ── WARNING ──────────────────────────────────────────────────────────────────
def collect_warnings_gateway(services=None) -> list[str]:
    """Warnings роли gateway: свои у каждой роли, потому что чинится разное.
    Клиентские проверки (awg-сервер, docker) шлюзу не о чем сказать — у него
    нет ни того, ни другого."""
    import os
    from awgbot.core import config as _c
    warns: list[str] = []
    warns += _service_autostart_warning()
    if services is not None and services.backup_env_leftover():
        warns.append("в /etc/awg-bot/env остались BACKUP_KEY/BACKUP_PASSPHRASE — секрет "
                     "перенесён в БД, шифрование настраивается из чата (💾 Резервное "
                     "копирование → 🔐 Шифрование); строки из env можно удалить")
    if not os.path.exists(_c.GW_LINK_CONF):
        warns.append(f"нет конфига линка {_c.GW_LINK_CONF} — линк не поднимется; "
                     f"шлюз ставится бандлом с ВПС (routing-link-setup.sh --bundle)")
    import subprocess
    rc = subprocess.run(["systemctl", "is-enabled", _c.GW_UNIT],
                        capture_output=True).returncode
    if rc != 0:
        warns.append(f"юнит {_c.GW_UNIT} не включён — после ребута обвязка "
                     f"шлюза не восстановится")
    if not _c.GW_CLIENT_SUBNET:
        warns.append("gateway.client_subnet не задан — проверка MASQUERADE "
                     "выключена (агент не увидит его пропажу)")
    return warns


def _firewall_warnings(services) -> list[str]:
    from awgbot.infra import nftguard
    if not nftguard.enabled():
        return ["файервол хоста не под управлением бота (firewall.enabled=false): "
                "SSH из туннеля не фильтруется по устройствам админа — "
                "выполните `awg-bot firewall setup`"]
    out: list[str] = []
    spec = nftguard.build_spec(services.db.admin_device_addresses(config.ADMIN_ID))
    if spec.ssh_open:
        out.append("firewall.ssh_allow пуст — SSH хоста открыт для всех адресов; "
                   "добавьте свои IP: `awg-bot firewall allow <ip>`")
    if spec.unresolved:
        out.append("firewall.ssh_allow: не резолвятся " + ", ".join(spec.unresolved))
    if not spec.udp_ports and not awg_in_container():
        out.append("файервол: не найден ListenPort ни в одном conf интерфейса — "
                   "порт клиентов awg может быть закрыт")
    if nftguard.ufw_active():
        out.append("ufw активен рядом с таблицей awg_bot_guard — два владельца "
                   "правил; после проверки входа: `awg-bot firewall confirm --disable-ufw`")
    return out


def awg_in_container() -> bool:
    from awgbot.infra import awg
    return awg.in_container()


def collect_warnings(services) -> list[str]:
    """Не-блокирующие замечания. Возвращает список строк для отправки админу.
    Каждая проверка изолирована: её собственный сбой не роняет остальные и не
    роняет бота — в худшем случае конкретная проверка молча пропускается."""
    warns: list[str] = []

    # Автозагрузка awg-интерфейса в host-режиме. Дыра, найденная ребутом ВПС:
    # в докерном режиме интерфейс поднимал контейнер, после переезда на хост
    # его должен поднимать awg-quick@<iface>, и если юнит не включён — после
    # ребута туннели не поднимутся, а узнать об этом можно только по ребуту.
    # Проверяем при каждом старте, пока это не исправлено.
    try:
        warns += _host_autostart_warnings()
    except Exception as e:                       # noqa: BLE001
        log.warning("preflight: проверка автозагрузки: %s", e)

    # файервол хоста: единственная точка — таблица awg_bot_guard под ботом
    try:
        warns += _firewall_warnings(services)
    except Exception as e:                       # noqa: BLE001
        log.warning("preflight: проверка файервола: %s", e)

    # свободное место под data-dir
    try:
        free_mb = shutil.disk_usage(config.DATA_DIR).free // (1024 * 1024)
        if free_mb < _DISK_WARN_MB:
            warns.append(f"мало места на диске: {free_mb} МБ свободно "
                         f"(порог {_DISK_WARN_MB} МБ) — бэкапы/логи могут не поместиться")
    except OSError as e:
        log.warning("preflight: проверка диска не удалась: %s", e)

    # awg отвечает. Совет по ремонту — по режиму: на хосте докера нет вовсе, и
    # «проверьте docker ps» отправило бы чинить не тот слой. Такая адресация уже
    # стоила нам часов при переезде (docs/ROADMAP.md, шаг 2).
    from awgbot.infra import awg
    _in_cont = awg.in_container()
    try:
        if not services.server_ok():
            warns.append(
                "контейнер AmneziaWG не отвечает на старте — "
                "проверьте `docker ps` и журнал контейнера" if _in_cont else
                f"awg не отвечает на старте — проверьте "
                f"`ip link show {config.AWG_INTERFACE}` и `awg show`")
    except Exception as e:                               # noqa: BLE001
        log.warning("preflight: проверка awg не удалась: %s", e)

    # серверный awg0.conf читается (единственная копия вне сервера — в бэкапе)
    try:
        awg.read_file(config.CONF_PATH)
    except Exception as e:                               # noqa: BLE001
        where = "в контейнере" if _in_cont else "на хосте"
        warns.append(f"не читается {config.CONF_PATH} {where} ({e}) — "
                     "выдача конфигов/реконсиляция могут не работать")

    # условная маршрутизация: инструменты на месте и namespace сходится.
    # Проверяем ТОЛЬКО если фича включена в конфиге — иначе она спит и мешать не
    # должна. Провал не блокирует старт: фича сама себя выключает (UI её прячет,
    # планировщик пропускает), VPN при этом работает как обычно.
    if config.ROUTING_ENABLED:
        try:
            ok, reason = services.routing_status()
            if not ok:
                warns.append(f"условная маршрутизация не поднимется ({reason}) — "
                             "российский IP у пользователей работать не будет, "
                             "остальное не затронуто")
            else:
                # Именно ЗАМЕР, а не routing_link_ok(): тот читает результат
                # прошлого тика, а на старте это сведения из прошлой жизни бота.
                from awgbot.infra import routing as _rt
                verdict = services.routing_probe()
                if verdict == _rt.PROBE_NO_PATH:
                    warns.append("шлюз условной маршрутизации отвечает, но интернета "
                                 "за ним нет — чинить на самом шлюзе (аплинк, "
                                 "ip_forward, MASQUERADE). Маркировка снята, "
                                 "российские сервисы временно открываются с "
                                 "зарубежного адреса")
                elif verdict != _rt.PROBE_OK:
                    warns.append("шлюз условной маршрутизации не отвечает на старте — "
                                 "маркировка снята, российские сервисы временно "
                                 "открываются с зарубежного адреса")
        except Exception as e:                           # noqa: BLE001
            log.warning("preflight: проверка маршрутизации не удалась: %s", e)

    # email-выход из приостановки: IMAP доступен и пускает по кредам. Проверяем
    # ТОЛЬКО если фича активна (заданы креды и хосты) — иначе она спит и мешать
    # не должна. Таймаут короткий: глухой хост не должен задерживать старт.
    # почта: ящик настроен — вход по IMAP должен проходить; иначе email-выход
    # из паузы молча не работает, и человек, заперевшийся в паузе, не выйдет
    try:
        if services.email_resume_enabled():
            ok, detail = services.email_check()
            if not ok:
                warns.append(f"почта: {detail} — аварийный email-выход из паузы не "
                             f"работает. Проверь ящик в «⚙️ Настройки → ✉️ E-mail»")
        if services.backup_env_leftover():
            warns.append("в /etc/awg-bot/env остались BACKUP_KEY/BACKUP_PASSPHRASE — секрет "
                         "перенесён в БД, шифрование настраивается из чата (💾 Резервное "
                         "копирование → 🔐 Шифрование); строки из env можно удалить")
        if services.email_env_leftover():
            warns.append("в /etc/awg-bot/env остались EMAIL_RESUME_LOGIN/PASSWORD — "
                         "почта теперь настраивается из чата (⚙️ Настройки → ✉️ E-mail), "
                         "строки из env можно удалить")
    except Exception as e:                       # noqa: BLE001
        log.warning("preflight: проверка почты: %s", e)

    return warns


def _unit_enabled(unit: str) -> str:
    """Вывод `systemctl is-enabled` (enabled/disabled/not-found/…), пусто —
    systemctl недоступен."""
    import subprocess
    try:
        proc = subprocess.run(["systemctl", "is-enabled", unit],
                              capture_output=True, timeout=5)
        return proc.stdout.decode(errors="replace").strip()
    except Exception:                            # noqa: BLE001
        return ""


def _service_autostart_warning() -> list[str]:
    """Сам юнит бота не включён — после ребута бот не поднимется. Ребут малины:
    установка агента оставила юнит disabled, и узнать об этом было неоткуда."""
    state = _unit_enabled("awg-bot")
    if state and state != "enabled":
        return [f"awg-bot не включён на автозагрузку ({state}) — после ребута бот "
                f"не поднимется. Исправить: systemctl enable awg-bot"]
    return []


def _host_autostart_warnings() -> list[str]:
    warns: list[str] = list(_service_autostart_warning())
    if config.AWG_RUNTIME != "host":
        return warns
    unit = f"awg-quick@{config.AWG_INTERFACE}"
    state = _unit_enabled(unit)
    if state and state != "enabled":
        warns.append(f"интерфейс {config.AWG_INTERFACE} не включён на автозагрузку "
                     f"({unit}: {state}) — после ребута туннели не поднимутся. "
                     f"Исправить: systemctl enable {unit}")
    # Остаток прежних версий: списки теперь обновляет сам бот, а юнит с
    # таймером указывает на удалённый скрипт и падает при каждой загрузке.
    stale = [u for u in ("awg-bot-lists.timer", "awg-bot-lists.service")
             if _unit_enabled(u) in ("enabled", "static", "failed", "linked")]
    if stale:
        warns.append("остались юниты старых версий: " + ", ".join(stale) +
                     " — списки обновляет сам бот. Убрать: "
                     "systemctl disable --now awg-bot-lists.timer awg-bot-lists.service; "
                     "rm -f /etc/systemd/system/awg-bot-lists.*; systemctl daemon-reload")
    return warns


def format_warnings(warns: list[str]) -> str:
    """Сообщение админу с накопленными предупреждениями старта. Тексты содержат
    str(e) — экранируем: угловая скобка в тексте ошибки не должна ломать
    HTML-отправку (иначе предупреждение молча потеряется)."""
    import html
    safe = [html.escape(w) for w in warns]
    return ("⚠️ <b>Замечания при запуске</b>\n\nБот работает, но обрати внимание:\n• "
            + "\n• ".join(safe))
