"""Условная маршрутизация и шлюз — сторона основного бота."""

from __future__ import annotations

from awgbot.util import timeutil
from awgbot.domain.services.gwchannel import drift_lines   # строки расхождения рисует домен
from awgbot.domain.gwchecks import WRITE_ERROR

from .fmt import _e, human_bytes, _updown, updown_brief, plain_ip, client_link, holder_link, plural_ru, details
from .updates import _ver


# ── шлюз условной маршрутизации ──────────────────────────────────────────────

def vps_hostname() -> str:
    """Имя ВПС для подписи «Пинг с …»; без имени — «сервера»."""
    import socket
    try:
        name = socket.gethostname().strip()
    except OSError:
        name = ""
    return name or "сервера"


def ping_line(ms) -> str:
    """«Пинг с AWG-SRV: 43 мс» — с ВПС до шлюза по линку."""
    return (f"Пинг с {_e(vps_hostname())}: "
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
    return "🔴" if state.get("unavailable") else "⏳"


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
    last = timeutil.fmt_handshake(dev.last_handshake)
    online = timeutil.handshake_is_online(dev.last_handshake)
    dot = "🟢" if online else "🔴"
    rx, tx = int(dev.traffic_rx_month), int(dev.traffic_tx_month)
    head = (f"{dot} 🛰 {_e(dev.name)} ({plain_ip(dev.address)}), последний коннект: {last}\n"
            # стрелки — со стороны шлюза: его исходящее — это tx сервера
            f"Потребление: {human_bytes(rx + tx)} {_updown(tx, rx)}")
    if state is not None:
        head += "\n\n" + ext_ip_line(state.get("ext_ip")) + "\n" + ping_line(state.get("ping_ms"))
    tail = ("\n\n<b>Это устройство — шлюз РФ-доступа, через него идёт "
            "трафик на РФ-домены.</b>")
    role = gateway_role_line(state)
    agent = agent_bot_line(state)
    return head + tail + (f"\n{role}" if role else "") + (f"\n\n{agent}" if agent else "")


def gateway_claim_marked(dev) -> str:
    return (f"🛰 Устройство «{_e(dev.name)}» ({plain_ip(dev.address)}) назначено шлюзом. "
            "Конфигурация ниже — перешли её боту шлюза и примени: без этого он себя "
            "шлюзом не считает.")


def gateway_claim_already(dev) -> str:
    return f"🛰 «{_e(dev.name)}» уже шлюз, ничего не менял."


# ── слоты шлюзов ───────────────────────────────


def slot_short(state) -> str:
    """«NASPi» (дом 1) — для уведомлений и кнопок."""
    dev, gw = state.get("device"), state.get("gateway")
    name = f"«{_e(dev.name)}»" if dev is not None else f"слот {gw.id}"
    return name + (f" ({_e(gw.label)})" if gw is not None and gw.label else "")


def _slot_down_mins(state) -> int:
    from awgbot.core import settings as _settings
    return max(1, int(state.get("down_ticks", 0)) * _settings.get_int("app.routing.probe_seconds", 30) // 60)


def _bot_link(agent_bot: dict | None) -> str:
    """Ссылка в чат бота шлюза из {'username', 'name'} (кэш getMe или снимок
    канала); бот неизвестен — пустая строка."""
    me = agent_bot or {}
    if not me.get("username"):
        return ""
    return f'<a href="https://t.me/{_e(me["username"])}">{_e(me.get("name") or me["username"])}</a>'


def _slot_note(state: str, gw: str, agent_bot: dict | None, error: str, purpose: str, what: str) -> str:
    """Строка состояния под записями SMB / своими списками в карточке слота —
    общие ветки: обновить шлюз (со ссылкой на бота), отказ шлюза, ожидание.
    purpose — «Необходимо» / «Для синхронизации необходимо», what — «записи» / «списки»."""
    if state == "old_agent":
        bot = _bot_link(agent_bot)
        return f"⚠️ {purpose} обновить шлюз{gw}" + (f" (бот: {bot})" if bot else "")
    if state == "failed":
        if error.startswith(WRITE_ERROR):
            return f"⚠️ Шлюз{gw}: {_e(error)}"          # поломка на шлюзе, не отказ
        return f"⚠️ Шлюз{gw} не смог принять {what}" + (f": {_e(error)}" if error else "")
    return "⏳ Синхронизация с другими шлюзами…"


ROUTER_IP_PLACEHOLDER = "АДРЕС_ШЛЮЗА"


def _nets_overlap(a: list, b: list) -> list:
    from awgbot.util import nets
    return nets.overlap(a, b)


# ── Условная маршрутизация ─────────────────────

ROUTING_NAME = "РФ-доступ"


# ── слоты: имя и состояние ───────────────────────────────────────────────────

def slot_name(state, star: bool = False) -> str:
    """«NASPi, дача» — имя устройства и подпись; star — «⭐» у
    предпочтительного, «🛰» у остальных не рисуется здесь (заголовок карточки
    ставит его сам)."""
    dev, gw = state.get("device"), state.get("gateway")
    name = _e(dev.name) if dev is not None else f"слот {gw.id}"
    pre = "⭐ " if star and state.get("preferred") else ""
    return pre + name + (f", {_e(gw.label)}" if gw is not None and gw.label else "")


def slot_status(state) -> str:
    """«🟢 Активен» / «🟢 Резерв» / «🟡 Резерв, проверка связи» /
    «🔴 Резерв, не отвечает 14 мин»."""
    role = "Активен" if state.get("active") else "Резерв"
    if state.get("link_ok"):
        return f"🟢 {role}"
    if state.get("unavailable"):
        age = state.get("handshake_age")
        what = "нет доступа в интернет" if age is not None and age <= 300 else "не отвечает"
        return f"🔴 {role}, {what} {_slot_down_mins(state)} мин"
    return f"🟡 {role}, проверка связи"


def slot_ref(state) -> str:
    """Имя шлюза для фраз («упадёт NASPi», «останется в резерве»): только имя
    устройства, без подписи и адреса."""
    dev, gw = state.get("device"), state.get("gateway")
    return _e(dev.name) if dev is not None else f"слот {gw.id}"


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


def peer_nets_line(info: dict) -> str:
    """Строка «↔️ Связь подсетей» на экране «Шлюзы» — во всех состояниях."""
    st = info.get("state", "off")
    if st == "off":
        return "↔️ Связь подсетей: выключена"
    def _plain(w: str) -> str:
        return _e(str(w).replace("«", "").replace("»", ""))
    who = ", ".join(_plain(w) for w in info.get("who") or [])
    if st == "no_lan":
        return (f"↔️ Связь подсетей не работает: у {who} выключен VPN-транзит — "
                "без него ответы не найдут дорогу назад")
    if st == "no_nets":
        return f"↔️ Связь подсетей не работает: у {who} не заданы подсети — «🗺 Подсети» в карточке шлюза"
    if st == "overlap":
        nets = ", ".join(_e(n) for n in info.get("nets") or [])
        w = info.get("who") or ["?", "?"]
        return (f"↔️ Связь подсетей не работает: подсети {_plain(w[0])} и {_plain(w[1])} пересекаются "
                f"({nets}) — смени подсеть одного из шлюзов")
    pairs = " ↔ ".join(f"{', '.join(_e(n) for n in nets)} ({_e(name)}{', ' + _e(label) if label else ''})"
                       for name, label, nets in info.get("pairs_named") or [])
    return f"↔️ Связь подсетей: {pairs}"


GATEWAYS_ABOUT = ("трафик несёт один шлюз, второй ждёт в резерве; активный перестал отвечать, а "
                  "резерв жив — бот перекладывает трафик сам и остаётся на нём, вернуть можно кнопкой")
ROUTING_PROVISION_INTRO = ("🇷🇺 РФ-доступ не развёрнут\n"
                           "Кнопка поставит dnsmasq, перехват DNS клиентов, NAT и маршруты, линк до "
                           "будущего шлюза — до минуты. Шлюз назначается следующим шагом")
SETTINGS_ROUTING_ABSENT = "🇷🇺 Обвязка развёрнута, функция ждёт перезапуска бота"
GATEWAYS_OFF = "🛰 Шлюзы · 🇷🇺 РФ-доступ выключен · разрешения и списки сохранены"
SETTINGS_ROUTING_SUBOFF = "🇷🇺 РФ-доступ выключен — раздел пуст, пока он не включён"
GATEWAYS_AUTO_OFF = ("⚠️ Автопереключение выключено: при падении активного шлюза РФ-доступ "
                     "выключится, а не перейдёт на резерв")


def _fmt_n(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def _lists_tail(info: dict | None) -> str:
    """«списки 41 200, 2 ч назад» — для строки экрана «Шлюзы»; не обновлялись
    — без числа."""
    if not info:
        return ""
    age = info.get("age_seconds")
    if age is None:
        return "списки ещё не обновлялись"
    return f"списки {_fmt_n(info.get('count', 0))}, {timeutil.age_ago(age)}"


def gateways_text(states: list, *, status: tuple = (True, ""), switched_at: str = "",
                  auto_on: bool = True, peer_info: dict | None = None,
                  lists: dict | None = None) -> str:
    """Экран «🛰 Шлюзы» при включённой функции: заголовок с состоянием,
    строки слотов, переключение и списки, связь подсетей, «подробнее»."""
    ok, reason = status
    head = f"🛰 Шлюзы · 🇷🇺 {ROUTING_NAME} " + ("🟢" if ok else f"🔴 не работает: {_e(reason)}")
    lines = [head]
    if not states:
        lines.append("Шлюз не назначен")
        return "\n".join(lines)
    lines += [slot_line(st, star=len(states) > 1) for st in states]
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
        lines.append(f"Резерва нет: упадёт {slot_ref(states[0])} — РФ-сервисы станут открываться "
                     "с зарубежного адреса")
    else:
        if peer_info is not None:
            lines.append(peer_nets_line(peer_info))
        if not auto_on:
            lines.append(GATEWAYS_AUTO_OFF)
        lines.append(details(GATEWAYS_ABOUT))
        lines.append("⭐ — предпочтительный при холодном старте")
    return "\n".join(lines)


# ── карточка слота ───────────────────────────────────────────────────────────

def services_line(svc: dict, name: str = "", agent_bot: dict | None = None) -> str:
    """«🗂 SMB: свои — 1, извне — 2 · 🟢 доступны»; нулевая часть не выводится,
    обе нулевые — «🗂 SMB: не найдены»; что с ними на шлюзе — строкой под ней."""
    own, peer = int(svc.get("own") or 0), int(svc.get("peer") or 0)
    if not own and not peer:
        return "🗂 SMB: не найдены"
    parts = ([f"свои — {own}"] if own else []) + ([f"извне — {peer}"] if peer else [])
    head = "🗂 SMB: " + ", ".join(parts)
    if not peer:
        return head
    state = svc.get("state", "")
    gw = f" {name}" if name else ""
    if state == "applied":
        return head + " · 🟢 доступны"
    if state == "reissue":
        note = f"⚠️ Необходим перевыпуск конфигурации шлюза{gw}"
    else:
        note = _slot_note(state, gw, agent_bot, svc.get("error") or "", "Необходимо", "записи")
    return head + "\n" + note


def own_lists_line(own: dict, name: str = "", agent_bot: dict | None = None) -> str:
    """«📋 Свои списки: 5 в туннель, 1 напрямую» / «пусто»; что с ними на
    шлюзе — строкой сразу под ней, только если не «применены»."""
    vpn, ru = int(own.get("vpn") or 0), int(own.get("ru") or 0)
    parts = ([f"{vpn} в туннель"] if vpn else []) + ([f"{ru} напрямую"] if ru else [])
    head = "📋 Свои списки: " + (", ".join(parts) if parts else "пусто")   # нулевая часть не выводится
    state = own.get("state", "")
    gw = f" {name}" if name else ""
    if state == "applied":
        return head
    if state == "offline":
        note = f"⏳ Синхронизируется со шлюзом{gw}, когда он выйдет на связь"
    else:
        note = _slot_note(state, gw, agent_bot, own.get("error") or "", "Для синхронизации необходимо", "списки")
    return head + "\n" + note


def channel_lines(ch: dict | None, server_ok) -> tuple[str, list[str], str]:
    """Канал в карточке: (строка «🔗 Упр. канал …», предупреждения открытыми
    строками, «выход наружу» для «подробнее»). Всё, что пришло с малины, —
    недоверенные данные: только через _e."""
    if not ch:
        return "", [], ""
    if not ch.get("ever"):
        return ("🔗 Упр. канал: ещё не поднимался — перевыпусти конфигурацию шлюза", [], "")
    if ch.get("online"):
        head = "🔗 Упр. канал 🟢"
    else:
        seen = ch.get("seen") or ""
        when = ""
        if seen:
            try:
                when = " с " + timeutil.fmt_dt_ui(timeutil.parse_iso(seen))
            except ValueError:
                when = ""
        head = f"🔗 Упр. канал ⚪ нет связи{when}"
    warns: list[str] = []
    egress = ""
    if ch.get("has_snap"):
        agent = _e(str(ch.get("agent") or "?")[:32])          # строка со шлюза — с пределом
        head += f" · {_ver(agent)}"
        gen, mine = ch.get("awg_gen"), ch.get("awg_gen_mine")
        if gen is not None and mine is not None and gen != mine:
            warns.append(f"⚠️ Поколение AWG на шлюзе {gen}, у сервера {mine}")
        drift = ch.get("drift") or []
        stale = "" if ch.get("online") else f" (по снимку {timeutil.age_ago(ch.get('age'))})"
        if not ch.get("has_bundle"):
            head += " · ⚙️ конфиг: шлюз ещё не сообщал"
        elif drift:
            ack = ch.get("ack") or {}
            n = len(drift)
            n_items = f"{n} " + plural_ru(n, "пункт", "пункта", "пунктов")
            if ch.get("online") and ack and not ack.get("ok"):
                warns.append("⚠️ Шлюз не применил настройки с сервера: "
                             f"{_e(ack.get('error') or 'ошибка')} — вернул прежние")
            elif ch.get("online") and ch.get("drift_bundle") and not ch.get("drift_channel"):
                warns.append("⚠️ Локальные подсети других шлюзов неактуальны — перевыпусти "
                             "конфигурацию шлюза")
            elif ch.get("online"):
                tail = ("; для подсетей других шлюзов перевыпусти конфигурацию шлюза"
                        if ch.get("drift_bundle") else "")
                warns.append(f"⏳ Конфигурация на шлюзе расходится с выданной ({n_items}) — "
                             f"изменения уходят каналом и применятся сами{tail}")
            else:
                items = "\n".join(f"   • {d}" for d in drift_lines(ch.get("drift_items") or [], html=True))
                warns.append(f"⚠️ Конфигурация на шлюзе расходится с выданной ({n_items}, по снимку "
                             f"{timeutil.age_ago(ch.get('age'))}):\n{items}")
        else:
            head += f" · 🟢 конфиг актуален{stale}"
        gen_hint = {"old": "⚠️ Обвязка шлюза старого образца — перевыпусти файл конфигурации",
                    "none": "⚠️ Обвязка шлюза не развёрнута — перевыпусти файл конфигурации"}
        if ch.get("has_bundle") and ch.get("plumbing_gen") in gen_hint:
            warns.append(gen_hint[ch["plumbing_gen"]])
        gw_ok = ch.get("egress_gw")
        mark = {True: "есть", False: "нет", None: "не знает"}
        egress = (f"выход наружу: сервер — {mark[bool(server_ok)]}, "
                  f"шлюз — {mark[gw_ok if isinstance(gw_ok, bool) else None]}")
    skew = ch.get("clock_skew")
    if isinstance(skew, int) and abs(skew) >= 120:
        side = "спешат" if skew > 0 else "отстают"
        warns.append(f"⏱ Часы шлюза {side} на {abs(skew) // 60} мин — проверь синхронизацию времени "
                     "(TLS и расписания от неё зависят)")
    pn = ch.get("peer_nets") or {}
    if isinstance(pn, dict) and pn and not pn.get("ok"):
        miss = ", ".join(_e(str(n)[:18]) for n in (pn.get("missing") or [])[:8]) or "подсетей"
        warns.append(f"⚠️ Связь подсетей: на шлюзе нет {miss} — перевыпусти конфигурацию шлюза")
    lists = ch.get("lists") or {}
    if lists and not lists.get("ok"):
        warns.append(f"⚠️ Шлюз не принял фиды локальной сети: {_e(lists.get('error') or 'ошибка')}"
                     " — пока качает их сам")
    err = ch.get("error") or ""
    if err and not ch.get("online"):
        warns.append(f"Последний обрыв: {_e(err)}")
    return head, warns, egress


def _gw_traffic(dev) -> str:
    rx, tx = int(dev.traffic_rx_month), int(dev.traffic_tx_month)
    if rx + tx <= 0:
        return ""
    # стрелки — со стороны шлюза: его исходящее — это tx сервера
    return f" · 📊 {human_bytes(rx + tx)} {updown_brief(tx, rx)}"


def gateway_card_text(state: dict, states: list) -> str:
    """Карточка слота: заголовок со значком («⭐» у предпочтительного, «🛰» у
    остальных), линк, адрес и трафик, упр. канал, подсети и VPN-транзит,
    свои списки, связь подсетей, SMB, бот шлюза; предупреждения открытыми
    строками; редкое — под «подробнее»."""
    gw, dev = state["gateway"], state.get("device")
    name = f"«{_e(dev.name)}»" if dev is not None else f"слот {gw.id}"
    two = len(states) > 1
    lines = [f"{'⭐' if state.get('preferred') and two else '🛰'} {slot_name(state)} — {slot_status(state)}"]
    age = state.get("handshake_age")
    if age is None or age > 300:
        hs = "хендшейка нет"
    else:
        hs = "хендшейк " + (f"{int(age)} с" if age < 60 else f"{int(age) // 60} мин")
    lines.append(f"📡 {_e(gw.link_if)}:{gw.link_port} · {hs}{_slot_ping_tail(state)}")
    ip = state.get("ext_ip")
    lines.append(("🌐 " + (plain_ip(ip) if ip else "адрес не определён"))
                 + (_gw_traffic(dev) if dev is not None else ""))
    if state.get("active") and state.get("unavailable"):
        lines.append("🔴 РФ-доступ недоступен: российские сервисы открываются с зарубежного адреса")
    ch_head, ch_warns, egress = channel_lines(state.get("channel"), state.get("link_ok"))
    if ch_head:
        lines.append(ch_head)
    lines += ch_warns
    nets = gw.home_subnets
    nets_s = ", ".join(_e(n) for n in nets[:6]) + (f" и ещё {len(nets) - 6}" if len(nets) > 6 else "") \
        if nets else "подсети не заданы"
    lines.append(f"🗺 {nets_s} · 🔀 VPN-транзит " + ("✅" if gw.lan_mode else "☑️"))
    if gw.lan_mode:
        own = state.get("own_lists") or {}
        if own and own.get("state") != "off":
            lines.append(own_lists_line(own, name, state.get("agent_bot")))
    if state.get("peer_nets"):
        lines.append("↔️ Связь подсетей ✅")
        if state.get("services"):
            lines.append(services_line(state["services"], name, state.get("agent_bot")))
    others = [s for s in states if s["gateway"].id != gw.id]
    conflict = next((s for s in others if _nets_overlap(nets, s["gateway"].home_subnets)), None)
    if conflict is not None:
        ov_nets = _nets_overlap(nets, conflict["gateway"].home_subnets)
        ov = ", ".join(_e(n) for n in ov_nets)
        verb = "пересекается" if len(ov_nets) == 1 else "пересекаются"
        first = min([state] + others, key=lambda s: (0 if s.get("preferred") else 1, s["gateway"].id))
        route = "маршрут достаётся ему, " if first["gateway"].id != gw.id else ""
        lines.append(f"⚠️ {ov} {verb} с подсетью «{slot_ref(conflict)}»: {route}связь подсетей между "
                     "этими шлюзами не работает, пока они пересекаются")
    agent = agent_bot_line(state)
    if agent:
        lines.append(agent)
    more = []
    if egress:
        more.append(egress)
    issued = state.get("issued_at") or ""
    if issued:
        try:
            more.append(f"конфигурация выпущена {timeutil.fmt_dt_ui(timeutil.parse_iso(issued))}")
        except ValueError:
            pass
    if nets:
        more.append("устройства админа достают до подсети через линк")
    if state.get("preferred") and two:
        more.append("предпочтительный при холодном старте")
    if more:
        lines.append(details(" · ".join(more)))
    return "\n".join(lines)


def gateway_edit_text(state: dict) -> str:
    return f"✏️ {slot_ref(state)} — изменить"


# ── диалоги слота ────────────────────────────────────────────────────────────

def _reissue_or_channel(online: bool, what: str) -> str:
    """Хвост диалога: при живом канале изменение доедет само, без файла."""
    if online:
        return f"{what} шлюз получит по каналу и применит сам; итог придёт в чат бота шлюза"
    return f"{what} потребует перевыпуска конфигурации шлюза"


def already_state(on: bool) -> str:
    """Всплывашка на повторное нажатие «Включить/Выключить» с той же целью."""
    return "Уже включено" if on else "Уже выключено"


GW_TOKEN_NOT_FORGOTTEN = ("⚠️ Токен бота этого устройства не убран из env бота сервера — "
                          "убери строку GW_BOT_TOKEN слота руками, иначе новый слот с тем же "
                          "номером получит его в файле первого применения")


def gateway_lan_ask(state: dict, on: bool, resolver: str) -> str:
    """Диалог «🔀 VPN-транзит»: что произойдёт, что нужно от человека, чем грозит."""
    name = slot_ref(state)
    online = bool((state.get("channel") or {}).get("online"))
    if on:
        lines = [f"🔀 VPN-транзит на {name} — включить?",
                 "Роутер отдаёт весь трафик сети шлюзу, шлюз маршрутизирует: заблокированное — в туннель, "
                 "остальное — напрямую. VPN на устройствах в сети становится не нужен",
                 "Нужно от тебя: настроить роутер по рецепту — покажу после включения",
                 "⚠️ Шлюз станет точкой отказа: упадёт — подсеть без интернета, резерв не поможет"]
        if not resolver:
            lines.append("⚠️ Свой резолвер не настроен: шлюз пойдёт на 1.1.1.1 через туннель, без "
                         "защиты от DoH — рекомендуется настроить (⚙️ → 🖥 Сервер AWG)")
        if not online:
            lines.append("Потребуется перевыпуск конфигурации шлюза")
        return "\n".join(lines)
    return "\n".join([
        f"🔀 VPN-транзит на {name} — выключить?",
        "⚠️ Сначала убери на роутере маршрутизацию всего трафика на шлюз — иначе сеть останется без интернета",
        f"Связь подсетей для {name} выключится; списки и резолвер снимутся, свои списки останутся"
        + ("" if online else ". " + _reissue_or_channel(False, "Выключение")),
    ])


GATEWAY_LAN_NO_SUBNET = "Сначала задай подсеть шлюза («🗺 Подсети»): без неё работать не будет"


ROUTER_TABS = (("mt", "MikroTik"), ("ow", "OpenWrt"))


def gateway_router_text(title: str, net: str, gw_ip: str = "", peer_nets: list | None = None,
                        tab: str = "mt") -> str:
    """❓ Роутер — вкладками: требования одной строкой, подробно — под
    «подробнее», рецепт выбранной вкладки. Адрес шлюза в подсети знает
    только он сам: основной бот показывает плейсхолдер."""
    net = net or "ПОДСЕТЬ"
    gw_ip = _e(gw_ip) if gw_ip else ROUTER_IP_PLACEHOLDER
    peers = [str(p) for p in (peer_nets or []) if p]
    head = (f"❓ Роутер для {_e(title)} · {_e(net)} · шлюз {gw_ip}\n"
            "Весь трафик сети, кроме шлюза и локального, — на шлюз; DNS по DHCP — шлюз; "
            "ускорение и IPv6 — выключить; асимметричный путь — разрешить")
    req = details("• весь трафик локальной сети, кроме самого шлюза и трафика внутри сети, — на адрес "
                  "шлюза (policy-based routing);\n"
                  "• разрешён асимметричный путь: ответ от шлюза к устройству идёт мимо роутера — "
                  "правило выше drop invalid;\n"
                  "• аппаратное ускорение (fasttrack, flow offloading) выключено;\n"
                  "• DHCP раздаёт DNS = адрес шлюза; у шлюза статический адрес;\n"
                  "• IPv6 в локальной сети выключен (RA/DHCPv6): резолвер шлюза AAAA не отдаёт, "
                  "но адрес v6 от роутера увёл бы трафик мимо туннеля")
    recipes = []
    if tab in ("ow", "all"):
        ow_peer = "".join(f"ip route add {_e(p)} via {gw_ip}\n" for p in peers)
        recipes.append("<b>OpenWrt</b>\n"
                  "<pre>echo '200 vpn' &gt;&gt; /etc/iproute2/rt_tables\n"
                  f"ip route add default via {gw_ip} table vpn\n"
                  f"ip rule add from {gw_ip} priority 100 lookup main\n"
                  f"ip rule add from {_e(net)} to {_e(net)} priority 110 lookup main\n"
                  f"ip rule add from {_e(net)} priority 120 lookup vpn\n"
                  f"iptables -I FORWARD 1 -s {_e(net)} ! -d {_e(net)} -j ACCEPT\n"
                  "uci set firewall.@defaults[0].flow_offloading_hw='0'\n"
                  "uci set firewall.@defaults[0].flow_offloading='0'\n"
                  f"{ow_peer}"
                  f"uci add_list dhcp.lan.dhcp_option='6,{gw_ip}'\n"
                  "uci commit</pre>\n"
                  "Порядок важен: сначала исключается сам шлюз, затем трафик внутри сети, потом всё "
                  "остальное уходит на шлюз. Закрепить: ip rule/route — в /etc/rc.local, "
                  "iptables — в /etc/firewall.user")
    if tab in ("mt", "all"):
        mt_peer = "".join(f"/ip route add dst-address={_e(p)} gateway={gw_ip}\n" for p in peers)
        recipes.insert(0, "<b>MikroTik RouterOS 7</b>\n"
                  "<pre>/routing table add disabled=no fib name=antiblock\n"
                  "/ip firewall mangle\n"
                  f"add action=accept chain=prerouting comment=anti-loop src-address={gw_ip}\n"
                  "add action=mark-routing chain=prerouting comment=all-LAN-via-gw \\\n"
                  f"    new-routing-mark=antiblock passthrough=no src-address={_e(net)} dst-address=!{_e(net)}\n"
                  f"/ip route add dst-address=0.0.0.0/0 gateway={gw_ip} routing-table=antiblock\n"
                  "/ip firewall filter\n"
                  f"add action=accept chain=forward comment=asym-via-gw src-address={_e(net)} dst-address=!{_e(net)}\n"
                  f"{mt_peer}"
                  f"/ip dhcp-server network set [find] dns-server={gw_ip}</pre>\n"
                  "Правило asym-via-gw — выше drop invalid; fasttrack выключить")
    recipe = "\n\n".join(recipes)
    peer_note = ("" if not peers else
                 "\nСвязь подсетей: сам роутер отвечает со своего адреса по основной таблице, мимо "
                 "заворота, — чтобы он был достижим из подсетей других шлюзов, ему нужен маршрут до "
                 "них через шлюз (строки с " + ", ".join(f"<code>{_e(p)}</code>" for p in peers)
                 + "). Устройствам за роутером это не нужно")
    return f"{head}\n{req}\n{recipe}{peer_note}"


def gateway_switch_ask(target: dict, current, healthy: bool) -> str:
    who = slot_ref(target)
    cur = slot_ref(current) if current else "Прежний шлюз"
    if healthy:
        return (f"▶️ Переключить трафик на {who}?\n"
                "РФ-сервисы у всех начнут выходить с адреса этой сети — приложения могут попросить "
                f"войти заново. {cur} останется в резерве, обратно бот сам не вернёт")
    return (f"⚠️ {who} не отвечает {_slot_down_mins(target)} мин — точно переключаем?\n"
            "РФ-сервисы у всех перестанут работать, пока он не оживёт")


def gateway_peer_ask(on: bool) -> str:
    """Диалог тумблера «↔️ Связь подсетей»."""
    if on:
        return "\n".join([
            "↔️ Связь подсетей — включить?",
            "Устройства из подсети одного шлюза достанут до подсети другого по настоящим адресам, "
            "через AWG. Только твои локальные сети и только между шлюзами с включённым VPN-транзитом",
            "SMB-серверы подсетей: на Windows — <code>\\\\имя.awg.internal</code>, на macOS — в Finder: "
            "«Сеть» → awg.internal",
            "После включения перевыпусти конфигурацию каждого шлюза: подсети шлюзов живут в конфиге "
            "линка, а его везёт только файл",
            details("«VPN-транзит» гарантирует, что весь трафик подсети идёт через шлюз с обеих сторон, "
                    "иначе ответы не найдут дорогу назад · SMB-серверы видны только с тех подсетей, где на "
                    "шлюзе запущен avahi-daemon · задержка складывается из задержек шлюзов до сервера AWG; "
                    "связь живёт, пока подняты оба линка"),
        ])
    return ("↔️ Связь подсетей — выключить?\n"
            "Подсети шлюзов перестанут видеть друг друга сразу. Необходим перевыпуск конфигурации "
            "каждого шлюза")


def gateway_home_text(state: dict) -> str:
    gw = state["gateway"]
    nets = gw.home_subnets
    cur = ", ".join(f"<code>{_e(n)}</code>" for n in nets) if nets else "не заданы"
    lines = [f"🗺 Подсети {slot_ref(state)} · сейчас {cur}",
             "Пришли подсети через пробел: <code>192.168.2.0/24</code>; «-» — убрать все. "
             "Доступ через туннель — только твоим устройствам"]
    if gw.lan_mode:
        lines.append("<b>VPN-транзит</b> будет работать для первой подсети в списке")
    if state.get("peer_nets_enabled"):
        lines.append("<b>Связать</b> можно только непересекающиеся подсети")
    return "\n".join(lines)


def gateway_home_report(res: dict, state: dict) -> str:
    """Итог правки подсетей — первыми строками карточки."""
    parts = []
    kept = res.get("kept") or []
    shown = ", ".join(_e(n) for n in kept[:8]) + (f" и ещё {len(kept) - 8}" if len(kept) > 8 else "")
    parts.append(f"✅ Подсети {slot_ref(state)}: " + (shown if kept else "убраны"))
    rejected = res.get("rejected") or []
    if rejected:
        shown = "; ".join(f"<code>{_e(raw[:40])}</code> — {_e(why)}" for raw, why in rejected[:3])
        more = f"; и ещё {len(rejected) - 3}" if len(rejected) > 3 else ""
        parts.append(f"⚠️ Не принято: {shown}{more}")
    conflict = res.get("conflict")
    if conflict is not None:
        ov_nets = _nets_overlap(kept, conflict.home_subnets)
        ov = ", ".join(f"<code>{_e(n)}</code>" for n in ov_nets) or "подсеть"
        verb = "пересекаются" if len(ov_nets) > 1 else "пересекается"
        who = _e(res["conflict_name"]) if res.get("conflict_name") else f"шлюза №{conflict.id}"
        parts.append(f"⚠️ {ov} {verb} с подсетью {who}: маршрут достаётся предпочтительному "
                     "(при равенстве — первому), связь подсетей между этими шлюзами не работает, пока "
                     "они пересекаются")
    elif res.get("peer_others"):
        parts.append("Подсети попадут в конфигурацию остальных шлюзов: необходим перевыпуск для "
                     + ", ".join(_e(w) for w in res["peer_others"]))
    return "\n".join(parts)


def gateway_label_text(state: dict) -> str:
    return (f"✏️ Подпись {slot_ref(state)} — место одним-двумя словами: «дача», «офис». "
            "До 20 символов, «—» — убрать")


def gateway_remove_ask(dev, *, state=None, other=None) -> str:
    """Три варианта: резервный; активный при живом резерве; последний."""
    name = _e(dev.name)
    if state and not state.get("active") and other is not None:
        return (f"🛑 {name} — больше не резерв?\n"
                f"Линк снимется; трафик пойдёт через {slot_ref(other)}, резерва не будет. "
                "Устройство станет обычным")
    if state and state.get("active") and other is not None:
        return (f"🛑 {name} — больше не шлюз?\n"
                f"Трафик сразу перейдёт на {slot_ref(other)} — адрес сменится, приложения могут "
                "попросить войти заново. Линк снимется; устройство станет обычным")
    return (f"🛑 {name} — больше не шлюз?\n"
            "Ключи линка сменятся; РФ-доступ выключится до назначения нового. Устройство станет обычным")


def gateway_removed(dev, now_active=None) -> str:
    if now_active is not None:
        return f"🛑 {_e(dev.name)} больше не шлюз · трафик идёт через {slot_ref(now_active)}, резерва нет"
    return f"🛑 {_e(dev.name)} больше не шлюз · РФ-доступ выключен до назначения нового"


# ── назначение шлюза ─────────────────────────────────────────────────────────

GATEWAY_CHOOSE_INTRO = ("🛰 Назначить шлюз — одно из твоих устройств или новое. Для шлюза поднимется "
                        "отдельный линк со своим ключом и портом; первый файл конфигурации применяется "
                        "на устройстве руками")
GATEWAY_STANDBY_CHOOSE_INTRO = ("🛰 Резервный шлюз — когда основной перестанет отвечать, РФ-доступ "
                                "будет работать через него. Одно из твоих устройств или новое; линк — "
                                "свой, первый файл конфигурации применяется руками")
GATEWAY_PICK_INTRO = "📱 Из моих устройств — выбери, какое станет шлюзом. Обычным устройством оно быть перестанет"
GATEWAY_PICK_EMPTY = "📱 У профиля админа нет устройств, выпущенных ботом — назначь новое"


def gateway_replace_intro(state: dict) -> str:
    name = slot_ref(state)
    role = ("Слот резервный: трафик клиентов не затронут" if not state.get("active")
            else "Активный слот: трафик уйдёт на резерв, если он жив")
    return (f"🔁 Заменить устройство {name}? Ключи линка сменятся — прежнее потеряет линк само. {role}. "
            "Одно из твоих устройств или новое")


def gateway_mark_ask(dev, prev, *, standby: bool = False, replace_state=None) -> str:
    head = (f"🛰 {_e(dev.name)} станет шлюзом? Выйдет из лимитов; удалить, заблокировать, выдать "
            "ссылку будет нельзя. Ключи линка — новые, файл первого применения выпущу сразу")
    if prev is not None:
        head += f"\nСейчас шлюз — {_e(prev.name)}: прежнее устройство потеряет линк само"
    if replace_state is not None and replace_state.get("active"):
        head += "\nАктивный слот: трафик уйдёт на резерв, если он жив"
    return head


def gateway_new_ask(slot: int = 1, prev_name: str = "", active: bool = False) -> str:
    """Замена машины слота новым устройством — те же строки, что при выборе
    из своих: кто сейчас в слоте и что будет с трафиком."""
    name = "Шлюз" if slot <= 1 else f"Шлюз {slot}"
    text = f"➕ Новое устройство «{name}» в твоём профиле: выпущу ключи линка и файл первого применения"
    if prev_name:
        text += f"\nСейчас шлюз — {_e(prev_name)}: прежнее устройство потеряет линк само"
    if active:
        text += "\nАктивный слот: трафик уйдёт на резерв, если он жив"
    return text


def gateway_ask_token(slot: int = 1) -> str:
    who = "шлюза" if slot <= 1 else f"шлюза {slot}"
    return (f"🤖 Токен бота {who} — создай бота у @BotFather и пришли токен <code>123456789:AA…</code>. "
            "Уедет в файл первого применения; сообщение с токеном сразу удалю")


def routing_provisioned(tail: str) -> str:
    return ("✅ Обвязка развёрнута, линк до шлюза поднят, функция включена\n"
            "Интерфейс линка читается при старте: после перезапуска бота назначь шлюз — "
            "«🛰 Шлюзы» на главной"
            + (f"\n<pre>{_e(tail[-700:])}</pre>" if tail else ""))


# ── параметры и доступ ───────────────────────────────────────────────────────

def routing_params_text(info: dict, lists: dict) -> str:
    """«⚙️ Параметры РФ-доступа»: проверка живости и списки одной строкой
    каждая, объяснение — под «подробнее»."""
    cnt, src = int(lists.get("count", 0)), int(lists.get("sources", 0))
    age = lists.get("age_seconds")
    if age is None:
        lists_line = f"Списки: ещё не обновлялись · раз в {lists.get('every_hours', 6)} ч"
    else:
        lists_line = (f"Списки: {_fmt_n(cnt)} {plural_ru(cnt, 'запись', 'записи', 'записей')} из "
                      f"{src} {plural_ru(src, 'источника', 'источников', 'источников')}, {timeutil.age_ago(age)} · "
                      f"раз в {lists.get('every_hours', 6)} ч")
    need = int(info["need"])
    return "\n".join([
        "⚙️ Параметры РФ-доступа",
        f"Проверка живости: такт {info['probe_seconds']} с · окно {info['window']} · "
        f"порог {info['availability']}% ({need} {plural_ru(need, 'неудача', 'неудачи', 'неудач')} из {info['window']})",
        lists_line,
        details("наружу зонд ходит не каждый такт — трафик клиентов сам доказывает путь, без него "
                "зонд реже (защита от поведенческих блокировок) · резерв зондируется раз в "
                f"{info['standby_minutes']} мин · второе автопереключение не раньше чем через "
                f"{info['interval_minutes']} мин · окно живёт в памяти бота и после перезапуска "
                "копится заново"),
    ])


def routing_users_text() -> str:
    return ("👥 Кому доступен РФ-доступ (тебе — всегда)\n"
            "Владельцы устройств управляют настройкой на них сами")


ROUTING_DISABLE_CONFIRM = ("🔴 Выключить РФ-доступ для всех? Российские сервисы снова будут ругаться "
                           "на VPN; разрешения и списки сохранятся")


def gateway_bundle_applied_text(display: str, ok: bool, error: str = "") -> str:
    """Уведомление в чате админа по итогу применения файла на шлюзе (каналом):
    файл и карточка над ним уходят, следом — карточка слота."""
    if ok:
        return f"✅ Конфигурация шлюза <b>{_e(display)}</b> успешно обновлена"
    return (f"⚠️ Конфигурация шлюза <b>{_e(display)}</b> не обновлена"
            + (f": {_e(error)}" if error else ""))


def gateway_installed_text(display: str, agent_bot: dict | None) -> str:
    """Шлюз, поставленный файлом первого применения, вышел на связь: агент
    стоит, канал поднят. Ссылка на бота — из снимка канала (агент знает
    своего бота сам); без неё строки бота нет."""
    me = agent_bot or {}
    text = f"✅ Шлюз <b>{_e(display)}</b> успешно настроен 🎉"
    if me.get("username"):
        text += (f'\n<b>Бот шлюза:</b> <a href="https://t.me/{_e(me["username"])}">'
                 f'{_e(me.get("name") or me["username"])}</a>')
    return text


def gateway_bundle_caption(display: str, agent_bot: dict | None) -> str:
    """Подпись под файлом конфигурации: какому шлюзу и какому боту его
    пересылать — ссылкой в чат, как в карточке. Бота ещё не спросили —
    без ссылки."""
    me = agent_bot or {}
    who = "боту шлюза"
    if me.get("username"):
        who += f' (<a href="https://t.me/{_e(me["username"])}">{_e(me.get("name") or me["username"])}</a>)'
    return (f"📤 Конфигурация шлюза <b>{_e(display)}</b>.\n"
            f"Перешли это сообщение {who} — он проверит и применит сам.\n"
            "Результат применения конфигурации сообщит бот шлюза.\n\n"
            "ℹ️ Возврат в меню удалит это сообщение")


def gateway_plain_bundle_caption(display: str) -> str:
    """Подпись под файлом первого применения: инструкция — сообщением выше."""
    return ("🛰 Файл конфигурации шлюза\n"
            f"Воспользуйся инструкцией выше для настройки нового шлюза: <b>{_e(display)}</b>\n\n"
            "После возврата в меню сообщение с файлом и инструкция удалятся из чата.")


def routing_provision_failed(reason: str) -> str:
    return ("⚠️ Обвязка не развёрнута.\n\n<pre>" + _e(str(reason)[-900:]) + "</pre>\n\n"
            "Ничего наполовину не осталось: следующий запуск начнёт с того же места. "
            "Частая причина — нет пакета dnsmasq в репозиториях образа.")


GW_INSTALL_URL = ("https://raw.githubusercontent.com/justSunny12/awg-bot/main/"
                  "install/awg-bot-install.sh")


def gateway_install_instructions(dev, bundle_name: str = "awg-gw-bundle.sh",
                                 routing_reset: bool = False) -> str:
    """Что делать с файлом первого применения — ОДНА строка со своей машины.

    Копирование и установка склеены намеренно: установка на шлюзе не задаёт
    вопросов, значит её незачем отделять от копирования и незачем заходить на
    шлюз отдельным сеансом. `ssh -t` — чтобы у sudo был терминал для пароля.
    Поставка агента едет ВНУТРИ файла: с шлюза в России GitHub без туннеля не
    достать, а туннель этим файлом и ставится.
    """
    reset = "\nРФ-доступ у устройства снят: шлюзу он не нужен." if routing_reset else ""
    return (f"🛰 Шлюзом назначен «{_e(dev.name)}» ({plain_ip(dev.address)}).{reset}\n\n"
            "Сохрани файл ниже и выполни <b>со своего компьютера</b> (из директории с файлом) "
            "одну команду — она скопирует его на шлюз и сразу поставит агента:\n\n"
            f"<code>scp {_e(bundle_name)} root@ШЛЮЗ:/root/ &amp;&amp; \\\n"
            f"  ssh -t root@ШЛЮЗ 'sudo sh /root/{_e(bundle_name)} --install'</code>\n\n"
            "Установка полностью автоматическая: все необходимые настройки уже собраны в файле.\n"
            "Когда установка закончится, отправь <b>боту шлюза</b> <code>/start</code> — "
            "и всё готово 🙂\n\n"
            "После установки файл конфигурации удалится с хоста шлюза сам.")


def routing_status_line(ok: bool) -> str:
    """Строка о состоянии РФ-доступа в ОБЩЕМ статусном блоке клиента — рядом
    со статусом сервера, а не отдельным сообщением. Два состояния: трафик
    проходит или нет; про шлюзы и резерв клиенту знать незачем.

    Показывается только тем, кому админ функцию разрешил: рассказывать про
    механизм тому, кто им не пользуется, — шум.
    """
    return (f"🇷🇺 {ROUTING_NAME}: 🟢 работает" if ok else
            f"🇷🇺 {ROUTING_NAME}: 🔴 не работает")


GW_CARD_PAYLOAD = "gw"          # «/start gw-<слот>» — карточка слота из шапки


def routing_admin_status_line(info: dict, bot_username: str = "") -> str:
    """Та же строка в шапке админа — с тем, кто несёт трафик, и состоянием
    резерва (services.routing_admin_status): один шлюз — «работает (имя)»;
    два — «…, резерв жив / не отвечает / проверяется», мёртвый резерв красит
    строку в 🟠; выключен — кто именно не отвечает. Имя шлюза и слово
    «резерв» — ссылки в карточку слота (deep-link на себя), когда username
    известен; состояние резерва остаётся текстом."""
    from .fmt import deep_link
    active, standby = info.get("active", ""), info.get("standby") or []

    def _link(label: str, slot) -> str:
        return deep_link(bot_username, f"{GW_CARD_PAYLOAD}-{int(slot)}", label) if slot else _e(label)

    if info.get("ok"):
        dead = [s for s in standby if s["state"] == "dead"]
        dot = "🟠" if dead else "🟢"
        line = f"🇷🇺 {ROUTING_NAME}: {dot} работает"
        if active:
            line += f" ({_link(active, info.get('active_slot'))})"
        if standby:
            # ссылка — только на слове «резерв»: состояние остаётся текстом
            st = standby[0]
            tail = {"alive": "жив", "dead": "не отвечает"}.get(st["state"], "проверяется")
            line += ", " + _link("резерв", st.get("slot")) + " " + tail
        return line
    names = ([(active, info.get("active_slot"))] if active else []) \
        + [(s["name"], s.get("slot")) for s in standby if s["state"] != "alive"]
    alive = [s for s in standby if s["state"] == "alive"]
    if not names and not alive:
        return ""                                     # шлюзов нет — строке нечего сказать
    line = f"🇷🇺 {ROUTING_NAME}: 🔴 недоступен"
    if len(names) == 1:
        line += f", {_link(*names[0])} не отвечает"
    elif names:
        line += " — " + ", ".join(_link(n, s) for n, s in names) + " не отвечают"
    if alive:
        line += ", " + _link("резерв", alive[0].get("slot")) + " жив"
    return line


def agent_bot_line(state: dict | None) -> str:
    """Последняя строка карточки: ссылка в чат бота шлюза — имя профиля из
    getMe по токену слота. Ссылка на диалог, без команд. Бота ещё не спросили
    (токена нет или Telegram не ответил) — строки нет."""
    bot = _bot_link((state or {}).get("agent_bot"))
    return f"Бот шлюза: {bot}" if bot else ""


ROUTING_ABOUT = (
    "Включишь — банки, госуслуги, маркетплейсы будут открываться с российского "
    "адреса, заблокированное — через VPN. Ссылки менять не нужно"
)
ROUTING_ABOUT_OFF = ROUTING_ABOUT

ROUTING_ADD_PROMPT = (
    "➕ Сайты с российского адреса\n"
    "Пришли адреса — по одному в строке или через запятую, можно ссылками. "
    "Добавляй то, что пишет «вы не из России»"
)

ROUTING_APPLY_HINT = "применится в теч. минуты, не сработало — переподключись"
ROUTING_ADDED_HINT = ROUTING_APPLY_HINT
ROUTING_SITES_ABOUT = ("Открываются с российского адреса. Банки, госуслуги, маркетплейсы — "
                       "уже в общем списке")


def _short(domain: str, limit: int = 60) -> str:
    return domain if len(domain) <= limit else domain[:limit - 1] + "…"


def routing_domain_removed(domain: str) -> str:
    """Всплывашка после «➖» — не длиннее 200 знаков answer(): домен режется."""
    return f"{_short(domain)} убран · применится в теч. минуты"


def _lent_out_lines(lent_out) -> list[str]:
    return [f"{_e(d.name)} — у профиля {holder_link(d)}, управляет функцией он" for d in lent_out]


def routing_panel_text(*, enabled: int, total: int, domains: list, lent_out=(),
                       link_ok: bool = True) -> str:
    """Экран «🇷🇺 РФ-доступ»: охват первой строкой, свои сайты второй; на всех
    выключено — объяснение открытым текстом; переданные — строкой без кнопки."""
    head = f"🇷🇺 {ROUTING_NAME}"
    lines = []
    if not total:
        lines.append(f"{head}: устройств пока нет")
    elif not enabled:
        lines += [f"{head}: выкл", ROUTING_ABOUT]
    else:
        lines.append(f"{head}: вкл на " + ("всех" if enabled >= total else f"{enabled} из {total}"))
        if not link_ok:
            lines.append("🔴 временно недоступен")
    if domains:
        shown = ", ".join(_e(d) for d in domains[:2])
        more = f" +{len(domains) - 2}" if len(domains) > 2 else ""
        lines.append(f"Свои сайты: {shown}{more}")
    lines += _lent_out_lines(lent_out)
    return "\n".join(lines)


def routing_sites_text(domains: list) -> str:
    """Экран «📋 Сайты»: счётчик и подсказка; сами адреса — кнопками «➖»."""
    if not domains:
        return ("📋 Свои сайты\n"
                "Тут пока пусто. Банки, госуслуги, маркетплейсы — уже в общем списке; "
                "добавляй то, что пишет «вы не из России»")
    return f"📋 Свои сайты · {len(domains)}\n{ROUTING_SITES_ABOUT}"


def routing_add_report(added: list, rejected: list, over_limit: int, limit: int) -> str:
    """Итог разбора пачки — первой строкой экрана «Сайты»: что взято, что нет
    и почему; человек вставляет списком, и молча взять половину нельзя."""
    parts = []
    if added:
        shown = ", ".join(_e(_short(d)) for d in added[:5])
        more = f" и ещё {len(added) - 5}" if len(added) > 5 else ""
        parts.append(f"✅ Добавлено: {shown}{more}")
    if rejected:
        shown = "; ".join(f"{_e(_short(raw, 40))} — {_e(reason)}" for raw, reason in rejected[:3])
        more = f"; и ещё {len(rejected) - 3}" if len(rejected) > 3 else ""
        parts.append(f"⚠️ Не добавлено: {shown}{more}")
    if over_limit:
        parts.append(f"📦 Не поместилось: {over_limit} — в списке максимум {limit} "
                     f"{plural_ru(limit, 'адрес', 'адреса', 'адресов')}")
    if not parts:
        return "⚠️ Не нашёл в сообщении ни одного адреса"
    if added:
        parts.append(ROUTING_APPLY_HINT)
    return " · ".join(parts)


def routing_clear_ask(n: int) -> str:
    return f"🗑 Удалить все свои сайты ({n})? Общий список останется"


ROUTING_CLEAR_CONFIRM = routing_clear_ask(0)

ROUTING_UNAVAILABLE = "РФ-доступ временно недоступен. Попробуй позже"


def routing_gateway_warning(verdict: str, *, at_start: bool) -> str:
    """Замер шлюза не прошёл: одна формулировка для замечаний при запуске и для
    момента включения функции в настройках. PROBE_OK — пусто."""
    from awgbot.infra import routing as _rt
    when = "на старте" if at_start else "при включении"
    if verdict == _rt.PROBE_NO_PATH:
        return ("шлюз РФ-доступа отвечает, но интернета за ним нет — "
                "чинить на самом шлюзе (аплинк, ip_forward, MASQUERADE). Маркировка "
                "снята, российские сервисы временно открываются с зарубежного адреса")
    if verdict != _rt.PROBE_OK:
        return (f"шлюз РФ-доступа не отвечает {when} — маркировка снята, "
                "российские сервисы временно открываются с зарубежного адреса")
    return ""

# Подтверждение выключения фичи целиком (общая настройка, не профиль).

# Владельцу профиля — админ выдал/отозвал разрешение на РФ-доступ.
ROUTING_GRANTED_NOTICE = (
    "🇷🇺 К твоей подписке добавлен РФ-доступ 🎉\n"
    "РФ-приложения и сайты теперь будут работать со включённым VPN. Включён на всех "
    "твоих устройствах; выключить и добавить свои сайты — в разделе «🇷🇺 РФ-доступ»"
)
def routing_granted_holder_notice(donor) -> str:
    return (f"🇷🇺 К устройствам от профиля {client_link(donor)} добавлен РФ-доступ 🎉\n"
            "РФ-приложения и сайты теперь будут работать со включённым VPN. Включён на всех "
            "твоих устройствах; выключить и добавить свои сайты — в разделе «🇷🇺 РФ-доступ»")


def routing_revoked_holder_notice(donor) -> str:
    return (f"🇷🇺 РФ-доступ для устройств от профиля {client_link(donor)} больше недоступен 😔\n"
            "Для доступа к ресурсам, которые ругаются на VPN, теперь придётся его выключать")


ROUTING_REVOKED_NOTICE = (
    "🇷🇺 РФ-доступ больше не входит в твою подписку 😔\n"
    "Для доступа к ресурсам, которые ругаются на VPN, теперь придётся его выключать"
)


ROUTING_LENT_OUT_NOTE = "Переданными устройствами управляют те, кому они переданы"

