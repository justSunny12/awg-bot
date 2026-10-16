"""card.py — карточка слота: сервисы, свои списки, канал, трафик."""

from __future__ import annotations

from awgbot.bot import ui
from awgbot.util import timeutil
from awgbot.domain import gwcfg
from awgbot.domain.services.gwchannel import drift_lines   # строки расхождения рисует домен
from awgbot.domain.notices.lanmap import lan_map_failed_line, lan_space_off_line

from ..fmt import _e, human_bytes, updown_brief, plain_ip, plural_ru, details
from ..updates import _ver

from .slots import _slot_note, _slot_ping_tail, agent_bot_line, lan_example, lan_pairs, slot_name, slot_status


# ── карточка слота ───────────────────────────────────────────────────────────

def services_line(svc: dict, agent_bot: dict | None = None, online: bool | None = None) -> str:
    """«🗂 SMB: свои — 1, извне — 2 · 🟢 доступны»; нулевая часть не выводится,
    обе нулевые — «🗂 SMB: не найдены»; что с ними на шлюзе — строкой под ней.
    online — канал конфигурации на связи (False — ожидание «когда выйдет на связь»)."""
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
        note = "⚠️ Для доступа к SMB перевыпусти конфигурацию шлюза"
    else:
        note = _slot_note(state, agent_bot, svc.get("error") or "", "Для доступа к SMB", "записи SMB",
                          offline=online is False)
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
    if not parts and state in ("offline", "pending"):
        return head                                # пусто — ждать нечего
    if state == "offline":
        note = "🟡 Уйдут на шлюз, когда он выйдет на связь"
    else:
        note = _slot_note(state, agent_bot, own.get("error") or "", "Для синхронизации", "списки")
    return head + "\n" + note


def _agent_at_least(version, floor: str) -> bool:
    """Версия агента из снимка не ниже floor; мусор или пусто — ложь."""
    from awgbot.domain import gwservices
    try:
        return bool(version) and gwservices.version_at_least(str(version), floor)
    except Exception:                                      # noqa: BLE001 — строка со шлюза
        return False


CHANNEL_HEAD = "🔧 Канал конфигурации"
NOT_APPLIED_TAIL = " — оставил прежнюю; подробности в чате бота шлюза"
UPLINK_ERR_PREFIX = "конфиг аплинка: "        # префикс проверки раздела — в строке карточки лишний
RESTORE_BTN = "«🚑 Восстановить»"             # кнопка восстановления в чате бота шлюза


def _snap_at(ch: dict) -> str:
    """«, по снимку от 15.09 12:00» — момент снимка абсолютным временем:
    сообщение само не перерисовывается, «12 мин назад» врало бы через час;
    возраст неизвестен — пусто."""
    age = ch.get("age")
    if age is None:
        return ""
    from datetime import timedelta
    return ", по снимку от " + timeutil.fmt_dt_ui(timeutil.now() - timedelta(seconds=max(0, int(age))))


