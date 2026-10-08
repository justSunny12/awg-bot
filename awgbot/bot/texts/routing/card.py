"""card.py — карточка слота: сервисы, свои списки, канал, трафик."""

from __future__ import annotations

from awgbot.util import timeutil
from awgbot.util import nets as nets_util
from awgbot.domain.services.gwchannel import drift_lines   # строки расхождения рисует домен

from ..fmt import _e, human_bytes, updown_brief, plain_ip, plural_ru, details
from ..updates import _ver

from .slots import _slot_note, _slot_ping_tail, agent_bot_line, slot_name, slot_ref, slot_status


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
    name = _e(dev.name) if dev is not None else f"слот {gw.id}"     # без кавычек, как в уведомлениях
    two = len(states) > 1
    lines = [f"{'⭐' if state.get('preferred') and two else '🛰'} <b>{slot_name(state)}</b> — {slot_status(state)}"]
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
    lines += ch_warns
    nets = gw.home_subnets
    nets_s = ", ".join(f"<code>{_e(n)}</code>" for n in nets[:6]) + (f" и ещё {len(nets) - 6}" if len(nets) > 6 else "") \
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
    conflict = next((s for s in others if nets_util.overlap(nets, s["gateway"].home_subnets)), None)
    if conflict is not None:
        ov_nets = nets_util.overlap(nets, conflict["gateway"].home_subnets)
        ov = ", ".join(f"<code>{_e(n)}</code>" for n in ov_nets)
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
    return f"✏️ <b>{slot_name(state)}</b> — изменить:"
