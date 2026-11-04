"""reserve_move.py — часть пакета notices (см. __init__): переезд и возврат
пользователей — админу резерва (у меня пользователи основы), админу основы
(пользователи на резерве; из гостя), итоги возврата и строки
Т-РУ5б, запись DNS, пользователям («переподключи VPN»), возврат без резерва
(С9) и снятый вебхук резерва. Правила — как у reserve: имена серверов «имя»,
адреса и имена записей моноширинным, без точки в конце."""
from __future__ import annotations

from awgbot.util import timeutil
from awgbot.util.textfmt.base import plural_ru

from .reserve_fmt import _e, _n, _when


# ── админу резерва ───────────────────────────────────────────────────────────

def hosting_started(primary: str, kind: str, since: str, extra: str = "") -> str:
    """Т-РУ4: пользователи и бот основы у меня; kind — button | dead | rf | nogw | code."""
    if kind == "button":
        return f"📥 {_n(primary)} перевёл к тебе пользователей и бот"
    at = _when(since)
    if kind == "rf":
        return f"📥 Пользователи и бот {_n(primary)} у тебя: {_n(primary)} недоступен из РФ с {at} — его шлюзы его не видят, этот сервер видит"
    if kind == "code":
        return f"📥 Пользователи и бот {_n(primary)} у тебя по аварийному коду его администратора: {_n(primary)} и его шлюзы не отвечают с {at}"
    text = f"📥 Пользователи и бот {_n(primary)} у тебя: {_n(primary)} не отвечает с {at} ни этому серверу, ни своим шлюзам"
    return text + (f"; {_e(extra)}" if extra else "")


def hosting_ended(primary: str, traffic: str) -> str:
    """Т-РУ5: пользователи вернулись к основе."""
    tail = f" — через тебя прошло {_e(traffic)}" if traffic else ""
    return f"↩️ Пользователи и бот {_n(primary)} вернулись к нему{tail}, данные {_n(primary)} сняты"


def hosting_failed(primary: str, reason: str) -> str:
    return f"⚠️ Пользователей {_n(primary)} принять не удалось: {_e(reason)}"


def hosting_without_bot(primary: str, reason: str) -> str:
    """Запись DNS уже на мне, клон несёт пользователей, а бот основы здесь Telegram не взял:
    зона остаётся (VPN работает), захват повторяется."""
    return (f"⚠️ Пользователи {_n(primary)} у тебя, VPN работает, но гостевой экземпляр не отвечает в Telegram: "
            f"{_e(reason)} — повторяю раз в минуту")


def return_resumed(primary: str, bot: bool = True) -> str:
    """Возврат сорвался у основы (слияние не прошло): бот основы снова работает здесь; bot=False —
    до возврата он здесь Telegram не держал (VPN без бота), и после — так же, захват повторяется."""
    head = f"⚠️ Возврат пользователей на {_n(primary)} отклонён: ошибка слияния данных на основном сервере. "
    if not bot:
        return (head + "Пользователи остаются на этом сервере (VPN работает, опрос гостевого экземпляра повторяется каждую "
                f"минуту). Повторный возврат запускается администратором {_n(primary)}")
    return head + f"Пользователи и бот {_n(primary)} остаются на этом сервере. Повторный возврат запускается администратором {_n(primary)}"


# ── админу основы (из гостя) ───────────────────────────────

def moved_to_reserve(reserve: str, kind: str, since: str, dead_since: str = "", reserve_ip: str = "") -> str:
    """Т-РУ4а: пользователи и бот работают на резерве; kind как у hosting_started."""
    at, dead = _when(since), _when(dead_since)
    mailed = "пользователям на связи ушло «переподключи VPN»"
    if kind == "button":
        return f"✅ Пользователи и бот — на {_n(reserve)} с {at}; {mailed}"
    if kind == "rf":
        tail = f". Следующим из РФ могут закрыть и адрес {_n(reserve)} <code>{_e(reserve_ip)}</code>" if reserve_ip else ""
        return (f"🚫 Сервер AWG недоступен из РФ с {dead}: шлюзы его не видят, резерв {_n(reserve)} видит — пользователи и бот "
                f"работают на {_n(reserve)} с {at}; {mailed}{tail}")
    if kind == "code":
        return (f"🔴 Сервер AWG и шлюзы не отвечали резерву {_n(reserve)} с {dead} — по твоему аварийному коду пользователи и бот "
                f"работают на {_n(reserve)} с {at}; {mailed}")
    return (f"🔴 Сервер AWG не отвечает с {dead} ни резерву, ни шлюзам — пользователи и бот работают на резерве {_n(reserve)} "
            f"с {at}; {mailed}")