def _cfg_lines(cfg: dict, ch: dict) -> list[str]:
    """Строки документа конфигурации у агента, который его принимает: отказ
    шлюза (К5), доставка в пути (К6), расхождение не на связи (К9 по разделам,
    с действием — доедет само, когда шлюз выйдет на связь), вопросы человеку
    (К14), не собранные документ и конфиг линка. «Актуальна» не пишется:
    только негативные исходы."""
    warns: list[str] = []
    due = list(cfg.get("due") or [])
    failed = cfg.get("failed") or {}
    if cfg.get("env_err"):
        warns.append(f"⚠️ Конфигурация для шлюза не собрана: {_e(cfg['env_err'])}")
    if cfg.get("link_err"):
        warns.append(f"⚠️ Конфиг линка для шлюза не собран: {_e(cfg['link_err'])}")
    if cfg.get("uplink_err"):
        err = str(cfg["uplink_err"])
        if err.startswith(UPLINK_ERR_PREFIX):
            err = err[len(UPLINK_ERR_PREFIX):]
        warns.append(f"⚠️ Новый конфиг аплинка не отправлен шлюзу: {_e(err)}")
    if failed:
        why = "; ".join(_e(v) for v in dict.fromkeys(failed.values())) or "ошибка"   # одна причина на env и link — один раз
        if "*" in failed:
            warns.append(f"⚠️ Шлюз отверг конфигурацию: {why} — ничего не менял")
        elif not {"env", "link"} & set(failed):
            what = ", ".join(_e(gwcfg.human(s)) for s in failed)
            warns.append(f"⚠️ Шлюз не применил {what}: {why}")
        else:
            warns.append(f"⚠️ Шлюз не применил конфигурацию: {why}{NOT_APPLIED_TAIL}")
    elif due:
        n = len(due)
        n_items = f"{n} " + plural_ru(n, "пункт", "пункта", "пунктов")
        if ch.get("online"):
            warns.append(f"🟡 Конфигурация на шлюзе расходится с выданной ({n_items}) — "
                         "изменения уходят по каналу конфигурации и применятся сами")
        else:
            items = "\n".join(f"   • {_e(gwcfg.human(s))}" for s in due)
            warns.append(f"⚠️ Конфигурация на шлюзе расходится с выданной ({n_items}{_snap_at(ch)}):\n{items}\n"
                         "└ изменения уйдут по каналу конфигурации, когда шлюз выйдет на связь")
    if "uplink" in (cfg.get("wait") or []):
        fp = f" · отпечаток <code>{_e(cfg['uplink_fp'])}</code>" if cfg.get("uplink_fp") else ""
        warns.append(f"⏳ Конфиг аплинка ждёт подтверждения в чате бота шлюза{fp}")
    elif "uplink" in (cfg.get("declined") or []):
        warns.append("⚠️ Шлюз отклонил новый конфиг аплинка — аплинк прежний")
    wait_text = {"mail": "⏳ Почта ждёт решения в чате бота шлюза: на шлюзе свой ящик",
                 "backup": "⏳ Ключевая фраза бэкапов ждёт решения в чате бота шлюза: на шлюзе своя"}
    warns += [wait_text[s] for s in cfg.get("wait") or [] if s in wait_text]
    return warns


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
        out.append("почта и ключевая фраза бэкапов на шлюзе свои — так решено в чате бота шлюза")
    elif "mail" in declined:
        out.append("почта на шлюзе своя — так решено в чате бота шлюза")
    elif "backup" in declined:
        out.append("ключевая фраза бэкапов на шлюзе своя — так решено в чате бота шлюза")
    return out


