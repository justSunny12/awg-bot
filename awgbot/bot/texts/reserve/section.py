"""section.py — раздел «🛟 Резерв» без пары (Т-Р1) и карточка резерва (Т-Р9),
диалоги удаления и выключения готовности, экран «🆘 На крайний случай»."""
from __future__ import annotations

from awgbot.util import timeutil
from awgbot.util.textfmt.base import details

from ..fmt import _e


def _n(name: str) -> str:
    return f"«{_e(name)}»"


def _when(iso: str) -> str:
    try:
        return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
    except ValueError:
        return ""


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
    lines.append("")
    out_ok, out_why = d.get("dir_out") or (False, "")
    in_ok, in_why = d.get("dir_in") or (False, "")
    lines.append("📤 Мои клиенты: на этом сервере")
    if out_ok:
        gws = ", ".join(_n(g) for g in d.get("voters") or [])
        sent = d.get("pkg_sent_at") or ""
        tail = f"данные для переезда у {_n(name)} от {_e(_when(sent))}" if sent else f"данные для переезда у {_n(name)} ещё не отправлены"
        lines.append("└ " + tail + (f" · падение подтвердят шлюзы {gws}" if gws else ""))
    else:
        lines.append("└ ⚠️ " + _e(out_why))
    lines.append(f"📥 Клиенты {_n(name)}: на его сервере")
    if not in_ok and in_why:
        lines.append("└ ⚠️ " + _e(in_why))
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
            "экран админа второго сервера в этом боте — прочь. Второй сервер узнает об этом сам. Подключить "
            "заново можно только новым приглашением")


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


def code_shown(code: str) -> str:
    return ("🆘 <b>Аварийный код резерва</b>\n\n"
            f"<code>{_e(code)}</code>\n\n"
            "Сообщение удалю через 10 минут")
