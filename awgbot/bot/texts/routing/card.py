"""card.py — карточка слота: сервисы, свои списки, канал, трафик."""

from __future__ import annotations

from awgbot.bot import ui
from awgbot.util import timeutil
from awgbot.util import nets as nets_util
from awgbot.domain import gwcfg
from awgbot.domain.services.gwchannel import drift_lines   # строки расхождения рисует домен

from ..fmt import _e, human_bytes, updown_brief, plain_ip, plural_ru, details
from ..updates import _ver

from .slots import _bot_link, _slot_note, _slot_ping_tail, agent_bot_line, slot_name, slot_ref, slot_status


# ── карточка слота ───────────────────────────────────────────────────────────

def services_line(svc: dict, agent_bot: dict | None = None) -> str:
    """«🗂 SMB: свои — 1, извне — 2 · 🟢 доступны»; нулевая часть не выводится,
    обе нулевые — «🗂 SMB: не найдены»; что с ними на шлюзе — строкой под ней."""
    own, peer = int(svc.get("own") or 0), int(svc.get("peer") or 0)
    if not own and not peer:
        return "🗂 SMB: не найдены"
    parts = ([f"свои — {own}"] if own else []) + ([f"извне — {peer}"] if peer else [])
    head = "🗂 SMB: " + ", ".join(parts)
    tunnel = " · в туннеле ✅" if svc.get("tunnel") and own else ""   # «SMB в туннеле»: свои видны устройствам админа
    if not peer:
        return head + tunnel
    state = svc.get("state", "")
    if state == "applied":
        return head + " · 🟢 доступны" + tunnel
    head += tunnel
    if state == "reissue":
        note = "⚠️ Необходим перевыпуск конфигурации шлюза"
    else:
        note = _slot_note(state, agent_bot, svc.get("error") or "", "Необходимо", "записи")
    return head + "\n" + note


def own_lists_line(own: dict, agent_bot: dict | None = None) -> str:
    """«📋 Свои списки: 5 в туннель, 1 напрямую» / «пусто»; что с ними на
    шлюзе — строкой сразу под ней, только если не «применены»."""
    vpn, ru = int(own.get("vpn") or 0), int(own.get("ru") or 0)
    parts = ([f"{vpn} в туннель"] if vpn else []) + ([f"{ru} напрямую"] if ru else [])
    head = "📋 Свои списки: " + (", ".join(parts) if parts else "пусто")   # нулевая часть не выводится
    state = own.get("state", "")
    if state == "applied":
        return head
    if state == "offline":
        note = "⏳ Синхронизируется, когда шлюз выйдет на связь"
    else:
        note = _slot_note(state, agent_bot, own.get("error") or "", "Для синхронизации необходимо", "списки")
    return head + "\n" + note


def _agent_at_least(version, floor: str) -> bool:
    """Версия агента из снимка не ниже floor; мусор или пусто — ложь."""
    from awgbot.domain import gwservices
    try:
        return bool(version) and gwservices.version_at_least(str(version), floor)
    except Exception:                                      # noqa: BLE001 — строка со шлюза
        return False


