"""reserve.py — часть пакета notices (см. __init__): уведомления пары серверов
из доменного слоя — связь с резервом (Т-РУ8), совместимость направлений
(Т-РУ9), смена админа второго сервера, удаление пары второй стороной; переезд
и возврат пользователей — админу резерва (у меня пользователи основы), админу основы
(пользователи на резерве; из бота основы на резерве), итоги возврата, запись DNS,
пользователям («переподключи VPN»). Журнал данных (хвост старой эпохи, Т-Ж4…Т-Ж6,
строки Т-РУ5б о недошедших правках) — reserve_tail. Имена серверов — «имя», адреса
и имена записей моноширинным, без точки в конце; «один раз на расхождение»."""
from __future__ import annotations

import html

from awgbot.util import timeutil
from awgbot.util.textfmt.base import plural_ru


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
    return f"🔴 Нет связи с резервом {_n(peer)}" + (f" с {at}" if at else "") + " — пока её нет, пользователи к нему не переедут"


def peer_up(peer: str) -> str:
    return f"🟢 Связь с резервом {_n(peer)} восстановлена"


def zone_tools_failed(reason: str) -> str:
    """Пакеты гостевой зоны (dnsmasq, conntrack) не поставились сами."""
    return (f"⚠️ Для гостевой зоны резерва не поставились пакеты: {_e(reason)} — поставь руками "
            "<code>apt-get install dnsmasq conntrack</code>, иначе пользователей второго сервера не принять")


def pair_unconfirmed(peer: str) -> str:
    """Сопряжение разошлось: пара записана, а сессии на новых ключах не было."""
    return (f"⚠️ Сопряжение с {_n(peer)} не подтвердилось: линк на новых ключах не заработал за 10 минут — "
            "пара снята с этой стороны. Пригласи заново")


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
    """Т-РУ4: пользователи и бот основы у меня; kind — button | dead | rf | nogw | code."""
    if kind == "button":
        return f"📥 {_n(primary)} перевёл к тебе пользователей и бот"
    at = _when(since)
    if kind == "rf":
        return f"📥 Пользователи и бот {_n(primary)} у тебя: {_n(primary)} недоступен из РФ с {at} — его шлюзы его не видят, этот сервер видит"
    if kind == "nogw":
        return f"📥 Пользователи и бот {_n(primary)} у тебя: его шлюзы не на связи с этим сервером с {at} — РФ-доступ его пользователей идёт через твои шлюзы"
    if kind == "code":
        return f"📥 Пользователи и бот {_n(primary)} у тебя по аварийному коду его админа: {_n(primary)} и его шлюзы не отвечают с {at}"
    text = f"📥 Пользователи и бот {_n(primary)} у тебя: {_n(primary)} не отвечает с {at} ни этому серверу, ни своим шлюзам"
    return text + (f"; {_e(extra)}" if extra else "")


def hosting_ended(primary: str, traffic: str) -> str:
    """Т-РУ5: пользователи вернулись к основе."""
    tail = f" — через тебя прошло {_e(traffic)}" if traffic else ""
    return f"↩️ Пользователи и бот {_n(primary)} вернулись к нему{tail}, данные {_n(primary)} сняты"


def hosting_failed(primary: str, reason: str) -> str:
    return f"⚠️ Пользователей {_n(primary)} принять не удалось: {_e(reason)}"


# ── админу основы (из бота основы на резерве) ───────────────────────────────

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
    if kind == "nogw":
        return (f"🛟 Шлюзы не на связи с резервом {_n(reserve)} с {dead} — пользователи и бот работают на {_n(reserve)} с {at}, "
                f"РФ-доступ идёт через шлюзы {_n(reserve)}; {mailed}. Вернутся шлюзы — пользователи вернутся сюда")
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


def primary_back(auto: bool) -> str:
    """Т-РУ6 (из бота основы на резерве): сервер основы снова в строю."""
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


# ── шлюзы и голосование (этап 5) ────────────────────────────────────────────

def _names(names) -> str:
    return ", ".join(_n(x) for x in names or [])


def gateway_reserve_down(gw: str, reserve: str, since: str = "") -> str:
    """Т-РУ18: шлюз не на связи с резервом."""
    at = _when(since)
    return (f"⚠️ Шлюз {_n(gw)} не на связи с резервом {_n(reserve)}" + (f" с {at}" if at else "")
            + " — пока так, при падении сервера он не голосует")


def gateways_reserve_all_down(reserve: str, since: str = "") -> str:
    at = _when(since)
    return (f"⚠️ Ни один шлюз не на связи с резервом {_n(reserve)}" + (f" с {at}" if at else "")
            + " — если сервер упадёт, пользователи переедут только по твоему аварийному коду")


def gateway_reserve_up(gw: str, reserve: str) -> str:
    return f"🟢 Шлюз {_n(gw)} снова на связи с резервом {_n(reserve)}"


