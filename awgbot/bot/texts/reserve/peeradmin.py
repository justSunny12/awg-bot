"""peeradmin.py — экран роли админа второго сервера (Т-РН1) и приглашение
кода (Т-РН2, Т-РН3). С точки зрения бота «Beta», в который пишет админ
«Alpha»."""
from __future__ import annotations

from awgbot.util import timeutil

from ..fmt import _e


def _n(name: str) -> str:
    return f"«{_e(name)}»"


def _when(iso: str) -> str:
    try:
        return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
    except ValueError:
        return ""


def screen(d: dict) -> str:
    me, peer = str(d.get("me") or "этот сервер"), str((d.get("peer") or {}).get("name") or "твой сервер")
    lines = [f"🛟 <b>Резерв {_n(me)} для {_n(peer)}</b> · v{_e(str(d.get('version') or ''))}", "",
             f"Ты — админ {_n(peer)}. Здесь видно, как {_n(me)} страхует твой сервер; управлять {_n(me)} отсюда нельзя", ""]
    ok, why = d.get("direction_in") or (False, "")
    if ok:
        lines.append("✅ Твои пользователи переедут сюда, если сервер упадёт")
    elif not d.get("my_ready"):
        lines.append(f"☑️ {_n(me)} не готов быть резервом")
    else:
        lines.append("⚠️ Твои пользователи сюда не переедут: " + _e(why))
    lines += ["", f"<b>Сервисы {_n(me)}</b>",
              "🟢 бот и AWG" if d.get("awg_ok") else "🔴 AWG не работает"]
    if d.get("link_online"):
        lines.append(f"🟢 линк с {_n(peer)}")
    else:
        since = d.get("down_since") or ""
        lines.append(f"🔴 линк с {_n(peer)}: нет связи" + (f" с {_e(_when(since))}" if since else ""))
    if d.get("has_gateway"):
        use_since = _when(((d.get("rfpeer") or {}).get("peer_use") or {}).get("since") or "")
        lines.append((f"🟢 РФ-доступ через шлюзы {_n(me)}: разрешён" + (f" · сейчас идёт через них с {_e(use_since)}" if use_since else ""))
                     if d.get("rf_via_mine") else f"☑️ РФ-доступ через шлюзы {_n(me)}: выключен его админом")
    mem = int(d.get("mem_free") or 0)
    if mem and mem < 400:
        lines.append(f"⚠️ у {_n(me)} мало свободной памяти ({mem} МБ) — твоих пользователей он может не потянуть")
    tp = d.get("tg_pool") or {}
    if tp:
        learned = int(tp.get("learned") or 0)
        tail = f" и {learned} выученн{'ая сеть' if learned == 1 else ('ые сети' if 2 <= learned <= 4 else 'ых сетей')}" if learned else ""
        lines.append(("⚠️" if tp.get("stale") else "🟢") + f" пул адресов Telegram для вебхука: ядро{tail}"
                     + (" · анонсы давно не обновлялись" if tp.get("stale") else ""))
    gws = list(d.get("gw_views") or [])
    known = {int(g.get("slot") or 0) for g in gws}
    for slot in d.get("zone_links") or []:                   # сторона в зоне есть, сессии нет — линк без связи
        if int(slot) not in known:
            gws.append({"slot": int(slot), "name": f"шлюз {int(slot)}", "voter": False, "down_since": "", "seen": ""})
    gws.sort(key=lambda g: int(g.get("slot") or 0))
    if d.get("zone_links") and gws:
        down = [g for g in gws if not g.get("voter")]
        if not down:
            lines.append("🟢 линки твоих шлюзов на связи: " + ", ".join(_e(str(g["name"])) for g in gws))
        else:
            for g in down:
                since = g.get("down_since") or ""
                lines.append(f"⚠️ линк: {_e(str(g['name']))} без связи" + (f" с {_e(_when(since))}" if since else ""))
    lines += ["", f"<b>Как {_n(me)} видит {_n(peer)}</b>"]
    seen = d.get("seen") or "ok"
    if seen == "down":
        lines.append(f"🔴 сервер не отвечает с {_e(_when(d.get('down_since') or ''))}")
    elif seen == "suspect":
        lines.append("🟡 сервер молчит — проверяю")
    else:
        lines.append("🟢 сервер на связи")
    if gws:
        parts = []
        for g in gws:
            if not g.get("voter"):
                since = g.get("down_since") or ""
                parts.append(f"⚠️ {_n(g['name'])} не на связи с {_n(me)}" + (f" с {_e(_when(since))}" if since else ""))
            elif g.get("seen"):
                parts.append(f"🔴 {_n(g['name'])} не видит сервер с {_e(_when(g['seen']))}")
        sees = [g["name"] for g in gws if g.get("voter") and not g.get("seen")]
        if sees:
            parts.insert(0, "🟢 " + " и ".join(_n(n) for n in sees) + (" видит сервер" if len(sees) == 1 else " видят сервер"))
        lines.append(" · ".join(parts))
    q = d.get("quorum") or {}
    if q:                                                     # Т-К22
        mine = "видит" if q.get("w") in ("see", "held") else ("не видит" + (f" с {_e(_when(q['since']))}" if q.get("since") else ""))
        lines.append(f"🗳 Твой {_n(peer)} видят {int(q.get('y') or 0)} из {int(q.get('v') or 0)} · этот сервер — {mine}")
    inn = d.get("inn") or "idle"
    if d.get("code_needed") and d.get("code_by") == "link":
        lines.append(f"🆘 {_n(me)} не на связи с твоим сервером с {_e(_when(d.get('code_since') or ''))}, а голосование о нём "
                     "не включено — пришли аварийный код резерва, и пользователи переедут сюда")
    elif d.get("code_needed"):
        lines.append(f"🆘 {_n(me)} не видит твой сервер с {_e(_when(d.get('code_since') or ''))}, а голосов для переезда "
                     "не хватает — пришли аварийный код резерва, и пользователи переедут сюда")
    if inn in ("taking", "serving"):
        why = {"dead": " — сервер не отвечал", "button": " — ты перевёл их кнопкой", "rf": " — сервер недоступен из РФ",
               "code": " — по твоему аварийному коду"}.get(str(d.get("in_kind") or ""), "")
        lines += ["", f"📥 Твои пользователи и бот здесь с {_e(_when(d.get('in_since') or ''))}{why}"]
    if d.get("pkg_at"):
        lines += ["", f"📦 Данные для переезда — от {_e(_when(d['pkg_at']))}"]
    else:
        lines += ["", "📦 Данных для переезда ещё нет — они придут с твоего сервера"]
    tr = d.get("traffic") or {}
    if tr.get("in"):
        lines.append(f"📊 Твои пользователи через {_n(me)} за {_e(tr.get('month_label') or 'месяц')}: {_e(tr['in'])}")
    return "\n".join(lines)


