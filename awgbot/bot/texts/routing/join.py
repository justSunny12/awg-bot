"""join.py — подключение шлюза командой: сообщение подключения во всех
состояниях, приглашение кода, итоги, отмена, срок, карточка (Т-П1…Т-П15)."""

from __future__ import annotations

from awgbot.bot import ui
from awgbot.util import timeutil
from ..fmt import _e


def until_text(iso: str) -> str:
    """«13:07 (UTC+3)»; срок не сегодня — с датой (Т-П1)."""
    try:
        dt = timeutil.parse_iso(iso)
    except ValueError:
        return ""
    if dt.astimezone(timeutil.TZ).date() == timeutil.now().astimezone(timeutil.TZ).date():
        return timeutil.fmt_clock_ui(dt)
    return timeutil.fmt_dt_ui(dt)


def _nm(st: dict) -> str:
    """«Имя» слота: ссылкой на карточку, если её подставил обработчик
    (st["ref"] — уже HTML), иначе текстом."""
    return st.get("ref") or f"«{_e(st.get('name') or '')}»"


def _host_tail(host: str) -> str:
    return f" (hostname: {_e(host)})" if host else ""


def _head(st: dict) -> str:
    name = _e(st.get("name") or "")
    if st.get("kind") in ("replace", "same"):
        return ui.head(f"🔁 Замена устройства «{name}»")
    return ui.head(f"🛰 Подключение шлюза «{name}»")


def join_message(st: dict, command: str, *, event: str = "", addr: str = "") -> str:
    """Т-П1 с последним абзацем по состоянию: ждём (Т-П1), подключился (Т-П2),
    заново (Т-П2), с двух адресов (Т-П3)."""
    ver = _e(str(st.get("version") or ""))
    if st.get("kit"):
        # своя сборка сервера: выпуска на GitHub нет — только кит (flow-12)
        body = (f"Сервер AWG стоит не из выпуска — агента v{ver} на GitHub нет, шлюз (Debian или Raspberry Pi "
                "OS, часы синхронизированы) ставится китом сервера: <code>sudo awg-bot gw-kit --stdout</code> на "
                "сервере, как передать его шлюзу — <code>awg-bot help</code>. Киту нужен токен "
                "<code>AWGJ1…</code> из команды:")
    else:
        body = ("Выполни на шлюзе одну команду (Debian или Raspberry Pi OS, часы синхронизированы, нужен доступ к "
                f"GitHub) — она поставит AmneziaWG и агента v{ver} и подключит шлюз к серверу AWG:")
    hosts = st.get("hosts") or {}
    endpoints = list(st.get("endpoints") or [])
    last = addr or (endpoints[-1] if endpoints else "")
    if event == "connected":
        tail = (f"🟢 Шлюз подключился с адреса <code>{_e(last)}</code>{_host_tail(hosts.get(last, ''))} — введи "
                "код с экрана шлюза")
    elif event == "reconnected":
        tail = (f"🟢 Шлюз подключился заново с адреса <code>{_e(last)}</code>{_host_tail(hosts.get(last, ''))} — "
                "код на экране шлюза сменился, введи новый")
    elif event == "two":
        a, b = endpoints[-2], endpoints[-1]
        tail = (f"⚠️ К слоту подключались с двух адресов: <code>{_e(a)}</code>{_host_tail(hosts.get(a, ''))} и "
                f"<code>{_e(b)}</code>{_host_tail(hosts.get(b, ''))}. Если один из них не твой — отмени "
                "подключение и начни заново")
    else:
        tail = ("В конце шлюз покажет код подтверждения — введи его здесь кнопкой «✏️ Ввести код». Команда действует "
                f"час — до {until_text(st.get('expires') or '')} — и только для этого слота")
    return "\n\n".join([_head(st), body, f"<code>{_e(command)}</code>", tail])


JOIN_CODE_PROMPT = "\n\n".join([ui.head("🔑 Код с экрана шлюза"),
                                "Пришли 6 знаков из терминала шлюза, например <code>K7F-2QX</code>; регистр, пробелы "
                                "и дефис не важны"])
JOIN_CODE_WAIT = "Шлюз ещё не подключился — код появится на его экране в конце установки"
JOIN_CODE_FORMAT = "⚠️ Код — 6 букв и цифр, например K7F-2QX. Попробуй ещё раз"
JOIN_GONE = "Подключение больше не действует"


def join_code_wrong(left: int) -> str:
    return f"⚠️ Код не совпал — сверь с экраном шлюза и пришли ещё раз. Осталось попыток: {int(left)}"


