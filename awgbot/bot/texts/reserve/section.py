"""section.py — раздел «🛟 Резерв» без пары (Т-Р1) и карточка резерва (Т-Р9),
диалоги удаления и выключения готовности, экран «🆘 На крайний случай»."""
from __future__ import annotations

from awgbot.util import timeutil
from awgbot.util.textfmt.base import details

from ..fmt import _e


LOW_MEM_MB = 400                     # меньше — строка «мало свободной памяти» (решение по в. 56)


def _n(name: str) -> str:
    return f"«{_e(name)}»"


def _when(iso: str) -> str:
    try:
        return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
    except ValueError:
        return ""


def _date(iso: str) -> str:
    try:
        return timeutil.fmt_dt_ui(timeutil.parse_iso(iso))
    except ValueError:
        return ""


def _move_step(step: str) -> str:
    return {"pkg": "обновляю данные для переезда", "up": "жду подъёма клиентов", "record": "жду смену записи",
            "release": "передаю бот", "released": "передаю бот"}.get(step, "идёт переезд")


ABOUT = ("Два сервера AWG с разными админами страхуют друг друга: упадёт один — его клиенты и его бот будут "
         "работать на втором без новых конфигураций, вернётся — всё переедет обратно, вместе с тем, что "
         "изменилось за это время. Свои клиенты каждого всегда остаются на своём сервере")

MORE = ("Резерв хранит у себя зашифрованные данные твоих клиентов и бота. Открыть их он может, только когда "
        "этот сервер не отвечает ему и хотя бы одному твоему шлюзу, — или когда этот сервер сам их отдаст: ты "
        "перевёл клиентов кнопкой; все шлюзы 15 минут не видят сервер, а резерв видит (похоже, сервер закрыли "
        "из РФ); шлюзы 15 минут не на связи с резервом — тогда РФ-доступ клиентов пойдёт через шлюзы резерва. "
        "Пока клиенты на резерве, ключи и данные живут только в памяти и работает с ними только твой бот — "
        "резерв даёт ему VPN и сеть; после возврата всё это снимается, а ключи для следующего раза меняются. "
        "Как у любого оператора VPN, трафик твоих клиентов в это время идёт через сервер резерва, и его админ "
        "— хозяин своего сервера. Ключ сервера без перевыпуска конфигураций не сменить — если доверие "
        "кончилось, «🖥 Сервер AWG» → «🚚 Порт или подсеть». Устройства, получившие конфигурацию, когда адресом "
        "сервера был IP, к резерву не переедут. Переезжают устройства при переподключении VPN — бот напишет "
        "тем, кто был на связи. На случай, когда сервер AWG и все шлюзы недоступны разом, — аварийный код: "
        "бот пришлёт его после подключения резерва, показать снова — «🆘 На крайний случай»")


def section_text(d: dict) -> str:
    """Раздел без пары: проверки хоста и готовность."""
    lines = ["🛟 <b>Резерв</b>", "", ABOUT, ""]
    host = d.get("host") or ""
    if not host:
        lines.append("⛔ Адрес для клиентов не задан — задай домен в «🖥 Сервер AWG»")
    elif d.get("host_is_ip"):
        lines.append(f"⛔ Адрес для клиентов — IP <code>{_e(host)}</code>: переезд меняет A-запись домена, а у IP "
                     "её нет. Задай домен в «🖥 Сервер AWG» — новые конфигурации получат его, выданные — нет")
    else:
        lines.append(f"🟢 Адрес для клиентов: <code>{_e(host)}</code>")
    if d.get("port"):
        lines.append(f"🟢 Порт клиентов: <code>{int(d['port'])}</code>")
    lines.append("🔴 Запись DNS: не настроена — переезд только кнопкой, запись меняешь у провайдера сам")
    gws = d.get("gateways") or []
    good = [g for g in gws if g.get("rsv")]
    old = [g for g in gws if not g.get("rsv")]
    if not gws:
        lines.append("🔴 Шлюзов нет — твоих клиентов резерв примет, только если у сервера есть хотя бы один шлюз "
                     "на агенте v4.0.0 или новее: шлюз подтверждает падение сервера и держит часть ключа. "
                     "Подключи его: «🛰 Шлюзы» → «🛰 Подключить шлюз»")
    else:
        if good:
            lines.append("🟢 Шлюзы: " + ", ".join(_n(g["name"]) for g in good) + " — подтвердят падение сервера")
        for g in old:
            lines.append(f"🟡 Шлюз {_n(g['name'])} на агенте v{_e(g.get('version') or '?')} — обнови: пока он не "
                         "голосует и не держит часть ключа")
    ready = bool(d.get("ready"))
    lines.append(("✅ Готов быть резервом: вкл" if ready else
                  "☑️ Готов быть резервом: выкл — пока выключено, пригласить и принять приглашение нельзя"))
    if d.get("invite_live"):
        lines.append("")
        lines.append("📤 Приглашение выпущено — ждёт второго сервера; сообщение с ним выше в чате")
    if d.get("accepting"):
        lines.append("")
        lines.append("📥 Подключаюсь ко второму серверу — код для его админа выше в чате")
    lines += ["", details("▸ подробнее: " + MORE)]
    return "\n".join(lines)


