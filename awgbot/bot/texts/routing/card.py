"""card.py — карточка слота: сервисы, свои списки, канал, трафик."""

from __future__ import annotations

from awgbot.bot import ui
from awgbot.util import timeutil
from awgbot.domain import gwcfg
from awgbot.domain.notices.backup import chat_ref
from awgbot.domain.notices.lanmap import lan_map_failed_line, lan_space_off_line

from awgbot.util import listlink
from awgbot.util.textfmt import deep_link, more_link

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


CHANNEL_HEAD = "🔧 Канал конфигурации"
NOT_APPLIED_TAIL = " — оставил прежнюю; подробности в чате бота шлюза"
UPLINK_ERR_PREFIX = "конфиг аплинка: "        # префикс проверки раздела — в строке карточки лишний
RESTORE_BTN = "«🚑 Восстановить»"             # кнопка восстановления в чате бота шлюза


def _snap_at(ch: dict) -> str:
    """«, по снимку от 15.09 12:00 (UTC+5)» — момент снимка абсолютным временем:
    сообщение само не перерисовывается, «12 мин назад» врало бы через час;
    возраст неизвестен — пусто."""
    age = ch.get("age")
    if age is None:
        return ""
    return ", по снимку от " + timeutil.fmt_dt_ui(timeutil.shift(timeutil.now(), -max(0, int(age))))