def channel_lines(ch: dict | None, server_ok) -> tuple[str, list[str], str]:
    """Канал в карточке: (строка «🔧 Канал конфигурации …», предупреждения
    открытыми строками, «выход шлюза в интернет» для «подробнее»). Строка
    канала — только связь и версия агента: «конфигурация актуальна» не
    пишется, расхождение и отказы — ⚠️/⏳ строками с действием. Всё, что
    пришло со шлюза, — недоверенные данные: только через _e."""
    if not ch:
        return "", [], ""
    if not ch.get("ever"):
        # канал обязателен: перевыпуск тут не лечит. Линк жив — канал не держит агент
        # (не запущен или прежней версии: тем канал включала свежая конфигурация)
        if ch.get("link_fresh"):
            return (f"{CHANNEL_HEAD} 🔴 ещё не подключался — линк есть, агент шлюза не отвечает: "
                    "запусти или обнови его", [], "")
        return (f"{CHANNEL_HEAD} 🔴 ещё не подключался — линк не поднят или агент шлюза не запущен", [], "")
    if ch.get("online"):
        head = f"{CHANNEL_HEAD} 🟢"
    else:
        seen = ch.get("seen") or ""
        when = ""
        if seen:
            try:
                when = " с " + timeutil.fmt_dt_ui(timeutil.parse_iso(seen))
            except ValueError:
                when = ""
        head = f"{CHANNEL_HEAD} 🔴 нет связи{when}"
    warns: list[str] = []
    egress = ""
    if ch.get("has_snap"):
        agent = _e(str(ch.get("agent") or "?")[:32])          # строка со шлюза — с пределом
        head += f" · {_ver(agent)}"
        gen, mine = ch.get("awg_gen"), ch.get("awg_gen_mine")
        if gen is not None and mine is not None and gen != mine:
            # поколение — класс совместимости протокола AWG: линк между сторонами
            # разных поколений не поднимется, пока отстающая не обновится
            warns.append(f"⚠️ Поколение AWG на шлюзе {gen}, у сервера {mine} — линк работает только на одном "
                         "поколении: обнови отстающую сторону — агента шлюза или сервера AWG")
        drift = ch.get("drift") or []
        cfg = ch.get("cfg") or {}
        if cfg.get("cfg1"):
            warns += _cfg_lines(cfg, ch)
        elif not ch.get("has_bundle"):
            if ch.get("online"):                 # канал лежит — второй 🔴 в строке ничего не добавит
                head += " · 🔴 шлюз не сообщает свою конфигурацию"
        elif drift:
            ack = ch.get("ack") or {}
            n = len(drift)
            n_items = f"{n} " + plural_ru(n, "пункт", "пункта", "пунктов")
            if ch.get("online") and ack and not ack.get("ok") and ch.get("ack_current"):
                warns.append("⚠️ Шлюз не применил конфигурацию: "
                             f"{_e(ack.get('error') or 'ошибка')}{NOT_APPLIED_TAIL}")
            elif ch.get("online") and ch.get("drift_bundle") and not ch.get("drift_channel"):
                warns.append("⚠️ Локальные подсети других шлюзов неактуальны — перевыпусти "
                             "конфигурацию шлюза")
            elif ch.get("online") and ch.get("drift_bundle"):
                warns.append(f"⚠️ Конфигурация на шлюзе расходится с выданной ({n_items}): остальное уйдёт "
                             "по каналу конфигурации само, а подсети других шлюзов — только с перевыпуском: "
                             "перевыпусти конфигурацию шлюза")
            elif ch.get("online"):
                warns.append(f"🟡 Конфигурация на шлюзе расходится с выданной ({n_items}) — "
                             "изменения уходят по каналу конфигурации и применятся сами")
            else:
                raw = ch.get("drift_items") or []
                # устройства админа — одним именем: адреса всех устройств в карточке не нужны;
                # разница по устройствам — в уведомлении, когда шлюз на связи отверг набор
                shown = [_e(it[1]) if it[0] == "ADMIN_IPS" else line
                         for it, line in zip(raw, drift_lines(raw, html=True))]
                items = "\n".join(f"   • {d}" for d in shown)
                # агент без документа получает изменения только файлом
                warns.append(f"⚠️ Конфигурация на шлюзе расходится с выданной ({n_items}{_snap_at(ch)}):\n{items}\n"
                             "└ перевыпусти конфигурацию шлюза — «📤 Конфигурация»")
        gen_hint = {"old": "⚠️ Обвязка шлюза старого образца — перевыпусти файл конфигурации",
                    "none": "⚠️ Обвязка шлюза не развёрнута — перевыпусти файл конфигурации"}
        if cfg.get("cfg1"):
            # обвязку освежает обновление агента, значения приезжают каналом: «новый файл» не лечит
            gen_hint = {"old": f"⚠️ Обвязка шлюза старого образца — {RESTORE_BTN} в чате бота шлюза",
                        "none": f"⚠️ Обвязка шлюза не развёрнута — {RESTORE_BTN} в чате бота шлюза; "
                                "не поможет — причина в «🩺 Здоровье» там же"}
        if ch.get("has_bundle") and ch.get("plumbing_gen") in gen_hint:
            warns.append(gen_hint[ch["plumbing_gen"]])
        # «проверка с сервера» — зонд живости сервера через шлюз (link_ok);
        # «проверка шлюза» — его собственная проверка выхода через провайдера (egress_ok)
        gw_ok = ch.get("egress_gw")
        mark = {True: "работает", False: "не работает", None: "нет данных"}
        egress = (f"выход шлюза в интернет: проверка с сервера — {mark[bool(server_ok)]}, "
                  f"проверка шлюза — {mark[gw_ok if isinstance(gw_ok, bool) else None]}"
                  + ("" if ch.get("online") or not _snap_at(ch) else f" ({_snap_at(ch)[2:]})"))
    skew = ch.get("clock_skew")
    if isinstance(skew, int) and abs(skew) >= 120:
        side = "спешат" if skew > 0 else "отстают"
        warns.append(f"⚠️ Часы шлюза {side} на {abs(skew) // 60} мин — проверь на нём синхронизацию "
                     "времени: от неё зависят TLS и расписания")
    pn = ch.get("peer_nets") or {}
    if isinstance(pn, dict) and pn and not pn.get("ok"):
        miss = ", ".join(f"<code>{_e(str(n)[:18])}</code>" for n in (pn.get("missing") or [])[:8]) or "подсетей"
        if (ch.get("cfg") or {}).get("cfg1"):
            warns.append(f"⚠️ Связь подсетей: на шлюзе нет {miss} — сервер повторит доставку; "
                         f"не поможет — {RESTORE_BTN} в чате бота шлюза")
        else:
            warns.append(f"⚠️ Связь подсетей: на шлюзе нет {miss} — перевыпусти конфигурацию шлюза")
    lists = ch.get("lists") or {}
    if lists and not lists.get("ok"):
        warns.append(f"⚠️ Шлюз не принял списки доменов: {_e(lists.get('error') or 'ошибка')}"
                     " — пока качает их сам")
    err = ch.get("error") or ""
    if err and not ch.get("online"):
        warns.append(f"Последний обрыв канала конфигурации: {_e(err)}")
    return head, warns, egress


