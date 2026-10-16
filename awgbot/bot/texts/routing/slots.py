"""slots.py — шлюзовое устройство и слоты: карточка устройства, имя и состояние слота, строки списка, экран «🛰 Шлюзы»."""

from __future__ import annotations

from awgbot.bot import ui
from awgbot.util import timeutil
from awgbot.domain.gwchecks import WRITE_ERROR

from ..fmt import _e, human_bytes, _updown, plain_ip, details, num


# ── адреса в туннеле: пары «настоящая → в туннеле» ──────────────────────────

def lan_pairs(nets, twins, limit: int = 6) -> str:
    """«<code>192.168.1.0/24</code> · в туннеле <code>10.240.1.0/24</code>; …»
    по порядку подсетей слота; подсеть без адреса в туннеле (не в таблице) —
    с хвостом « · вне туннеля», иначе читалась бы вторым адресом в туннеле
    соседней пары; больше limit — «и ещё N». twins — [(реальная, в туннеле)]."""
    tw = dict(twins or [])
    items = [f"<code>{_e(n)}</code>" + (f" · в туннеле <code>{_e(tw[n])}</code>" if n in tw else " · вне туннеля")
             for n in list(nets or [])[:limit]]
    more = len(nets or []) - limit
    return "; ".join(items) + (f"; и ещё {more}" if more > 0 else "")


def lan_example(twins) -> str:
    """Пример перевода для подсказки: «192.168.1.10 → 10.240.1.10» по первой
    паре; хост 10 сети (в узкой — первый); пар нет — пусто."""
    import ipaddress
    if not twins:
        return ""
    real, twin = (ipaddress.IPv4Network(x) for x in twins[0])
    off = 10 if real.num_addresses > 12 else 1
    return f"{real.network_address + off} → {twin.network_address + off}"


def lan_space_range() -> str:
    """Пространство адресов в туннеле словами для подсказки: «10.240–10.255»."""
    from awgbot.util import lanspace
    sp = lanspace.space()
    a, b = str(sp.network_address).split(".")[:2]
    last = str(sp.broadcast_address).split(".")[1]
    return f"{a}.{b}–{a}.{last}"


# ── шлюз условной маршрутизации ──────────────────────────────────────────────

def vps_hostname() -> str:
    """Имя сервера для подписи «Пинг с сервера …»; без имени — пусто."""
    import socket
    try:
        return socket.gethostname().strip()
    except OSError:
        return ""


def ping_line(ms) -> str:
    """«Пинг с сервера AWG-SRV: 43 мс» — с сервера до шлюза по линку; «сервера»
    — чтобы имя машины не приняли за шлюз."""
    host = vps_hostname()
    return (f"Пинг с сервера{' ' + _e(host) if host else ''}: "
            + (ping_fmt(ms) if ms is not None else "шлюз не отвечает"))


def ext_ip_line(ip) -> str:
    return "↗️ Внешний IP: " + (plain_ip(ip) if ip else "не определён")


def ping_fmt(ms) -> str:
    """«43 мс» / «1,2 с» — русские сокращённые единицы."""
    if ms is None:
        return "не измерен"
    ms = int(ms)
    if ms < 1000:
        return f"{ms} мс"
    return f"{ms / 1000:.1f}".replace(".", ",") + " с"


def _slot_dot(state) -> str:
    """Кружок состояния слота — тот же, что в строке статуса."""
    if state.get("link_ok"):
        return "🟢"
    return "🔴" if state.get("unavailable") else "🟡"


def gateway_role_line(state) -> str:
    """Строка роли слота для карточки устройства: кружок состояния, тег роли,
    что это значит для трафика."""
    if not state or state.get("gateway") is None:
        return ""
    dot = _slot_dot(state)
    if state.get("active"):
        return f"{dot} <b>[Активен]</b> — несёт трафик РФ-доступа"
    via = state.get("active_display") or ""
    return f"{dot} <b>[Резерв]</b> — трафик идёт через {via}" if via else f"{dot} <b>[Резерв]</b>"