def card_text(d: dict) -> str:
    """Карточка резерва (этап пары: статус, готовность, направления, итог трафика)."""
    peer = d.get("peer") or {}
    name = peer.get("name") or "резерв"
    ver = (d.get("peer_state") or {}).get("version") or ""
    seen = d.get("seen") or "ok"
    if seen == "down":
        status = f"🔴 недоступен с {_e(_when(d.get('down_since') or ''))}"
    elif d.get("out") in ("away", "leaving"):
        status = f"🟢 несёт нагрузку с {_e(_when((d.get('out_since') or '')))}"
    else:
        status = "🟢 жив"
    head = f"🛟 <b>Резерв {_n(name)}</b> {status}" + (f" · v{_e(ver)}" if ver else "")
    lines = [head, "", f"Сервер {_n(name)} · <code>{_e(peer.get('host') or '')}</code>"]
    my_ready, peer_ready = bool(d.get("ready")), bool((d.get("peer_state") or {}).get("ready"))
    if my_ready and peer_ready:
        lines.append("✅ Готовы быть резервом оба")
    elif not my_ready:
        lines.append("☑️ Ты не готов быть резервом — переезды в обе стороны выключены")
    else:
        lines.append(f"☑️ {_n(name)} не готов быть резервом — переезды в обе стороны выключены")
    if not d.get("online"):
        since = d.get("down_since") or ""
        lines.append(f"🔴 Нет связи с {_n(name)}" + (f" с {_e(_when(since))}" if since else ""))
    mem = int(d.get("mem_free") or 0)
    if mem and mem < LOW_MEM_MB:
        lines.append(f"⚠️ У {_n(name)} мало свободной памяти ({mem} МБ) — твоих клиентов он может не потянуть")
    lines.append("")
    out_ok, out_why = d.get("dir_out") or (False, "")
    in_ok, in_why = d.get("dir_in") or (False, "")
    out, inn = str(d.get("out") or "home"), str(d.get("inn") or "idle")
    if out == "leaving":
        lines.append(f"📤 Мои клиенты: переезжают на {_n(name)} — {_e(_move_step(d.get('out_step') or ''))}")
    elif out == "away":
        here, there = int(d.get("here") or 0), int(d.get("there") or 0)
        tail = f" · здесь ещё {here}" + (f", там {there}" if there else "") if here or there else ""
        lines.append(f"📤 Мои клиенты: на {_n(name)} с {_e(_when(d.get('out_since') or ''))}{tail}")
    elif out == "returning":
        lines.append(f"📤 Мои клиенты: возвращаются с {_n(name)}")
    else:
        lines.append("📤 Мои клиенты: на этом сервере")
        if out_ok:
            gws = ", ".join(_n(g) for g in d.get("voters") or [])
            sent = d.get("pkg_sent_at") or ""
            tail = f"данные для переезда у {_n(name)} от {_e(_when(sent))}" if sent else f"данные для переезда у {_n(name)} ещё не отправлены"
            lines.append("└ " + tail + (f" · падение подтвердят шлюзы {gws}" if gws else ""))
            if d.get("pkg_error"):
                lines.append(f"└ ⚠️ данные для переезда не собираются: {_e(d['pkg_error'])}")
        else:
            lines.append("└ ⚠️ " + _e(out_why))
        for g in d.get("gw_links") or []:
            if not g.get("rsv1"):
                continue
            if g.get("seen") == "down" or (g.get("seen") == "absent" and g.get("online")):
                since = g.get("since") or ""
                lines.append(f"└ ⚠️ {_n(g['name'])} не на связи с {_n(name)}" + (f" с {_e(_when(since))}" if since else ""))
        if d.get("peer_case_line"):
            lines.append("└ " + _e(d["peer_case_line"]))
        if d.get("move_why") and out_ok:
            lines.append("└ перевести кнопкой нельзя: " + _e(d["move_why"]))
        if d.get("out_relay_until"):
            lines.append(f"└ пересылка хвоста у {_n(name)} до {_e(_date(d['out_relay_until']))}")
    if inn in ("taking", "serving", "releasing"):
        since = d.get("in_since") or ""
        lines.append(f"📥 Клиенты {_n(name)}: здесь" + (f" с {_e(_when(since))}" if since else "")
                     + (" — принимаю" if inn == "taking" else " — возвращаю" if inn == "releasing" else ""))
    elif inn == "relay":
        lines.append(f"📥 Клиенты {_n(name)}: возвращаются к нему — пересылка до {_e(_date(d.get('in_relay_until') or ''))}")
    else:
        lines.append(f"📥 Клиенты {_n(name)}: на его сервере")
        if not in_ok and in_why:
            lines.append("└ ⚠️ " + _e(in_why))
        if d.get("await_merge"):
            lines.append(f"└ ⏳ жду реплику {_n(name)} — она вольётся, когда он выйдет на связь")
        if d.get("code_open"):
            lines.append(f"└ 🆘 {_n(name)} и его шлюзы не отвечают — жду аварийный код его админа")
        elif d.get("auto_block"):
            lines.append("└ ⚠️ переезд по взглядам не идёт: " + _e(d["auto_block"]))
        elif d.get("rf_line"):
            lines.append("└ " + _e(d["rf_line"]))
    dns = d.get("dns") or {}
    if dns.get("name"):
        prov = dns.get("provider_label") or "вручную"
        cur = dns.get("current") or ""
        where = ("этот сервер" if dns.get("points_here") else f"<code>{_e(cur)}</code>") if cur else "не проверена"
        lines.append(f"🌐 Запись <code>{_e(dns['name'])}</code>: {where} · {_e(prov)}"
                     + ("" if dns.get("provider") == "manual" or dns.get("has_token") else " · токен не задан"))
    tr = d.get("traffic") or {}
    if tr.get("out") or tr.get("in"):
        lines += ["", f"📊 Через резерв за {_e(tr.get('month_label') or 'месяц')}"]
        if tr.get("out"):
            lines.append(f"└ мои у {_n(name)}: {_e(tr['out'])}")
        if tr.get("in"):
            lines.append(f"└ {_n(name)} у меня: {_e(tr['in'])}")
    ret = "сразу, как только этот сервер в строю пять минут" if d.get("ret_immediate") else "по кнопке в боте"
    lines += ["", details("▸ подробнее: Свои цифры каждый сервер считает сам; месяц — по часовому поясу своей "
                          f"стороны. Возврат клиентов: {ret}")]
    return "\n".join(lines)


