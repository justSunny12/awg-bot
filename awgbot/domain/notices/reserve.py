"""reserve.py — часть пакета notices (см. __init__): уведомления пары серверов
из доменного слоя — связь с резервом (Т-РУ8), совместимость направлений
(Т-РУ9), смена админа второго сервера, удаление пары второй стороной; переезд
и возврат клиентов — админу резерва (у меня клиенты основы), админу основы
(клиенты на резерве; из бота основы на резерве), итоги возврата, запись DNS,
клиентам («переподключи VPN»). Имена серверов — «имя», адреса и имена записей
моноширинным, без точки в конце; «один раз на расхождение»."""
from __future__ import annotations

import html

from awgbot.util import timeutil


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _n(name: str) -> str:
    return f"«{_e(name)}»"


def _when(iso: str) -> str:
    try:
        return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
    except (ValueError, TypeError):
        return ""


def peer_down(peer: str, since: str = "") -> str:
    """Т-РУ8: линк пары молчит."""
    at = _when(since)
    return f"🔴 Нет связи с резервом {_n(peer)}" + (f" с {at}" if at else "") + " — пока её нет, клиенты к нему не переедут"


def peer_up(peer: str) -> str:
    return f"🟢 Связь с резервом {_n(peer)} восстановлена"


def peer_unpaired(peer: str) -> str:
    return f"ℹ️ {_n(peer)} удалил пару: резерва больше нет, его данные с этого сервера сняты"


def direction_off(peer: str, why: str) -> str:
    """Т-РУ9: направление выключилось (версия, возможности)."""
    return f"⚠️ Резерв {_n(peer)}: {_e(why)}"


def direction_on(peer: str) -> str:
    return f"🟢 Резерв {_n(peer)} снова совместим"


def peer_admin_changed(peer: str) -> str:
    return f"ℹ️ У {_n(peer)} сменился админ — его прежний аккаунт потерял доступ к экрану резерва в этом боте"


# ── админу резерва ───────────────────────────────────────────────────────────

def hosting_started(primary: str, kind: str, since: str, extra: str = "") -> str:
    """Т-РУ4: клиенты и бот основы у меня; kind — button | dead | rf | nogw | code."""
    if kind == "button":
        return f"📥 {_n(primary)} перевёл к тебе клиентов и бот"
    at = _when(since)
    if kind == "rf":
        return f"📥 Клиенты и бот {_n(primary)} у тебя: {_n(primary)} недоступен из РФ с {at} — его шлюзы его не видят, этот сервер видит"
    if kind == "nogw":
        return f"📥 Клиенты и бот {_n(primary)} у тебя: его шлюзы не на связи с этим сервером с {at} — РФ-доступ его клиентов идёт через твои шлюзы"
    if kind == "code":
        return f"📥 Клиенты и бот {_n(primary)} у тебя по аварийному коду его админа: {_n(primary)} и его шлюзы не отвечают с {at}"
    text = f"📥 Клиенты и бот {_n(primary)} у тебя: {_n(primary)} не отвечает с {at} ни этому серверу, ни своим шлюзам"
    return text + (f"; {_e(extra)}" if extra else "")


def hosting_ended(primary: str, traffic: str) -> str:
    """Т-РУ5: клиенты вернулись к основе."""
    tail = f" — через тебя прошло {_e(traffic)}" if traffic else ""
    return f"↩️ Клиенты и бот {_n(primary)} вернулись к нему{tail}, данные {_n(primary)} сняты"


def hosting_failed(primary: str, reason: str) -> str:
    return f"⚠️ Клиентов {_n(primary)} принять не удалось: {_e(reason)}"


# ── админу основы (из бота основы на резерве) ───────────────────────────────

def moved_to_reserve(reserve: str, kind: str, since: str, dead_since: str = "", reserve_ip: str = "") -> str:
    """Т-РУ4а: клиенты и бот работают на резерве; kind как у hosting_started."""
    at, dead = _when(since), _when(dead_since)
    mailed = "клиентам на связи ушло «переподключи VPN»"
    if kind == "button":
        return f"✅ Клиенты и бот — на {_n(reserve)} с {at}; {mailed}"
    if kind == "rf":
        tail = f". Следующим из РФ могут закрыть и адрес {_n(reserve)} <code>{_e(reserve_ip)}</code>" if reserve_ip else ""
        return (f"🚫 Сервер AWG недоступен из РФ с {dead}: шлюзы его не видят, резерв {_n(reserve)} видит — клиенты и бот "
                f"работают на {_n(reserve)} с {at}; {mailed}{tail}")
    if kind == "nogw":
        return (f"🛟 Шлюзы не на связи с резервом {_n(reserve)} с {dead} — клиенты и бот работают на {_n(reserve)} с {at}, "
                f"РФ-доступ идёт через шлюзы {_n(reserve)}; {mailed}. Вернутся шлюзы — клиенты вернутся сюда")
    if kind == "code":
        return (f"🔴 Сервер AWG и шлюзы не отвечали резерву {_n(reserve)} с {dead} — по твоему аварийному коду клиенты и бот "
                f"работают на {_n(reserve)} с {at}; {mailed}")
    return (f"🔴 Сервер AWG не отвечает с {dead} ни резерву, ни шлюзам — клиенты и бот работают на резерве {_n(reserve)} "
            f"с {at}; {mailed}")