def gateway_device_card(dev, state=None) -> str:
    """Карточка устройства-шлюза без слота (у слота — карточка слота): один
    значок состояния в заголовке, трафик, роль; жирная фраза о трафике —
    только когда слот активен (без слота через устройство ничего не идёт)."""
    if not dev.last_handshake:
        status = "🔴 ещё не подключалось"
    elif timeutil.handshake_is_online(dev.last_handshake):
        status = "🟢 онлайн"
    else:
        status = f"🔴 офлайн · последнее подключение: {timeutil.fmt_handshake(dev.last_handshake)}"
    rx, tx = int(dev.traffic_rx_month), int(dev.traffic_tx_month)
    head = (ui.head(f"🛰 {_e(dev.name)}", status=f"({plain_ip(dev.address)}) {status}") + "\n"
            # стрелки — со стороны шлюза: его исходящее — это tx сервера
            f"Трафик: {human_bytes(rx + tx)} {_updown(tx, rx)}")
    if state is not None:
        head += "\n\n" + ext_ip_line(state.get("ext_ip")) + "\n" + ping_line(state.get("ping_ms"))
    if state is not None and state.get("active"):
        tail = ("\n\n<b>Это устройство — шлюз РФ-доступа, через него идёт "
                "трафик на российские сервисы.</b>")
    else:
        tail = "\n\nЭто устройство — шлюз РФ-доступа"
    role = gateway_role_line(state)
    agent = agent_bot_line(state)
    return head + tail + (f"\n{role}" if role else "") + (f"\n\n{agent}" if agent else "")


def gateway_claim_marked(dev) -> str:
    """Устройство назначено шлюзом по пересланному сообщению: конфигурацию
    везёт канал — файла не будет."""
    return (f"🛰 Устройство «{_e(dev.name)}» ({plain_ip(dev.address)}) назначено шлюзом. "
            "Настройки применятся по каналу конфигурации, когда шлюз подключится; не подключится — "
            "см. причину в строке «🔧 Канал конфигурации» карточки шлюза")


def gateway_claim_already(dev) -> str:
    return f"🛰 «{_e(dev.name)}» уже шлюз, ничего не менял."


# ── слоты шлюзов ───────────────────────────────