def _gw_traffic(dev) -> str:
    rx, tx = int(dev.traffic_rx_month), int(dev.traffic_tx_month)
    if rx + tx <= 0:
        return ""
    # стрелки — со стороны шлюза: его исходящее — это tx сервера
    return f" · 📊 {human_bytes(rx + tx)} {updown_brief(tx, rx)}"


PEER_LINKS_SHOWN = 3         # соседей по имени в строке «Связь подсетей» карточки, дальше «и ещё N»
LAN_ISSUES_SHOWN = 3         # строк ⚠️ о подсетях вне таблицы в карточке, дальше «и ещё N»


def peer_links_line(links: list[str]) -> str:
    """«🔗 Связь подсетей: с NASPi, Pi2» — соседи с VPN-транзитом, имена уже
    ссылками на их карточки (HTML); больше трёх — «и ещё N»."""
    more = len(links) - PEER_LINKS_SHOWN
    return ("🔗 Связь подсетей: с " + ", ".join(links[:PEER_LINKS_SHOWN])
            + (f" и ещё {more}" if more > 0 else ""))


def lan_space_lines(info: dict | None) -> list[str]:
    """Карточка слота: адреса в туннеле выключены целиком (пространство или
    метка), таблица не поставилась, или подсети слота, не попавшие в таблицу,
    — данные до 3.3.0, которые ввод теперь не примет."""
    info = info or {}
    if info.get("problem"):
        return [lan_space_off_line(info["problem"], info.get("key") or "routing.lan_space")]
    out = [lan_map_failed_line(info["error"])] if info.get("error") else []
    issues = info.get("issues") or []
    for it in issues[:LAN_ISSUES_SHOWN]:
        net = f"<code>{_e(it.get('net', ''))}</code>"
        # подсеть накрывает префикс соседа в туннеле: на шлюзе она пересеклась
        # бы с его AllowedIPs — конфиг линка не соберётся
        link = ", и конфиг линка не соберётся (пересекается с адресами в туннеле другого шлюза)" \
            if it.get("blocks_link") else ""
        if it.get("kind") == "space":
            out.append(f"⚠️ {net} — в служебном пространстве сервера AWG, в туннеле не видна{link}: смени подсеть "
                       "в «🗺 Подсети»")
        elif it.get("kind") == "clash":
            out.append(f"⚠️ {net} в туннеле (<code>{_e(it.get('twin', ''))}</code>) пересекается с "
                       f"<code>{_e(it.get('other', ''))}</code> (<code>{_e(it.get('other_twin', ''))}</code>) "
                       "и не видна: убери одну из них в «🗺 Подсети»")
        elif it.get("kind") == "wide":
            out.append(f"⚠️ {net} шире /16, в туннеле не видна{link}: сузь подсеть в «🗺 Подсети»")
    if len(issues) > LAN_ISSUES_SHOWN:
        n = len(issues) - LAN_ISSUES_SHOWN
        out.append(f"⚠️ Ещё {n} " + plural_ru(n, "подсеть", "подсети", "подсетей") + " вне адресов в туннеле: "
                   "проверь их в «🗺 Подсети»")
    return out