def returned_home(reserve: str, report: dict, traffic: str) -> str:
    """Т-РУ5б: клиенты и бот снова на основе; итог слияния."""
    parts = []
    if report.get("clients"):
        parts.append(f"{int(report['clients'])} новых профиля")
    if report.get("devices"):
        parts.append(f"{int(report['devices'])} новых устройства")
    if report.get("extensions"):
        parts.append(f"{int(report['extensions'])} продления")
    if report.get("blocks"):
        parts.append(f"{int(report['blocks'])} блокировки")
    body = ", ".join(parts) if parts else "без новых профилей и устройств"
    text = f"↩️ Клиенты и бот снова здесь. За время на {_n(reserve)}: {body}"
    if traffic:
        text += f" · через резерв {_e(traffic)}"
    for lost in report.get("lost") or []:
        text += "\n\n" + _e(lost)
    return text


def lost_device(name: str, owner: str, taken_by: str, taken_owner: str, reserve: str) -> str:
    """Строка конфликта для Т-РУ5б."""
    return (f"⚠️ Не встало устройство «{_e(name)}» профиля {_e(owner)} — его адрес занял «{_e(taken_by)}» профиля "
            f"{_e(taken_owner)}, заведённый на {_n(reserve)}. Выдай «{_e(name)}» конфигурацию заново")


def primary_back(auto: bool) -> str:
    """Т-РУ6 (из бота основы на резерве): сервер основы снова в строю."""
    if auto:
        return "🟢 Сервер AWG снова в строю — возвращаю клиентов и бот"
    return "🟢 Сервер AWG снова в строю — верни клиентов кнопкой"


def move_failed(reserve: str, reason: str, released: bool = False) -> str:
    text = f"⚠️ Перевод не состоялся: {_e(reason)}"
    return text + (f"; {_n(reserve)} отпустил клиентов" if released else "")


def record_not_changed(name: str, reason: str, ip: str) -> str:
    """Т-РУ11."""
    return (f"⚠️ Запись <code>{_e(name)}</code> не изменена: {_e(reason)} — смени её у провайдера на "
            f"<code>{_e(ip)}</code>")


def record_foreign(name: str, ip: str) -> str:
    """Т-РУ16: запись указывает не сюда, переезда не было."""
    return (f"🚨 Запись <code>{_e(name)}</code> указывает на <code>{_e(ip)}</code>, а не на этот сервер, — клиенты "
            "уходят туда. Переезда не было")


def dns_token_rotate(reserve: str) -> str:
    """Т-РУ17: токен был у резерва во время аварийного переезда."""
    return (f"🔑 Токен API DNS был у {_n(reserve)} во время переезда — смени его у провайдера и пришли новый: "
            "«🛟 Резерв» → «🌐 Запись DNS» → «✏️ Токен»")


# ── клиентам ─────────────────────────────────────────────────────────────────

def client_moved(rf: bool = False) -> str:
    """Т-РК1: сервер временно переехал."""
    if rf:
        return ("🔁 Сервер VPN стал недоступен из РФ и временно переехал. Если VPN не работает — выключи и включи "
                "его в приложении, настройки менять не нужно")
    return ("🔁 Сервер VPN временно переехал. Если VPN не работает — выключи и включи его в приложении, настройки "
            "менять не нужно")


def client_returned(until: str) -> str:
    """Т-РК2: сервер вернулся; until — дата, до которой работает пересылка."""
    try:
        when = timeutil.fmt_date_ui(timeutil.parse_iso(until))
    except (ValueError, TypeError, AttributeError):
        when = ""
    tail = f": до {_e(when)} он работает и так, но через запасной путь" if when else " — пока он работает через запасной путь"
    return f"🔁 Сервер VPN вернулся на основной. Выключи и включи VPN в приложении, когда будет удобно{tail}"