def _cfg_lines(cfg: dict, ch: dict) -> list[str]:
    """Строки документа конфигурации: отказ шлюза (К5), доставка в пути (К6), расхождение не на связи (К9 по разделам,
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
            # без скобок вокруг: у момента снимка своя метка пояса в скобках
            warns.append(f"⚠️ Конфигурация на шлюзе расходится с выданной — {n_items}{_snap_at(ch)}:\n{items}\n"
                         "└ изменения уйдут по каналу конфигурации, когда шлюз выйдет на связь")
    if "uplink" in (cfg.get("wait") or []):
        fp = f" · отпечаток <code>{_e(cfg['uplink_fp'])}</code>" if cfg.get("uplink_fp") else ""
        warns.append(f"⏳ Конфиг аплинка ждёт подтверждения в чате бота шлюза{fp}")
    elif "uplink" in (cfg.get("declined") or []):
        warns.append("⚠️ Шлюз отклонил новый конфиг аплинка — аплинк прежний")
    if "mail" in (cfg.get("wait") or []):
        warns.append("⏳ Почта ждёт решения в чате бота шлюза: на шлюзе свой ящик")
    return warns


def cfg_more_lines(cfg: dict) -> list[str]:
    """«подробнее»: поколение и дата применения (К13), что оставлено своим."""
    out = []
    if cfg.get("gen") and cfg.get("applied_at"):
        try:
            out.append(f"конфигурация №{int(cfg['gen'])} применена {timeutil.fmt_dt_ui(timeutil.parse_iso(cfg['applied_at']))}")
        except ValueError:
            pass
    if "mail" in set(cfg.get("declined") or []):
        out.append("почта на шлюзе своя — так решено в чате бота шлюза")
    return out


def channel_lines(ch: dict | None, server_ok, bot: str = "", slot: int = 0) -> tuple[str, list[str], str]:
    """Канал в карточке: (строка «🔧 Канал конфигурации …», предупреждения
    открытыми строками, «выход шлюза в интернет» для «подробнее»). Строка
    канала — только связь и версия агента: «конфигурация актуальна» не
    пишется, расхождение и отказы — ⚠️/⏳ строками с действием. Всё, что
    пришло со шлюза, — недоверенные данные: только через _e."""
    if not ch:
        return "", [], ""
    if not ch.get("ever"):
        # канал обязателен. Линк жив — канал не держит агент (не запущен или упал)
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
        if not ch.get("outdated"):
            warns += _cfg_lines(ch.get("cfg") or {}, ch)
        # «проверка с сервера» — зонд живости сервера через шлюз (link_ok);
        # «проверка шлюза» — его собственная проверка выхода через провайдера (egress_ok)
        gw_ok = ch.get("egress_gw")
        mark = {True: "работает", False: "не работает", None: "нет данных"}
        snap = "" if ch.get("online") else _snap_at(ch)[1:]      # « по снимку от … (UTC+5)»
        egress = (f"выход шлюза в интернет{snap}: проверка с сервера — {mark[bool(server_ok)]}, "
                  f"проверка шлюза — {mark[gw_ok if isinstance(gw_ok, bool) else None]}")
    skew = ch.get("clock_skew")
    if isinstance(skew, int) and abs(skew) >= 120:
        side = "спешат" if skew > 0 else "отстают"
        warns.append(f"⚠️ Часы шлюза {side} на {abs(skew) // 60} мин — проверь на нём синхронизацию "
                     "времени: от неё зависят TLS и расписания")
    pn = ch.get("peer_nets") or {}
    if isinstance(pn, dict) and pn and not pn.get("ok"):
        missing = list(pn.get("missing") or [])
        miss = ", ".join(f"<code>{_e(str(n)[:18])}</code>" for n in missing[:8]) or "подсетей"
        if len(missing) > 8:                              # весь список — на экране связи подсетей слота
            miss += more_link(len(missing) - 8, bot, listlink.build("gwpeers", slot))
        warns.append(f"⚠️ Связь подсетей: на шлюзе нет {miss} — сервер повторит доставку; "
                     f"не поможет — {RESTORE_BTN} в чате бота шлюза")
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


def peer_links_line(links: list[str], bot: str = "", slot: int = 0) -> str:
    """«🔗 Связь подсетей: с NASPi, Pi2» — соседи с VPN-транзитом, имена уже
    ссылками на их карточки (HTML); больше трёх — «и ещё N» ссылкой на экран
    связи подсетей слота (без слота и имени бота — текстом)."""
    more = len(links) - PEER_LINKS_SHOWN
    tail = ""
    if more > 0:
        tail = more_link(more, bot, listlink.build("gwpeers", slot)) if slot else f" и ещё {more}"
    return "🔗 Связь подсетей: с " + ", ".join(links[:PEER_LINKS_SHOWN]) + tail


def lan_space_lines(info: dict | None, bot: str = "", slot: int = 0) -> list[str]:
    """Карточка слота: адреса в туннеле выключены целиком (пространство или
    метка), таблица не поставилась, или подсети слота, не попавшие в таблицу,
    — данные до 3.3.0, которые ввод теперь не примет."""
    info = info or {}
    if info.get("problem"):
        return [lan_space_off_line(info["problem"], info.get("key") or "routing.lan_space")]
    out = [lan_map_failed_line(info["error"])] if info.get("error") else []
    issues = info.get("issues") or []
    out += [x for x in (lan_issue_line(it) for it in issues[:LAN_ISSUES_SHOWN]) if x]
    if len(issues) > LAN_ISSUES_SHOWN:
        n = len(issues) - LAN_ISSUES_SHOWN
        whole = deep_link(bot, listlink.build("gwnets", slot), "весь список") if slot and bot else ""
        out.append(f"⚠️ Ещё {n} " + plural_ru(n, "подсеть", "подсети", "подсетей") + " вне адресов в туннеле"
                   + (f" — {whole}, править — в «🗺 Подсети»" if whole else ": проверь их в «🗺 Подсети»"))
    return out


def lan_issue_line(it: dict) -> str:
    """Подсеть слота, не попавшая в таблицу адресов в туннеле, — строкой «⚠️ …»
    с причиной (карточка и экран «весь список» подсетей)."""
    net = f"<code>{_e(it.get('net', ''))}</code>"
    # подсеть накрывает префикс соседа в туннеле: на шлюзе она пересеклась
    # бы с его AllowedIPs — конфиг линка не соберётся
    link = ", и конфиг линка не соберётся (пересекается с адресами в туннеле другого шлюза)" \
        if it.get("blocks_link") else ""
    if it.get("kind") == "space":
        return (f"⚠️ {net} — в служебном пространстве сервера AWG, в туннеле не видна{link}: смени подсеть "
                "в «🗺 Подсети»")
    if it.get("kind") == "clash":
        return (f"⚠️ {net} в туннеле (<code>{_e(it.get('twin', ''))}</code>) пересекается с "
                f"<code>{_e(it.get('other', ''))}</code> (<code>{_e(it.get('other_twin', ''))}</code>) "
                "и не видна: убери одну из них в «🗺 Подсети»")
    if it.get("kind") == "wide":
        return f"⚠️ {net} шире /16, в туннеле не видна{link}: сузь подсеть в «🗺 Подсети»"
    return ""


def keep_line(keep: dict | None, agent_bot: dict | None = None) -> str:
    """Копия настроек бота шлюза в бэкапе сервера: на дату, ждёт решения в
    чате шлюза, агент её не присылает (на связи и без keep1)."""
    keep = keep or {}
    if keep.get("state") == "outdated":
        return "⚠️ Настройки бота шлюза не сохраняются в бэкап: агент шлюза старше 3.4.0 — обнови его"
    if keep.get("state") == "asked":
        what = {"device": "⏳ Настройки прежнего устройства",
                "same": "⏳ Настройки бота шлюза с сервера AWG"}.get(keep.get("asked"), "⏳ Настройки бота шлюза из бэкапа")
        from awgbot.domain.notices.backup import chat_ref
        return f"{what} ждут решения в чате {chat_ref(agent_bot)}"
    if keep.get("state") == "saved":
        try:
            when = timeutil.fmt_dt_ui(timeutil.parse_iso(keep["at"]), year=True)
        except (ValueError, KeyError, TypeError):
            return ""
        return f"💾 Настройки бота шлюза в бэкапе: на {when}"
    return ""


def gateway_card_text(state: dict, states: list) -> str:
    """Карточка слота: заголовок со значком («⭐» у предпочтительного, «🛰» у
    остальных), линк, адрес и трафик, канал конфигурации, подсети и VPN-транзит,
    свои списки, связь подсетей, SMB, бот шлюза; предупреждения открытыми
    строками; редкое — под «подробнее»."""
    gw, dev = state["gateway"], state.get("device")
    two = len(states) > 1
    head = ui.head(f"{'⭐' if state.get('preferred') and two else '🛰'} {slot_name(state)}")
    if state.get("join"):
        # замена устройства идёт: статус слота — она, а не «🟢 Активен»
        # линка, который уже на ключах команды (ui-7)
        from .join import join_card_line
        lines = [f"{head} — {join_card_line(state['join'])}"]
    else:
        lines = [f"{head} — {slot_status(state)}"]
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
    bot, slot = str(state.get("bot_username") or ""), int(gw.id)
    ch_head, ch_warns, egress = channel_lines(state.get("channel"), state.get("link_ok"), bot, slot)
    if ch_head:
        lines.append(ch_head)
    fleet = state.get("fleet") or {}
    ch = state.get("channel") or {}
    cfg = ch.get("cfg") or {}
    from ..fleet import gw_no_upd, gw_state_line
    old = bool(fleet.get("row")) and gw_no_upd(fleet["row"])
    if old:
        # агент ниже 3.4.0: чего он не умеет — одной строкой, с одним «обнови»
        # (без команды сервера AWG он обновляется только из своего чата)
        facts = []
        if ch.get("outdated"):
            facts.append("настройки по каналу конфигурации не принимает")
        if (state.get("keep") or {}).get("state") == "outdated":
            facts.append("настройки бота шлюза в бэкап не сохраняет")
        facts.append("командой сервера AWG не обновляется")
        what = ", ".join(facts[:-1]) + (" и " if len(facts) > 1 else "") + facts[-1]
        ver = _ver(_e(str(fleet["row"].get("version") or "")[:32]))
        lines.append(f"⚠️ Агент {ver} устарел: {what} — обнови его в чате "
                     f"{chat_ref(fleet['row'].get('agent_bot') or state.get('agent_bot'))}: "
                     "«⚙️ Настройки» → «⬆️ Обновления»")
    elif fleet.get("row"):
        # набор обновлений: состояние — сразу под строкой канала
        fl = gw_state_line(fleet["row"], release=fleet.get("release") or "", run=fleet.get("run"))
        if fl:
            lines.append(fl)
    if ch.get("outdated") and not old:
        # агент ниже 3.3.0 (молчавший шлюз вернулся): документа не принимает —
        # рядом с каналом, о котором оно; версия — из снимка, если он есть;
        # ссылка на бота — строкой «Бот шлюза» внизу карточки, второй раз не нужна
        ver = _ver(_e(str(ch.get("agent") or "?")[:32])) if ch.get("has_snap") else ""
        lines.append(f"⚠️ Агент {ver + ' ' if ver else ''}устарел: настройки по каналу конфигурации не принимает — "
                     "обнови агента шлюза")
    keep = keep_line(state.get("keep"), state.get("agent_bot"))
    if keep and not ch.get("outdated") and not (old and (state.get("keep") or {}).get("state") == "outdated"):
        lines.append(keep)                                # «не сохраняет» у старого агента — в строке выше
    rk = state.get("rekey") or {}
    if rk.get("auto") and rk.get("state") in ("staged", "asked", "switched"):
        lines.append("🟡 Ключи линка: переключение…")         # без вопроса (I1, сопряжение) — ждать некого
    elif rk.get("auto") and rk.get("state") == "declined":
        pass                                              # отказ смены без вопроса — строкой I1 после трёх
    elif rk.get("state") in ("staged", "asked"):
        fp = f" · отпечаток <code>{_e(rk['fp'])}</code>" if rk.get("fp") else ""
        lines.append(f"⏳ Ключи линка ждут подтверждения в чате бота шлюза{fp}")
    elif rk.get("state") == "switched":
        lines.append("🟡 Ключи линка: переключение…")
    elif rk.get("state") == "declined":
        lines.append("⚠️ Шлюз отклонил новые ключи линка — линк работает на прежних; сменить снова — "
                     "«⚙️ Настройки» → «🔧 Сервис» → «🔑 Ключи линка»")
    if state.get("i1"):
        lines.append(state["i1"])                          # I1 линка — уже готовой строкой
    lines += ch_warns
    nets = gw.lan_subnets
    nets_s = (lan_pairs(nets, state.get("lan_twins"), bot=bot, payload=listlink.build("gwnets", slot)) if nets
              else "Подсети не заданы — без них сеть шлюза из туннеля не открыть")
    lines.append(f"🗺 {nets_s} · 🔀 VPN-транзит " + ("✅" if gw.lan_mode else "☑️"))
    lines += lan_space_lines(state.get("lan_space"), bot, slot)
    if gw.lan_mode:
        own = state.get("own_lists") or {}
        if own and own.get("state") != "off":
            lines.append(own_lists_line(own, state.get("agent_bot")))
    svc = state.get("services") or {}
    if state.get("peer_nets"):
        lines.append(peer_links_line(state.get("peer_links") or [], bot, slot))
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
    if more:
        lines.append(details(";\n".join(more)))
    return "\n".join(lines)


def gateway_edit_text(state: dict) -> str:
    return ui.head(f"✏️ {slot_name(state)}") + " — изменить:"