def code_needed_reserve(primary: str, since: str, admin_opened: bool) -> str:
    """Т-РУ7 (Н4, админу резерва): сам принять пользователей не могу."""
    tail = (" — сам принять его пользователей он не может: нужен аварийный код админа " + _n(primary)
            + (", бот написал ему" if admin_opened else "; его админ этот бот ещё не открывал — написать ему некуда"))
    return (f"⚠️ {_n(primary)} не отвечает с {_when(since)}, его шлюзы тоже не на связи с этим сервером{tail}. "
            f"Вернётся шлюз, который не видит {_n(primary)}, — пользователи переедут сами")


def code_needed_primary(primary: str, reserve: str, since: str) -> str:
    """Т-РУ7б (Н4, админу основы в чат его роли в боте резерва)."""
    return (f"🆘 Сервер {_n(primary)} и его шлюзы не отвечают резерву {_n(reserve)} с {_when(since)}. Если сервер упал — "
            f"пришли аварийный код резерва; вернётся шлюз, который не видит сервер, — пользователи переедут сами")


def code_window_closed(primary: str, why: str) -> str:
    """Т-РН3 (админу основы): приём кода закрыт — переезд не нужен или идёт сам."""
    if why == "gw_sees":                                      # primary здесь — «шлюз N» (имён шлюзов основы у резерва нет)
        return f"ℹ️ {_e(primary[:1].upper() + primary[1:])} снова на связи и видит сервер — переезд не нужен"
    if why == "gw_unseen":
        return f"ℹ️ {_e(primary[:1].upper() + primary[1:])} снова на связи и не видит сервер — переезд идёт сам, код не нужен"
    return f"ℹ️ {_n(primary)} снова на связи — переезд не нужен"


def code_wrong(left: int) -> str:
    """Т-РН3: код не подошёл."""
    return f"⚠️ Код не подошёл. Осталось попыток: {int(left)}" + (" — следующие через час" if left else "")


def code_open_failed(primary: str, reason: str) -> str:
    return f"⚠️ Код подошёл, но пользователей {_n(primary)} поднять не удалось: {_e(reason)}. Приём кода снова открыт"


def rf_block_warning(reserve: str, gateways: list, since: str, until: str) -> str:
    """Т-РУ13 (основе, случай 3): похоже, сервер недоступен из РФ."""
    at, to = _when(since), _when(until)
    if len(gateways) == 1:
        return (f"🚫 Шлюз {_n(gateways[0])} не видит этот сервер с {at}, а {_n(reserve)} видит — похоже, сервер недоступен "
                f"из РФ или у шлюза плохой путь до него. Если так останется до {to}, пользователи и бот переедут на {_n(reserve)}")
    return (f"🚫 Шлюзы {_names(gateways)} не видят этот сервер с {at}, а {_n(reserve)} видит — похоже, сервер недоступен "
            f"из РФ. Если так останется до {to}, пользователи и бот переедут на {_n(reserve)}")


def nogw_warning(reserve: str, since: str, until: str) -> str:
    """Т-РУ23 (основе, Н3): шлюзы не на связи с резервом."""
    return (f"⚠️ Шлюзы не на связи с резервом {_n(reserve)} с {_when(since)} — если так останется до {_when(until)}, "
            f"пользователи и бот переедут на {_n(reserve)} ради РФ-доступа через его шлюзы")


def case4_notice(reserve: str, unseen: list, seen: list, since: str) -> str:
    """Т-РУ21 (основе, случай 4): дело в сети шлюза, переезда не будет."""
    who = ("Шлюз " + _names(unseen)) if len(unseen) == 1 else ("Шлюзы " + _names(unseen))
    verb = "не видит" if len(unseen) == 1 else "не видят"
    sees = _n(reserve) + (" и " + _names(seen) if seen else "")
    return f"⚠️ {who} {verb} этот сервер с {_when(since)}, а {sees} видят — похоже, дело в сети шлюза; переезда не будет"


def ask_refused(reserve: str, case: str, since: str, link_if: str = "", reserve_ip: str = "") -> str:
    """Т-РУ22 (основе): отказ на просьбу резерва — причина у этого сервера."""
    at = _when(since)
    if case == "nogw":
        tail = f". Проверь путь шлюзов до <code>{_e(reserve_ip)}</code>" if reserve_ip else ""
        return (f"ℹ️ Шлюзы не на связи с резервом {_n(reserve)} с {at}, но этот сервер их видит и РФ-доступ работает — "
                f"переезда на {_n(reserve)} не будет{tail}")
    return (f"⚠️ Шлюзы не видят этот сервер с {at}, а {_n(reserve)} видит: на сервере не поднят линк "
            f"<code>{_e(link_if or 'awglink')}</code> — дело не в РФ, переезда на {_n(reserve)} не будет")


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
    """Т-Ж10: строка Т-РУ5б — возврат прошёл без реплики (переехавшего инстанса на резерве
    уже не было или реплика пуста)."""
    return f"ℹ️ Переехавшего инстанса на {_n(reserve)} уже не было — изменения, сделанные там, будут утеряны"


def forced_no_merge(reserve: str) -> str:
    """С9: резерв вышел на связь без переехавшего инстанса — изменений с него не будет."""
    return f"ℹ️ {_n(reserve)} снова на связи, но переехавшего инстанса на нём уже нет — изменения, сделанные там, будут утеряны"