def join_refused(st: dict) -> str:
    """Т-П7: попытки кончились."""
    name = _nm(st)
    if st.get("kind") in ("replace", "same"):
        return (f"⛔ Код не совпал трижды — подключение {name} закрыто, команда больше не действует — линк вернулся "
                f"прежнему устройству {name}; попробовать заменить снова — «✏️ Изменить» → «🔁 Заменить»")
    return (f"⛔ Код не совпал трижды — подключение {name} закрыто, команда больше не действует. Если на экране "
            "шлюза был твой код, выпусти новую команду: «🛰 Шлюзы» → «🛰 Подключить шлюз»")


def join_accepted(st: dict) -> str:
    """Т-П8: код верный — команды в сообщении больше нет."""
    return f"✅ Код верный — подключаю {_nm(st)}: постоянные ключи линка, аплинк, бот шлюза…"


def join_done(st: dict, version: str, bot: str) -> str:
    """Т-П9."""
    tail = f", бот шлюза @{_e(bot)} — открой его и нажми «Запустить»" if bot else ""
    ver = f": агент v{_e(version)}" if version else ""
    return f"✅ Шлюз {_nm(st)} подключён{ver}{tail}"


def join_cancelled(st: dict) -> str:
    """Т-П10."""
    name = _nm(st)
    if st.get("kind") in ("replace", "same"):
        return f"ℹ️ Замена отменена — линк вернулся прежнему устройству {name}, команда больше не действует"
    return f"ℹ️ Подключение {name} отменено — команда больше не действует"


def join_expired(st: dict) -> str:
    """Т-П11 (уведомление без звука и правка сообщения подключения)."""
    name = _nm(st)
    until = until_text(st.get("expires") or "")
    if st.get("kind") in ("replace", "same"):
        return (f"⚠️ Замена устройства {name} не завершилась за час, до {until} — линк вернулся прежнему "
                "устройству")
    return f"⚠️ Подключение {name} не завершилось за час, до {until} — команда больше не действует"


def join_stuck(st: dict) -> str:
    """Код верный, а шлюз за час так и не перешёл на постоянные ключи — откат."""
    name = _nm(st)
    if st.get("kind") in ("replace", "same"):
        return (f"⚠️ Замена устройства {name} не доведена: код был верный, но шлюз за час так и не перешёл на "
                "постоянные ключи линка — линк вернулся прежнему устройству")
    return (f"⚠️ Подключение {name} не доведено: код был верный, но шлюз за час так и не перешёл на постоянные "
            "ключи линка — подключение снято, команда больше не действует")


def join_stale(st: dict) -> str:
    """Линк на постоянных ключах, а агент за сутки не доложил конец подключения."""
    return (f"⚠️ {_nm(st)}: линк на постоянных ключах, но агент шлюза за сутки так и не доложил, что подключение "
            "доведено. Проверь шлюз: <code>sudo awg-bot join --status</code>")


def join_revert_failed(st: dict, err: str) -> str:
    """Возврат линка (отмена, срок, код) не удался — честно; такт повторит."""
    return (f"⚠️ Линк {_nm(st)} не вернулся: {_e(err)}. Команда больше не действует; повторю возврат "
            "через минуту")


JOIN_STALE = "ℹ️ Не актуально: выпущена новая команда — эта больше не действует"


def join_cancel_ask(st: dict) -> str:
    """Т-П14."""
    return "\n".join([f"↩️ Отменить замену {_nm(st)}?",
                      "Линк вернётся на ключи прежнего устройства, команда перестанет действовать"])


def join_card_line(card: dict) -> str:
    """Т-П15 — карточка и список шлюзов во время сопряжения."""
    if not card:
        return ""
    until = until_text(card.get("until") or "")
    if card.get("state") == "replace":
        return f"🔁 Замена устройства: жду новое до {until}"
    if card.get("state") == "code":
        return "⏳ Подключение: введи код с экрана шлюза"
    return f"🟡 Подключение: жду шлюз до {until}"


__all__ = ["join_stuck", "join_stale", "join_revert_failed", "until_text", "join_message", "JOIN_CODE_PROMPT", "JOIN_CODE_WAIT", "JOIN_CODE_FORMAT", "JOIN_GONE",
           "join_code_wrong", "join_refused", "join_accepted", "join_done", "join_cancelled", "join_expired",
           "JOIN_STALE", "join_cancel_ask", "join_card_line"]