def returned_home(reserve: str, report: dict, traffic: str) -> str:
    """Т-РУ5б: пользователи и бот снова на основе; итог слияния."""
    parts = []
    if report.get("clients"):
        parts.append(f"{int(report['clients'])} {plural_ru(int(report['clients']), 'новый профиль', 'новых профиля', 'новых профилей')}")
    if report.get("devices"):
        parts.append(f"{int(report['devices'])} {plural_ru(int(report['devices']), 'новое устройство', 'новых устройства', 'новых устройств')}")
    if report.get("extensions"):
        parts.append(f"{int(report['extensions'])} {plural_ru(int(report['extensions']), 'продление', 'продления', 'продлений')}")
    if report.get("blocks"):
        parts.append(f"{int(report['blocks'])} {plural_ru(int(report['blocks']), 'блокировка', 'блокировки', 'блокировок')}")
    body = ", ".join(parts) if parts else "без новых профилей и устройств"
    text = f"↩️ Пользователи и бот снова здесь. За время на {_n(reserve)}: {body}"
    if traffic:
        text += f" · через резерв {_e(traffic)}"
    lost = report.get("lost")
    # строки собраны готовыми к разметке (имена экранированы при сборке: lost_device,
    # returned_without_replica, сверка правок резерва); отчёт состояния до 4.1.0 мог нести True
    for line in lost if isinstance(lost, list) else []:
        text += "\n\n" + str(line)
    return text


def lost_device(name: str, owner: str, taken_by: str, taken_owner: str, reserve: str) -> str:
    """Строка конфликта для Т-РУ5б."""
    return (f"⚠️ Не встало устройство «{_e(name)}» профиля {_e(owner)} — его адрес занял «{_e(taken_by)}» профиля "
            f"{_e(taken_owner)}, заведённый на {_n(reserve)}. Выдай «{_e(name)}» конфигурацию заново")


def lost_orphan(name: str, owner: str, reserve: str) -> str:
    """Строка Т-РУ5б: устройство, заведённое на резерве для профиля, который основа удалила после снимка."""
    return (f"⚠️ Устройство «{_e(name)}» пользователя {_e(owner)}, созданное на {_n(reserve)}, пропущено: "
            "этот профиль был удалён на основном сервере во время аварии")


def primary_back(auto: bool) -> str:
    """Т-РУ6 (из гостя): сервер основы снова в строю."""
    if auto:
        return "🟢 Сервер AWG снова в строю — возвращаю пользователей и бот"
    return "🟢 Сервер AWG снова в строю — верни пользователей кнопкой"


def move_failed(reserve: str, reason: str, released: bool = False) -> str:
    text = f"⚠️ Перевод не состоялся: {_e(reason)}"
    return text + (f"; {_n(reserve)} отпустил профили" if released else "")


def return_failed(reserve: str, reason: str, final: bool = False) -> str:
    tail = ("сам возврат больше не повторяю — разберись по журналу и верни кнопкой «↩️ Вернуть пользователей»" if final
            else "проверь журнал и повтори возврат")
    return (f"⚠️ Возврат не состоялся: слияние данных с {_n(reserve)} не удалось — {_e(reason)}. Пользователи и бот "
            f"остаются на {_n(reserve)}; {tail}")


def record_not_changed(name: str, reason: str, ip: str) -> str:
    """Т-РУ11."""
    return (f"⚠️ Запись <code>{_e(name)}</code> не изменена: {_e(reason)} — смени её у провайдера на "
            f"<code>{_e(ip)}</code>")