def code_prompt(d: dict) -> str:
    """Т-РН2: приглашение ввести аварийный код."""
    peer, me = _n(str(d.get("peer") or "")), _n(str(d.get("me") or ""))
    rec = f" запись <code>{_e(d['record'])}</code> бот сменит сам." if d.get("record") else " запись для пользователей бот сменит сам."
    return (f"🔑 <b>Переезд {peer} на {me}</b>\n\n{peer} и его шлюзы не отвечают этому серверу с {_e(_when(d.get('since') or ''))}. "
            f"Пришли аварийный код — пользователи и бот {peer} переедут сюда,{rec} Сообщение с кодом удалю сразу")


def code_result(res: dict, peer: str) -> str:
    """Т-РН3: итог ввода кода."""
    if res.get("ok"):
        if res.get("pending"):
            return (f"✅ Код принят — поднимаю пользователей {_n(peer)}; если код не подойдёт, напишу сюда. "
                    "Дальше пиши своему боту — он ответит, как только будет готов")
        return (f"✅ Код подошёл — поднимаю пользователей {_n(peer)}. "
                "Дальше пиши своему боту — он ответит, как только будет готов")
    why = str(res.get("why") or "")
    if why == "garbled":
        return "⚠️ Это не похоже на код — проверь группы и знаки и пришли ещё раз"
    if why == "wrong":
        left = int(res.get("left") or 0)
        head = "⚠️ Код не подошёл" if res.get("checked") else "⚠️ Код не разобран — проверь группы и знаки"
        return f"{head}. Осталось попыток: {left}" + (" — следующие через час" if left else "")
    if why == "open_failed":
        return (f"⚠️ Код подошёл, но пользователей {_n(peer)} поднять не удалось: {_e(str(res.get('reason') or ''))}. "
                "Приём кода снова открыт")
    if why == "limit":
        return "⚠️ Код не подошёл три раза — следующие попытки через час"
    if why == "busy":
        return f"⚠️ Код подошёл, но пользователей {_n(peer)} поднять не удалось — админ этого сервера получил причину"
    return f"ℹ️ {_n(peer)} снова на связи — переезд не нужен"