def unpair_dialog(name: str) -> str:
    return (f"🗑 <b>Удалить резерв {_n(name)}?</b>\n\n"
            f"Линк пары снимется, данные для переезда у {_n(name)} перестанут обновляться, его доля ключа и "
            "экран админа второго сервера в этом боте — прочь. Второй сервер получит уведомление, когда будет на "
            "связи. Подключить заново можно только новым приглашением")


def ready_off_dialog(name: str) -> str:
    return (f"☑️ <b>Выключить «Готов быть резервом»?</b>\n\n"
            f"Выключение действует в обе стороны: ни ты не примешь клиентов {_n(name)}, ни он твоих. "
            "Пока идёт переезд, выключение вступит после возврата")


def emergency_text(has_code: bool, shown_at: str) -> str:
    lines = ["🆘 <b>На крайний случай</b>", "",
             "Аварийный код нужен, только если сервер AWG и все шлюзы недоступны резерву разом: тогда его "
             "вводит админ этого сервера в боте резерва — на своём экране там. Код — часть ключа переезда: "
             "резерв без него и без шлюза ключ не соберёт. Код меняется, когда ключ мог стать известен резерву: "
             "после переезда по доле шлюза или по коду, при снятии или замене шлюза и по кнопке ниже"]
    if has_code:
        lines += ["", "Показанный код удаляется из чата через 10 минут" + (f"; последний показ — {_e(_when(shown_at))}" if shown_at else "")]
    else:
        lines += ["", "⚠️ Кода нет — он появится после подключения резерва"]
    return "\n".join(lines)