def gateway_card_text(state: dict, states: list) -> str:
    """Карточка слота: заголовок со значком («⭐» у предпочтительного, «🛰» у
    остальных), линк, адрес и трафик, канал конфигурации, подсети и VPN-транзит,
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
    ch = state.get("channel") or {}
    cfg = ch.get("cfg") or {}
    if ch.get("has_snap") and not cfg.get("cfg1") and not _agent_at_least(ch.get("agent"), "3.3.0"):
        # предупреждение — рядом с каналом, о котором оно; версия — настоящая, из снимка;
        # ссылка на бота — строкой «Бот шлюза» внизу карточки, второй раз не нужна
        ver = _ver(_e(str(ch.get("agent") or "?")[:32]))
        lines.append(f"⚠️ Агент {ver} устарел: подсети других шлюзов, ключи линка и конфиг аплинка доходят до "
                     "него только с перевыпуском конфигурации, не по каналу. Обнови агента")
    rk = state.get("rekey") or {}
    if rk.get("state") in ("staged", "asked"):
        fp = f" · отпечаток <code>{_e(rk['fp'])}</code>" if rk.get("fp") else ""
        lines.append(f"⏳ Ключи линка ждут подтверждения в чате бота шлюза{fp}")
    elif rk.get("state") == "switched":
        lines.append("🟡 Ключи линка: переключение…")
    elif rk.get("state") == "declined":
        lines.append("⚠️ Шлюз отклонил новые ключи линка — линк работает на прежних; сменить снова — "
                     "«⚙️ Настройки» → «🔧 Сервис» → «🔑 Ключи линка»")
    lines += ch_warns
    nets = gw.lan_subnets
    nets_s = (lan_pairs(nets, state.get("lan_twins")) if nets
              else "Подсети не заданы — без них сеть шлюза из туннеля не открыть")
    lines.append(f"🗺 {nets_s} · 🔀 VPN-транзит " + ("✅" if gw.lan_mode else "☑️"))
    lines += lan_space_lines(state.get("lan_space"))
    if gw.lan_mode:
        own = state.get("own_lists") or {}
        if own and own.get("state") != "off":
            lines.append(own_lists_line(own, state.get("agent_bot")))
    svc = state.get("services") or {}
    if state.get("peer_nets"):
        lines.append(peer_links_line(state.get("peer_links") or []))
        if svc:
            lines.append(services_line(svc, state.get("agent_bot"), ch.get("online") if ch else None))
    elif svc.get("tunnel") and svc.get("own"):
        # без связи подсетей «извне» не бывает — только свои и «в туннеле»
        lines.append(services_line({**svc, "peer": 0}, state.get("agent_bot")))
    agent = agent_bot_line(state)
    if agent:
        lines.append(agent)
    # «подробнее» — построчно, «;» в конце: сначала адреса в туннеле (ответ на
    # «как зайти в сеть»), затем выход в интернет, служебное — последним
    more = []
    if nets:
        example = lan_example(state.get("lan_twins"))
        if example:
            # «из подсетей других шлюзов» — только при живой связи подсетей
            who = "через туннель и из подсетей других шлюзов" if state.get("peer_nets") else "через туннель"
            more.append(f"{who} сеть открывается по адресам в туннеле: {example} — два последних октета те же")
    if egress:
        more.append(egress)
    more += cfg_more_lines(cfg)
    issued = state.get("issued_at") or ""
    if issued:
        try:
            more.append(f"конфигурация выпущена {timeutil.fmt_dt_ui(timeutil.parse_iso(issued))}")
        except ValueError:
            pass
    if more:
        lines.append(details(";\n".join(more)))
    return "\n".join(lines)


def gateway_edit_text(state: dict) -> str:
    return ui.head(f"✏️ {slot_name(state)}") + " — изменить:"