def webhook_stuck_removed(reserve: str) -> str:
    """Вебхук резерва снят самим сервером: запись уже на нём, пользователей резерв не нёс."""
    return (f"ℹ️ Право отвечать в Telegram оставалось у {_n(reserve)}, хотя запись уже указывала сюда и пользователей "
            f"он не нёс — вебхук снят, бот снова отвечает отсюда")


# ── кворум (4.2.0) ──────────────────────────────────────────────────────────

def quorum_enabled(reserve: str, gateways: list) -> str:
    """Т-К12 (админу основы, со звуком): голосование включено, аварийный код сменился."""
    who = _n(reserve) + (" и шлюзы " + _names(gateways) if gateways else "")
    return (f"🗳 Голосование включено: о переезде пользователей решает большинство — {who}. Аварийный код сменился — "
            "новый в «⚙️ Настройки» → «🛟 Резерв» → «🆘 Аварийный код», прежний больше не действует")


def quorum_enabled_peer(primary: str) -> str:
    """Т-К12б (админу второго сервера, без звука)."""
    return f"🗳 Голосование о {_n(primary)} включено: этот сервер и шлюзы {_n(primary)} решают, когда его пользователям переезжать сюда"


# ── пул адресов Telegram и вебхук (этап 7) ──────────────────────────────────

def tg_pool_stale(since: str) -> str:
    """Т-РУ25: анонсы сетей Telegram не обновлялись."""
    at = _date(since)
    return ("⚠️ Анонсы сетей Telegram не обновлялись" + (f" с {at}" if at else "")
            + ": источник не отвечает — пул вебхука прежний")


def tg_pool_core_lost(prefix: str) -> str:
    return (f"⚠️ Сеть <code>{_e(prefix)}</code> со страницы Bot API больше не анонсирует Telegram — пул её держит, "
            "но страница могла устареть")


def tg_pool_ok() -> str:
    return "🟢 Анонсы сетей Telegram снова обновляются"


def _date(iso: str) -> str:
    try:
        return timeutil.parse_iso(iso).strftime("%d.%m")
    except (ValueError, TypeError):
        return ""


def webhook_stalled_reserve(primary: str, since: str, error: str, port: int) -> str:
    """Т-РУ19 админу резерва: Telegram не доставляет обновления бота основы сюда."""
    return (f"⚠️ Telegram не доставляет обновления бота {_n(primary)} на этот сервер с {_when(since)}: "
            f"<code>{_e(error)}</code> — проверь, открыт ли у хостинга порт <code>{int(port)}</code>")


def webhook_restored_reserve(primary: str) -> str:
    return f"🟢 Доставка обновлений бота {_n(primary)} восстановлена"


# ── агент шлюза ─────────────────────────────────────────────────────────────

def gateway_moved(reserve: str, case: str, since: str, record: str = "", ip: str = "", others: dict | None = None) -> str:
    """Т-РА3: пользователи переехали на резерв; case — dead | rf | partial | nogw."""
    at = _when(since)
    rec = f": запись <code>{_e(record)}</code> → <code>{_e(ip)}</code>" if record and ip else ""
    if case == "rf":
        return (f"🚫 Сервер AWG недоступен отсюда с {at}, а {_n(reserve)} его видит — похоже, сервер закрыт из РФ; "
                f"пользователи переехали на {_n(reserve)}{rec}")
    if case == "partial":
        unseen = _names((others or {}).get("unseen") or [])
        who = f" и шлюзу {unseen}" if unseen else ""
        return (f"🟡 Пользователи переехали на резерв {_n(reserve)}: сервер AWG не отвечает ему{who}, этому шлюзу — отвечает. "
                f"РФ-доступ пользователей с {_n(reserve)} идёт и через этот шлюз")
    if case == "nogw":
        return (f"🟡 Пользователи переехали на резерв {_n(reserve)}: этот шлюз не на связи с ним с {at} — "
                f"РФ-доступ пользователей идёт через шлюзы {_n(reserve)}")
    return f"🔴 Сервер AWG не отвечает с {at} — пользователи переехали на резерв {_n(reserve)}{rec}"


def gateway_rf_warning(reserve: str, since: str, until: str) -> str:
    """Т-РА4 (случай 3, до переезда)."""
    return (f"⚠️ Сервер AWG не отвечает с {_when(since)}, а {_n(reserve)} его видит — похоже, сервер недоступен из РФ; "
            f"если так останется до {_when(until)}, пользователи переедут на {_n(reserve)}")


def gateway_case4(reserve: str, since: str, seen: list) -> str:
    """Т-РА4 (случай 4): дело в сети этого шлюза."""
    who = _n(reserve) + (" и " + _names(seen) if seen else "")
    return (f"⚠️ Сервер AWG не отвечает этому шлюзу с {_when(since)}, а {who} его видят — похоже, дело в сети этого шлюза; "
            f"переезда не будет")


def gateway_move_failed(reserve: str, since: str, reason: str) -> str:
    """Т-РА5."""
    return f"⚠️ Сервер AWG не отвечает с {_when(since)}, переезд на {_n(reserve)} не состоялся: {_e(reason)}"