def record_own_unknown(name: str, host: str = "") -> str:
    """Т-РУ11: свой адрес не определился — запись на себя бот не сменит."""
    why = f" (домен <code>{_e(host)}</code> не резолвится)" if host else ""
    return (f"⚠️ Запись <code>{_e(name)}</code> не изменена: не удалось определить локальный адрес{why} — поправь "
            "запись вручную у DNS-провайдера")


def record_manual_needed(name: str, ip: str, back: bool = False) -> str:
    """Запись меняет человек: просьба сменить её — при возврате и при переезде без живого сообщения."""
    what = "пользователи возвращаются" if back else "пользователи переезжают"
    return f"✏️ {what.capitalize()}: смени у провайдера A-запись <code>{_e(name)}</code> на <code>{_e(ip)}</code> — бот увидит смену сам"


def record_missing(name: str) -> str:
    """Записи нет у авторитетного сервера — пользователи не резолвят имя."""
    return f"🚨 Записи <code>{_e(name)}</code> у авторитетного сервера нет — пользователи не находят сервер; заведи A-запись у провайдера"


def record_multi(name: str, addrs: list) -> str:
    """У записи несколько адресов — часть пользователей уходит мимо."""
    return (f"⚠️ У записи <code>{_e(name)}</code> несколько адресов: {', '.join(f'<code>{_e(a)}</code>' for a in addrs)} — "
            "часть пользователей уходит не сюда; оставь одну A-запись")


def record_foreign(name: str, ip: str) -> str:
    """Т-РУ16: запись указывает не сюда, переезда не было."""
    return (f"🚨 Запись <code>{_e(name)}</code> указывает на <code>{_e(ip)}</code>, а не на этот сервер, — пользователи "
            "уходят туда. Переезда не было")


def dns_token_rotate(reserve: str) -> str:
    """Т-РУ17: токен был у резерва во время аварийного переезда."""
    return (f"🔑 Токен API DNS был у {_n(reserve)} во время переезда — смени его у провайдера и пришли новый: "
            "«🛟 Резерв» → «🌐 DNS-запись» → «✏️ Токен»")


# ── пользователям ────────────────────────────────────────────────────────────

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


# ── возврат без резерва (С9), снятый вебхук резерва ─────────────────────────

def forced_home(reserve: str, journal: bool = False) -> str:
    """С9: пользователи возвращены без резерва; с журналом — изменения бота на резерве после
    разрыва придут хвостом, конфликтующие не применятся (как в диалоге Т-Ж7)."""
    tail = ("изменения, сделанные там после разрыва соединения, перенесутся, когда он выйдет на связь; "
            f"конфликтующие правки с {_n(reserve)} применены не будут"
            if journal else "изменения с него применятся, когда он выйдет на связь")
    return (f"↩️ Пользователи и бот снова здесь: запись на этом сервере, управление Telegram принято.\n"
            f"{_n(reserve)} не отвечает — {tail}")


def take_tg_stuck(reserve: str) -> str:
    """Возврат: deleteWebhook с этого сервера не проходит минутами."""
    return (f"⚠️ Возврат пользователей с {_n(reserve)}: Telegram с этого сервера не отвечает (deleteWebhook) уже 5 минут — "
            "бот не отвечает никому, пока сеть не вернётся")


def returned_without_replica(reserve: str) -> str:
    """Т-Ж10: строка Т-РУ5б — возврат прошёл без реплики (гостя на резерве
    уже не было или реплика пуста)."""
    return f"ℹ️ Гостевой экземпляр на {_n(reserve)} уже остановлен — несинхронизированные изменения могут быть утеряны"


def forced_no_merge(reserve: str) -> str:
    """С9: резерв вышел на связь без гостя — изменений с него не будет."""
    return (f"ℹ️ Связь с {_n(reserve)} восстановлена, но гостевой экземпляр там уже остановлен. Правки, сделанные "
            "во время работы на резерве, не сохранились")


def webhook_stuck_removed(reserve: str) -> str:
    """Вебхук резерва снят самим сервером: запись уже на нём, пользователей резерв не нёс."""
    return ("ℹ️ Резервный сервер больше не обслуживал пользователей, но продолжал удерживать бота. Управление ботом "
            "возвращено на этот сервер, обработка входящих сообщений восстановлена")