def _slot_down_for(state) -> str:
    """Сколько слот не отвечает: от момента падения из state («13 дн 4 ч»);
    момента нет (старый state) — по тактам окна, минутами."""
    import time as _time
    from awgbot.core import settings as _settings
    since = int(state.get("down_since") or 0)
    if since:
        return timeutil.age_short(max(90, int(_time.time()) - since))      # не меньше «1 мин»
    mins = max(1, int(state.get("down_ticks", 0)) * _settings.get_int("app.routing.probe_seconds", 30) // 60)
    return f"{mins} мин"


def _bot_link(agent_bot: dict | None) -> str:
    """Ссылка в чат бота шлюза из {'username', 'name'} (кэш getMe или снимок
    канала); бот неизвестен — пустая строка."""
    me = agent_bot or {}
    if not me.get("username"):
        return ""
    return f'<a href="https://t.me/{_e(me["username"])}">{_e(me.get("name") or me["username"])}</a>'


def _slot_note(state: str, agent_bot: dict | None, error: str, purpose: str, what: str,
               *, offline: bool = False) -> str:
    """Строка состояния под записями SMB / своими списками в карточке слота —
    общие ветки: обновить шлюз, отказ шлюза, ожидание. Имя слота не
    повторяется: строка стоит в его же карточке; ссылка на бота — строкой
    «Бот шлюза» внизу карточки. offline — канал конфигурации не на связи:
    ожидание — «когда он выйдет на связь», а не синхронизация сейчас.
    purpose — «Для доступа к SMB» / «Для синхронизации», what — «записи SMB» / «списки»."""
    if state == "old_agent":
        return f"⚠️ {purpose} обнови агента шлюза"
    if state == "failed":
        if error.startswith(WRITE_ERROR):           # поломка на шлюзе, не отказ
            detail = error[len(WRITE_ERROR):].lstrip(": ")
            return "⚠️ Ошибка записи файла на шлюзе" + (f": {_e(detail)}" if detail else "")
        return (f"⚠️ Шлюз не смог принять {what}" + (f": {_e(error)}" if error else "")
                + " — подробности в «🩺 Здоровье» бота шлюза")
    if offline:
        return f"🟡 {what[0].upper() + what[1:]} уйдут на шлюз, когда он выйдет на связь"
    return "🟡 Синхронизация с другими шлюзами…"


ROUTER_IP_PLACEHOLDER = "АДРЕС_ШЛЮЗА"
GATEWAY_GONE = "Такого шлюза нет"            # всплывашка на кнопку снятого слота


# ── Условная маршрутизация ─────────────────────

ROUTING_NAME = "РФ-доступ"


# ── слоты: имя и состояние ───────────────────────────────────────────────────

def slot_name(state, star: bool = False) -> str:
    """«NASPi (дача)» — имя устройства и подпись; star — «⭐» у
    предпочтительного, «🛰» у остальных не рисуется здесь (заголовок карточки
    ставит его сам)."""
    dev, gw = state.get("device"), state.get("gateway")
    name = _e(dev.name) if dev is not None else f"шлюз {gw.id}"
    pre = "⭐ " if star and state.get("preferred") else ""
    return pre + name + (f" ({_e(gw.label)})" if gw is not None and gw.label else "")


def slot_status(state) -> str:
    """«🟢 Активен» / «🟢 Резерв» / «🟡 Резерв, проверка связи» /
    «🔴 Резерв, не отвечает 14 мин»."""
    role = "Активен" if state.get("active") else "Резерв"
    if state.get("link_ok"):
        return f"🟢 {role}"
    if state.get("unavailable"):
        age = state.get("handshake_age")
        what = "нет доступа в интернет" if age is not None and age <= 300 else "не отвечает"
        return f"🔴 {role}, {what} {_slot_down_for(state)}"
    return f"🟡 {role}, проверка связи"


def slot_ref(state) -> str:
    """Имя шлюза для фраз («упадёт NASPi», «останется в резерве»): только имя
    устройства, без подписи и адреса."""
    dev, gw = state.get("device"), state.get("gateway")
    return _e(dev.name) if dev is not None else f"шлюз {gw.id}"


def _slot_ping_tail(state) -> str:
    ms = state.get("ping_ms")
    if ms is None:
        cached = state.get("ping")
        ms = cached[0] if cached else None
    return f" · {ping_fmt(ms)}" if ms is not None else ""


def slot_line(state, star: bool = True) -> str:
    """Строка слота на экране «Шлюзы»: «⭐ NASPi — 🟢 Активен · 43 мс»; при
    одном слоте звезды нет — выбирать не из чего."""
    return f"{slot_name(state, star=star)} — {slot_status(state)}{_slot_ping_tail(state)}"


PEER_NETS_SHOWN = 6           # адресов в туннеле на слот в строке «Связь подсетей» — как в карточке
PEER_NETS_SHOWN_MANY = 3      # при PEER_MANY и больше слотах — меньше: строка не раздувает экран
PEER_MANY = 9


def peer_nets_line(info: dict) -> str:
    """Строка «🔗 Связь подсетей» на экране «Шлюзы» — инфобокс без кнопки:
    связь подсетей — часть VPN-транзита. Пары — в адресах в туннеле, до шести
    на слот, при девяти слотах и больше — до трёх («и ещё N»); слотов с VPN-транзитом меньше двух — когда появится;
    адреса в туннеле выключены — отсылка к карточке."""
    st = info.get("state")
    if st == "off":
        return "🔗 Связь подсетей: выключена вместе с адресами в туннеле — см. карточку шлюза"
    if st != "ok":
        return "🔗 Связь подсетей появится, когда VPN-транзит будет включён хотя бы у двух шлюзов"

    named = info.get("pairs_named") or []
    limit = PEER_NETS_SHOWN_MANY if len(named) >= PEER_MANY else PEER_NETS_SHOWN

    def _nets(nets: list) -> str:
        if not nets:
            return ""
        shown = ", ".join(f"<code>{_e(n)}</code>" for n in nets[:limit])
        more = len(nets) - limit
        return ": " + shown + (f" и ещё {more}" if more > 0 else "")
    # подписи слотов — на кнопках карточек ниже, в строке только имена
    pairs = " ↔️ ".join(f"{_e(name)}{_nets(nets)}" for name, _label, nets in named)
    return f"🔗 Связь подсетей: {pairs}"


GATEWAYS_ABOUT = ("трафик несёт один шлюз, второй ждёт в резерве;\n"
                  "перестал отвечать активный, а резерв жив — бот переключит трафик на резерв и останется на нём;\n"
                  "вернуть — кнопкой «▶️ Переключить на …»")
# автопереключение выключено: «подробнее» не обещает переключения
GATEWAYS_ABOUT_MANUAL = ("трафик несёт один шлюз, второй ждёт в резерве;\n"
                         "переключить вручную — кнопкой «▶️ Переключить на …»")
ROUTING_PROVISION_INTRO = (ui.head("🛰 Шлюзы", meta=["🇷🇺 РФ-доступ не развёрнут"]) + "\n"
                           "«🚀 Развернуть» поставит на сервер AWG dnsmasq, перехват DNS клиентов, NAT, маршруты и "
                           "линк до будущего шлюза — займёт до минуты. Шлюз назначишь следующим шагом.")
SETTINGS_ROUTING_ABSENT = (ui.head("🛰 Шлюзы", meta=["🇷🇺 РФ-доступ ждёт перезапуска бота"]) + "\n"
                           "На сервере всё готово — РФ-доступ заработает после перезапуска бота")
GATEWAYS_OFF = ui.head("🛰 Шлюзы", meta=["🇷🇺 РФ-доступ выключен", "разрешения и списки сохранены"])
SETTINGS_ROUTING_SUBOFF = "🇷🇺 РФ-доступ выключен — раздел пуст, пока он не включён"
GATEWAYS_AUTO_OFF = ("⚠️ Автопереключение выключено: упадёт активный шлюз — российские сервисы станут "
                     "открываться с зарубежного адреса, пока не переключишь на резерв вручную")
GATEWAYS_NO_SLOT = ("Шлюз — устройство в локальной сети, через которое российские сервисы открываются с "
                    "российского адреса. Назначь своё устройство или создай новое")


def _lists_tail(info: dict | None) -> str:
    """«списки 41 200, 2 ч назад» — для строки экрана «Шлюзы»; не обновлялись
    — без числа."""
    if not info:
        return ""
    age = info.get("age_seconds")
    if age is None:
        return "списки ещё не обновлялись"
    return f"списки {num(info.get('count', 0))}, {timeutil.age_ago(age)}"


def gateways_text(states: list, *, status: tuple = (True, ""), switched_at: str = "",
                  auto_on: bool = True, peer_info: dict | None = None,
                  lists: dict | None = None) -> str:
    """Экран «🛰 Шлюзы» при включённой функции: заголовок с состоянием (слотов
    нет — «🔴 шлюз не назначен» и что такое шлюз), строки слотов, под ними
    предупреждение о выключенном автопереключении, переключение и списки,
    «подробнее» (без обещания переключения, когда оно выключено), связь
    подсетей. Что значит ⭐ — в README, не на экране."""
    ok, reason = status
    if not states:
        return "\n".join([ui.head("🛰 Шлюзы", meta=[f"🇷🇺 {ROUTING_NAME} " + ui.st("bad", "шлюз не назначен")]),
                          GATEWAYS_NO_SLOT])
    head = ui.head("🛰 Шлюзы", meta=[f"🇷🇺 {ROUTING_NAME} " + (ui.st("ok") if ok else ui.st("bad", f"не работает: {_e(reason)}"))])
    lines = [head]
    lines += [slot_line(st, star=len(states) > 1) for st in states]
    if len(states) > 1 and not auto_on:
        lines.append(GATEWAYS_AUTO_OFF)
    tail = []
    if len(states) > 1 and switched_at:
        try:
            tail.append(f"Переключение: {timeutil.fmt_dt_ui(timeutil.parse_iso(switched_at))}, авто")
        except ValueError:
            pass
    lt = _lists_tail(lists)
    if lt:
        tail.append(lt)
    if tail:
        line = " · ".join(tail)
        lines.append(line[0].upper() + line[1:])
    if len(states) == 1:
        lines.append(f"Резерва нет: упадёт {slot_ref(states[0])} — российские сервисы станут открываться "
                     "с зарубежного адреса")
    else:
        lines.append(details(GATEWAYS_ABOUT if auto_on else GATEWAYS_ABOUT_MANUAL))
        if peer_info is not None:
            lines.append(peer_nets_line(peer_info))
    return "\n".join(lines)


def agent_bot_line(state: dict | None) -> str:
    """Последняя строка карточки: ссылка в чат бота шлюза — имя профиля из
    getMe по токену слота. Ссылка на диалог, без команд. Бота ещё не спросили
    (токена нет или Telegram не ответил) — строки нет."""
    bot = _bot_link((state or {}).get("agent_bot"))
    return f"Бот шлюза: {bot}" if bot else ""