def _cfg_lines(head: str, cfg: dict, ch: dict, stale: str) -> tuple[str, list[str]]:
    """Строки документа конфигурации у агента, который его принимает: отказ
    шлюза (К5), доставка в пути (К6), расхождение не на связи (К9 по разделам),
    вопросы человеку (К14), не собранный конфиг линка."""
    warns: list[str] = []
    due = list(cfg.get("due") or [])
    failed = cfg.get("failed") or {}
    if cfg.get("link_err"):
        warns.append(f"⚠️ Конфиг линка для шлюза не собран: {_e(cfg['link_err'])}")
    if cfg.get("uplink_err"):
        warns.append(f"⚠️ Конфиг аплинка для шлюза не едет: {_e(cfg['uplink_err'])}")
    if failed:
        why = "; ".join(_e(v) for v in dict.fromkeys(failed.values())) or "ошибка"   # одна причина на env и link — один раз
        if "*" in failed:
            warns.append(f"⚠️ Шлюз отверг конфигурацию: {why} — ничего не менял")
        elif not {"env", "link"} & set(failed):
            what = ", ".join(_e(gwcfg.human(s)) for s in failed)
            warns.append(f"⚠️ Шлюз не применил {what}: {why}")
        else:
            warns.append(f"⚠️ Шлюз не применил конфигурацию: {why} — вернул прежнюю")
    elif due:
        n = len(due)
        n_items = f"{n} " + plural_ru(n, "пункт", "пункта", "пунктов")
        if ch.get("online"):
            warns.append(f"⏳ Конфигурация на шлюзе расходится с выданной ({n_items}) — "
                         "изменения уходят каналом и применятся сами")
        else:
            items = "\n".join(f"   • {_e(gwcfg.human(s))}" for s in due)
            warns.append(f"⚠️ Конфигурация на шлюзе расходится с выданной ({n_items}, по снимку "
                         f"{timeutil.age_ago(ch.get('age'))}):\n{items}")
    else:
        head += f" · 🟢 конфиг актуален{stale}"
    if "uplink" in (cfg.get("wait") or []):
        fp = f" · <code>{_e(cfg['uplink_fp'])}</code>" if cfg.get("uplink_fp") else ""
        warns.append(f"🔐 Конфиг аплинка ждёт подтверждения в чате бота шлюза{fp}")
    elif "uplink" in (cfg.get("declined") or []):
        warns.append("⚠️ Шлюз отклонил новый конфиг аплинка — аплинк прежний")
    wait_text = {"mail": "⏳ Почта ждёт решения в чате бота шлюза: на шлюзе свой ящик",
                 "backup": "⏳ Фраза бэкапов ждёт решения в чате бота шлюза: на шлюзе своя"}
    warns += [wait_text[s] for s in cfg.get("wait") or [] if s in wait_text]
    return head, warns


def cfg_more_lines(cfg: dict) -> list[str]:
    """«подробнее»: поколение и дата применения (К13), что оставлено своим."""
    out = []
    if cfg.get("gen") and cfg.get("applied_at"):
        try:
            out.append(f"конфигурация №{int(cfg['gen'])} применена {timeutil.fmt_dt_ui(timeutil.parse_iso(cfg['applied_at']))}")
        except ValueError:
            pass
    declined = set(cfg.get("declined") or [])
    if {"mail", "backup"} <= declined:
        out.append("почта и фраза бэкапов на шлюзе свои — оставлены в чате бота шлюза")
    elif "mail" in declined:
        out.append("почта на шлюзе своя — оставлена в чате бота шлюза")
    elif "backup" in declined:
        out.append("фраза бэкапов на шлюзе своя — оставлена в чате бота шлюза")
    return out