def new_code_dialog(name: str) -> str:
    return (f"🔄 <b>Сменить аварийный код?</b>\n\n"
            f"Ключ переезда сменится: прежний код и доли ключа у шлюзов и у {_n(name)} перестанут действовать, "
            f"новые уедут им сами в ближайшие минуты. Новый код пришлю отдельным сообщением")


# ── Т-Р10: переезд кнопкой ───────────────────────────────────────────────────

def move_dialog(d: dict) -> str:
    name, record, target = _n(d.get("name") or "резерв"), _e(d.get("record") or ""), _e(d.get("target") or "")
    if d.get("manual"):
        first = f"Смени у провайдера A-запись <code>{record}</code> на <code>{target}</code> — бот увидит смену сам"
    else:
        first = f"Запись <code>{record}</code> переключится на <code>{target}</code> сама"
    return (f"🔀 <b>Перевести клиентов на {name}?</b>\n\n"
            f"{first}. Устройства перейдут при переподключении VPN, бот переедет вместе с ними, подключение шлюзов к "
            f"{name} не меняется. Настройки, шлюзы, бэкапы и обновления до возврата будут недоступны; {name} получит "
            f"уведомление. Вернуть всё обратно можно в любой момент, пока {name} на связи — «↩️ Вернуть "
            "клиентов»; вернётся и всё, что изменилось там")


def move_live(d: dict) -> str:
    """Т-Р10б: ход перевода в живом сообщении; итоги — по состоянию."""
    name = _n(d.get("name") or "резерв")
    state, step = str(d.get("state") or ""), str(d.get("step") or "")
    if state == "away":
        there, here = int(d.get("there") or 0), int(d.get("online") or 0)
        tail = ", ".join(x for x in ((f"здесь ещё {here}" if here else ""), (f"там {there}" if there else "")) if x)
        return f"✅ Клиенты и бот — на {name}: устройства переходят при переподключении" + (f" — {tail}" if tail else "")
    if state == "home" and d.get("reason"):
        return f"⚠️ Перевод не состоялся: {_e(d['reason'])}"
    if state != "leaving":
        return f"ℹ️ Перевод клиентов на {name} завершён"
    order = ["pkg", "up", "record", "release", "released"]
    pos = order.index(step) + 1 if step in order else 1      # 1 — пакет в работе, 2 — резерв поднимает, 3 — запись, 4 — бот
    def mark(i):
        return "✅" if i < pos else "⏳"
    record, target = _e(d.get("record") or ""), _e(d.get("target") or "")
    lines = [f"⏳ <b>Перевод клиентов на {name}</b>", "",
             f"{mark(1)} данные для переезда у {name}" + (f" — от {_e(_when(d.get('pkg_at') or ''))}" if d.get("pkg_at") else ""),
             f"{mark(2)} {name} поднял клиентов",
             f"{mark(3)} " + (f"запись <code>{record}</code> → <code>{target}</code>" if pos > 3
                               else (f"смени запись <code>{record}</code> на <code>{target}</code> у провайдера — жду…"
                                     if d.get("manual") else f"жду смену записи <code>{record}</code> → <code>{target}</code>…")),
             f"{mark(4)} передаю бот на {name}…" if pos < 5 else f"✅ бот передан на {name}"]
    return "\n".join(lines)


def return_dialog(d: dict) -> str:
    """Т-Р11б: вернуть клиентов с резерва (у основы, рычаг «по кнопке»)."""
    name = _n(d.get("name") or "резерв")
    rec = ""
    if d.get("record") and d.get("own"):
        rec = (f"; смени у провайдера запись <code>{_e(d['record'])}</code> на <code>{_e(d['own'])}</code> сам — бот увидит смену"
               if d.get("manual") else f"; запись <code>{_e(d['record'])}</code> вернётся на <code>{_e(d['own'])}</code>")
    return (f"↩️ <b>Вернуть клиентов с {name}?</b>\n\n"
            f"Сервер в строю. Бот вернётся сюда, всё, что изменилось на {name}, — тоже{rec}. Устройства, "
            f"которые ещё ходят через {name}, он будет пересылать сюда, пока они не переподключатся, — у каждого "
            "связь прервётся на секунды")


# ── Т-Р12: вернуть клиентов к основе (у резерва) ─────────────────────────────