def channel_lines(ch: dict | None, server_ok) -> tuple[str, list[str], str]:
    """Канал в карточке: (строка «🔗 Упр. канал …», предупреждения открытыми
    строками, «выход наружу» для «подробнее»). Всё, что пришло со шлюза, —
    недоверенные данные: только через _e."""
    if not ch:
        return "", [], ""
    if not ch.get("ever"):
        # канал обязателен: перевыпуск тут не лечит. Линк жив — канал не держит агент
        # (не запущен или прежней версии: тем канал включала свежая конфигурация)
        if ch.get("link_fresh"):
            return ("🔗 Упр. канал: ещё не поднимался — линк поднят, канал не держит агент шлюза: "
                    "не запущен или прежней версии, обнови его", [], "")
        return ("🔗 Упр. канал: ещё не поднимался — линк не поднят или агент шлюза не запущен", [], "")
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
        cfg = ch.get("cfg") or {}
        if cfg.get("cfg1"):
            head, cfg_warns = _cfg_lines(head, cfg, ch, stale)
            warns += cfg_warns
        elif not ch.get("has_bundle"):
            head += " · ⚙️ конфиг: шлюз ещё не сообщал"
        elif drift:
            ack = ch.get("ack") or {}
            n = len(drift)
            n_items = f"{n} " + plural_ru(n, "пункт", "пункта", "пунктов")
            if ch.get("online") and ack and not ack.get("ok") and ch.get("ack_current"):
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
                raw = ch.get("drift_items") or []
                # устройства админа — одним именем: адреса всех устройств в карточке не нужны;
                # разница по устройствам — в уведомлении, когда шлюз на связи отверг набор
                shown = [_e(it[1]) if it[0] == "ADMIN_IPS" else line
                         for it, line in zip(raw, drift_lines(raw, html=True))]
                items = "\n".join(f"   • {d}" for d in shown)
                warns.append(f"⚠️ Конфигурация на шлюзе расходится с выданной ({n_items}, по снимку "
                             f"{timeutil.age_ago(ch.get('age'))}):\n{items}")
        else:
            head += f" · 🟢 конфиг актуален{stale}"
        gen_hint = {"old": "⚠️ Обвязка шлюза старого образца — перевыпусти файл конфигурации",
                    "none": "⚠️ Обвязка шлюза не развёрнута — перевыпусти файл конфигурации"}
        if cfg.get("cfg1"):
            # обвязку освежает обновление агента, значения приезжают каналом: «новый файл» не лечит
            gen_hint = {"old": "⚠️ Обвязка шлюза старого образца — «🔧 Восстановить» в чате бота шлюза",
                        "none": "⚠️ Обвязка шлюза не развёрнута — «🔧 Восстановить» в чате бота шлюза; "
                                "не поможет — файл первого применения"}
        if ch.get("has_bundle") and ch.get("plumbing_gen") in gen_hint:
            warns.append(gen_hint[ch["plumbing_gen"]])
        gw_ok = ch.get("egress_gw")
        mark = {True: "есть", False: "нет", None: "не знает"}
        egress = (f"выход наружу: сервер — {mark[bool(server_ok)]}, "
                  f"шлюз — {mark[gw_ok if isinstance(gw_ok, bool) else None]}"
                  + ("" if ch.get("online") else f" (по снимку {timeutil.age_ago(ch.get('age'))})"))
    skew = ch.get("clock_skew")
    if isinstance(skew, int) and abs(skew) >= 120:
        side = "спешат" if skew > 0 else "отстают"
        warns.append(f"⏱ Часы шлюза {side} на {abs(skew) // 60} мин — проверь синхронизацию времени "
                     "(TLS и расписания от неё зависят)")
    pn = ch.get("peer_nets") or {}
    if isinstance(pn, dict) and pn and not pn.get("ok"):
        miss = ", ".join(f"<code>{_e(str(n)[:18])}</code>" for n in (pn.get("missing") or [])[:8]) or "подсетей"
        if (ch.get("cfg") or {}).get("cfg1"):
            warns.append(f"⚠️ Связь подсетей: на шлюзе нет {miss} — сервер повторит доставку; "
                         "не поможет — «🔧 Восстановить» в чате бота шлюза")
        else:
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
    two = len(states) > 1
    lines = [ui.head(f"{'⭐' if state.get('preferred') and two else '🛰'} {slot_name(state)}") + f" — {slot_status(state)}"]
    age = state.get("handshake_age")
    if age is None or age > 300:
        hs = "хендшейка нет"
    else:
        hs = "хендшейк " + (f"{int(age)} с" if age < 60 else f"{int(age) // 60} мин")
    lines.append(f"📡 <code>{_e(gw.link_if)}:{gw.link_port}</code> · {hs}{_slot_ping_tail(state)}")
    ip = state.get("ext_ip")
    lines.append(("🌐 " + (plain_ip(ip) if ip else "адрес не определён"))
                 + (_gw_traffic(dev) if dev is not None else ""))
    if state.get("active") and state.get("unavailable"):
        lines.append("🔴 РФ-доступ недоступен: российские сервисы открываются с зарубежного адреса")
    ch_head, ch_warns, egress = channel_lines(state.get("channel"), state.get("link_ok"))
    if ch_head:
        lines.append(ch_head)
    rk = state.get("rekey") or {}
    if rk.get("state") in ("staged", "asked"):
        fp = f" · <code>{_e(rk['fp'])}</code>" if rk.get("fp") else ""
        lines.append(f"🔑 Ключи линка ждут подтверждения в чате бота шлюза{fp}")
    elif rk.get("state") == "switched":
        lines.append("🔑 Ключи линка: переключение…")
    elif rk.get("state") == "declined":
        lines.append("⚠️ Шлюз отклонил новые ключи линка — линк на прежних")
    lines += ch_warns
    nets = gw.lan_subnets
    nets_s = ", ".join(f"<code>{_e(n)}</code>" for n in nets[:6]) + (f" и ещё {len(nets) - 6}" if len(nets) > 6 else "") \
        if nets else "подсети не заданы"
    lines.append(f"🗺 {nets_s} · 🔀 VPN-транзит " + ("✅" if gw.lan_mode else "☑️"))
    if gw.lan_mode:
        own = state.get("own_lists") or {}
        if own and own.get("state") != "off":
            lines.append(own_lists_line(own, state.get("agent_bot")))
    svc = state.get("services") or {}
    if state.get("peer_nets"):
        lines.append("↔️ Связь подсетей ✅")
        if svc:
            lines.append(services_line(svc, state.get("agent_bot")))
    elif svc.get("tunnel") and svc.get("own"):
        # без связи подсетей «извне» не бывает — только свои и «в туннеле»
        lines.append(services_line({**svc, "peer": 0}, state.get("agent_bot")))
    others = [s for s in states if s["gateway"].id != gw.id]
    conflict = next((s for s in others if nets_util.overlap(nets, s["gateway"].lan_subnets)), None)
    if conflict is not None:
        ov_nets = nets_util.overlap(nets, conflict["gateway"].lan_subnets)
        ov = ", ".join(f"<code>{_e(n)}</code>" for n in ov_nets)
        verb = "пересекается" if len(ov_nets) == 1 else "пересекаются"
        first = min([state] + others, key=lambda s: (0 if s.get("preferred") else 1, s["gateway"].id))
        route = "маршрут достаётся ему, " if first["gateway"].id != gw.id else ""
        lines.append(f"⚠️ {ov} {verb} с подсетью «{slot_ref(conflict)}»: {route}связь подсетей между "
                     "этими шлюзами не работает, пока они пересекаются")
    ch = state.get("channel") or {}
    cfg = ch.get("cfg") or {}
    if ch.get("has_snap") and not cfg.get("cfg1") and not _agent_at_least(ch.get("agent"), "3.3.0"):
        bot = _bot_link(state.get("agent_bot"))
        lines.append("⚠️ Агент шлюза 3.2 получает каналом не всё — подсети других шлюзов, конфиг линка и "
                     "аплинка едут только файлом. Обнови агента" + (f" (бот: {bot})" if bot else ""))
    agent = agent_bot_line(state)
    if agent:
        lines.append(agent)
    more = []
    if egress:
        more.append(egress)
    more += cfg_more_lines(cfg)
    issued = state.get("issued_at") or ""
    if issued:
        try:
            when = timeutil.fmt_dt_ui(timeutil.parse_iso(issued))
            more.append(f"файл первого применения выпущен {when}" if cfg.get("cfg1") else f"конфигурация выпущена {when}")
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
    return ui.head(f"✏️ {slot_name(state)}") + " — изменить:"