def force_dialog(d: dict) -> str:
    """С9: вернуть клиентов без резерва."""
    name = _n(str(d.get("name") or "резерв"))
    since = _when(str(d.get("since") or ""))
    rec = ""
    if d.get("record") and d.get("own"):
        rec = (f"смени у провайдера запись <code>{_e(d['record'])}</code> на <code>{_e(d['own'])}</code> сам"
               if d.get("manual") else f"запись <code>{_e(d['record'])}</code> вернётся на <code>{_e(d['own'])}</code>")
    return (f"⚠️ <b>Вернуть клиентов без {name}?</b>\n\n"
            f"{name} не на связи" + (f" с {since}" if since else "") + ". Этот сервер заберёт Telegram и примет устройства сам"
            + (f"; {rec}" if rec else "") + f". Всё, что изменилось на {name} за это время, вольётся, когда он выйдет на связь; "
            "пока он не на связи, пересылки хвоста с него нет — устройства, которые ещё ходят через него, переподключатся сами")


def evict_dialog(name: str) -> str:
    n = _n(name)
    return (f"↩️ <b>Вернуть клиентов и бот {n} к нему?</b>\n\n"
            f"{n} на связи — он примет свой бот и данные, сам переключит запись на себя. Пока устройства {n} не "
            f"переподключатся, этот сервер пересылает их трафик к нему; данные {n} здесь снимаются сразу. {n} получит "
            "уведомление")


# ── Т-Р16: запись DNS ───────────────────────────────────────────────────────

DNS_MORE = ("▸ подробнее: Бот меняет только эту A-запись: при переезде кнопкой и при возврате — сам, при падении "
            "сервера — резерв, токеном из зашифрованных данных, которые он открывает только при переезде. После такого "
            "переезда бот попросит сменить токен. Раз в несколько минут бот сверяет запись и сообщит, если она "
            "указывает не сюда. Часть операторов связи держит запись в кэше дольше TTL — их абоненты переходят позже. "
            "Лучше дать токену право только на эту зону")


def dns_text(d: dict) -> str:
    name = d.get("name") or ""
    if not name:
        return ("🌐 <b>Запись DNS</b>\n\n⛔ Адрес для клиентов — IP, записи DNS у него нет. Задай домен в "
                "«🖥 Сервер AWG» — без записи переезд невозможен")
    lines = [f"🌐 <b>Запись DNS</b> · <code>{_e(name)}</code>", ""]
    cur = d.get("current") or ""
    addrs = [a for a in (d.get("addrs") or []) if a]
    if cur:
        who = "этот сервер" if d.get("points_here") else "не этот сервер"
        shown = ", ".join(f"<code>{_e(a)}</code>" for a in addrs) if len(addrs) > 1 else f"<code>{_e(cur)}</code>"
        lines.append(f"Сейчас: {shown} — {who}" + (f" · проверено в {_e(_when(d.get('checked_at') or ''))}"
                                                  if d.get("checked_at") else ""))
        if len(addrs) > 1:
            lines.append(f"⚠️ У записи {len(addrs)} адреса — при переезде часть клиентов останется на старом; оставь одну A-запись")
        ttl = int(d.get("ttl") or 0)
        if ttl:
            lines.append(f"TTL: {ttl} с" + (f" — ⚠️ клиенты перейдут на новый адрес не раньше чем через {ttl // 60} мин; "
                                           f"поставь ≤ {int(d.get('ttl_warn') or 300)} с" if ttl > int(d.get("ttl_warn") or 300) else ""))
    else:
        lines.append("Сейчас: не проверена — «🧪 Проверить»")
    prov = d.get("provider_label") or "вручную"
    if d.get("provider") == "manual":
        lines.append("Провайдер: вручную — при переезде запись меняет человек, аварийный переезд невозможен")
    else:
        lines.append(f"Провайдер: {_e(prov)} · " + ("токен задан" if d.get("has_token") else "⚠️ токен не задан"))
    if d.get("check_ok") is False and d.get("check_why"):
        lines.append("⚠️ " + _e(d["check_why"]))
    lines += ["", details(DNS_MORE)]
    return "\n".join(lines)


def dns_token_prompt(provider: str, zone_hint: str) -> str:
    if provider == "dyndns2":
        return "✏️ <b>DynDNS2</b>\n\nПришли строку <code>https://логин:пароль@сервер/nic/update</code> — сообщение удалю сразу"
    if provider == "duckdns":
        return "✏️ <b>Токен DuckDNS</b>\n\nПришли токен из личного кабинета DuckDNS — сообщение удалю сразу"
    return (f"✏️ <b>Токен Cloudflare</b>\n\nПришли API-токен с правом Zone → DNS → Edit на зону "
            f"<code>{_e(zone_hint)}</code> — сообщение удалю сразу")

