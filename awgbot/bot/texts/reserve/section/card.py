"""card.py — часть пакета section (см. __init__): карточка резерва (Т-Р9) —
статус, готовность, направления «📤»/«📥», DNS-запись, итог трафика. Блоки
голосов и РФ-доступа карточка берёт из votes и rf.

Предел 4096: причины и ошибки из состояния — через common._why (обрезка до
WHY_MAX), шлюзы без связи с резервом — первые GW_DOWN_SHOWN и «… и ещё N»,
голосующие — votes.VOTERS_SHOWN и « [+N]»; сторож — tests/unit/test_reserve_card_length."""
from __future__ import annotations

from awgbot.util.textfmt.base import details

from ...fmt import _e
from ..c9wait import card_line as c9_card_line
from ..pult import card_lines as pult_card_lines, peer_card_line as pult_peer_line
from ..common import _date, _n, _when, _why
from .rf import rfpeer_mine_lines, rfpeer_peer_lines
from .votes import quorum_mine_lines, quorum_peer_lines


LOW_MEM_MB = 400                     # меньше — строка «мало свободной памяти» (решение по в. 56)
GW_DOWN_SHOWN = 3                    # шлюзов «не на связи с резервом» поимённо; дальше — «… и ещё N» (предел 4096)
# приём сорвался после смены записи процессом открытия (inbound_keep): что не доделано — что делаю
_KEEP_LINES = {"record": ("DNS-запись не подтвердилась", "проверяю раз в минуту"),
               "serve": ("вход вебхука в зону не настроен", "повторяю раз в минуту"),
               "relaunch": ("клона нет", "перезапускаю открытие раз в минуту")}


def _move_step(step: str) -> str:
    return {"pkg": "обновляю данные для переезда", "eph": "передаю зашифрованный ключ переезда", "up": "жду подъёма профилей",
            "record": "жду смену DNS-записи", "release": "передаю бот", "released": "передаю бот"}.get(step, "идёт переезд")


def card_text(d: dict, bot_username: str = "") -> str:
    """Карточка резерва (этап пары: статус, готовность, направления, итог трафика);
    bot_username — для ссылок на экраны (плашка С9 «не ждать?»)."""
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
    if d.get("liveness_problem"):
        lines.append("⚠️ " + _e(d["liveness_problem"]))
    if not d.get("online"):
        since = d.get("down_since") or ""
        lines.append(f"🔴 Нет связи с {_n(name)}" + (f" с {_e(_when(since))}" if since else ""))
    mem = int(d.get("mem_free") or 0)
    if mem and mem < LOW_MEM_MB:
        lines.append(f"⚠️ У {_n(name)} мало свободной памяти ({mem} МБ) — твоих пользователей он может не потянуть")
    lines.append("")
    out_ok, out_why = d.get("dir_out") or (False, "")
    in_ok, in_why = d.get("dir_in") or (False, "")
    out, inn = str(d.get("out") or "home"), str(d.get("inn") or "idle")
    if out == "leaving":
        lines.append(f"📤 Твои пользователи: переезжают на {_n(name)} — {_e(_move_step(d.get('out_step') or ''))}")
    elif out == "away":
        here, there = int(d.get("here") or 0), int(d.get("there") or 0)
        tail = f" · здесь ещё {here}" + (f", там {there}" if there else "") if here or there else ""
        lines.append(f"📤 Твои пользователи: на {_n(name)} с {_e(_when(d.get('out_since') or ''))}{tail}")
        fol = d.get("jrn_follow") or {}
        if fol:                                               # Т-Ж1 / Т-Ж2
            # вычитка 29.10.2026: с датой — событие могло быть вчера, одно время обманчиво
            at = f" · актуальны на {_e(_date(fol['at']))}" if fol.get("at") else ""
            lines.append(f"└ 🟡 данные здесь догоняют бот на {_n(name)}{at}" if fol.get("lag")
                         else f"└ данные здесь следуют за ботом на {_n(name)}{at}")
        lines += pult_card_lines(d.get("pult") or {}, name)  # Telegram бота здесь, пользователи там
    elif out == "returning":
        lines.append(f"📤 Твои пользователи: возвращаются с {_n(name)}")
    else:
        lines.append("📤 Твои пользователи: на этом сервере")
        if out_ok:
            sent = d.get("pkg_sent_at") or ""
            tail = f"данные для переезда у {_n(name)} от {_e(_when(sent))}" if sent else f"данные для переезда у {_n(name)} ещё не отправлены"
            lines.append("└ " + tail)
            if d.get("pkg_error"):
                lines.append(f"└ ⚠️ данные для переезда не собираются: {_why(d['pkg_error'])}")
        else:
            lines.append("└ ⚠️ " + _why(out_why))
        if d.get("c9_wait"):                                  # правки бота на резерве после передачи ещё ждём
            lines.append(c9_card_line(d["c9_wait"], name, bot_username))
        gw_down = [g for g in d.get("gw_links") or [] if g.get("rsv1")
                   and (g.get("seen") == "down" or (g.get("seen") == "absent" and g.get("online")))]
        for g in gw_down[:GW_DOWN_SHOWN]:
            since = g.get("since") or ""
            lines.append(f"└ ⚠️ {_n(g['name'])} не на связи с {_n(name)}" + (f" с {_e(_when(since))}" if since else ""))
        if len(gw_down) > GW_DOWN_SHOWN:                      # своей строкой: к времени последнего не клеить
            lines.append(f"└ … и ещё {len(gw_down) - GW_DOWN_SHOWN}")
        lines += quorum_mine_lines(d.get("quorum") or {})
        lines += rfpeer_mine_lines(d.get("rfpeer") or {}, name)
        if d.get("move_why") and out_ok:
            lines.append("└ перевести кнопкой нельзя: " + _why(d["move_why"]))
        if d.get("out_relay_until"):
            lines.append(f"└ пересылка хвоста у {_n(name)} до {_e(_date(d['out_relay_until']))}")
    if inn in ("taking", "serving", "releasing"):
        since = d.get("in_since") or ""
        lines.append(f"📥 Пользователи {_n(name)}: здесь" + (f" с {_e(_when(since))}" if since else "")
                     + (" — принимаю" if inn == "taking" else " — возвращаю" if inn == "releasing" else ""))
        if d.get("peer_pult"):                                # её Telegram у неё (пульт), пользователи здесь
            lines.append(pult_peer_line(name))
        if d.get("in_tg_wait"):                               # VPN здесь, а бот второй стороны Telegram не взял
            lines.append(f"└ ⚠️ бот {_n(name)} здесь не отвечает в Telegram: {_why(d['in_tg_wait'])} — повторяю раз в минуту")
        keep = d.get("in_keep") or {}                         # запись уже здесь, приём сорвался — доделываю
        if keep.get("k") in _KEEP_LINES:
            lines.append(f"└ ⚠️ {_KEEP_LINES[keep['k']][0]}: {_why(keep.get('why') or '')} — {_KEEP_LINES[keep['k']][1]}")
    elif inn == "relay":
        lines.append(f"📥 Пользователи {_n(name)}: возвращаются к нему — пересылка до {_e(_date(d.get('in_relay_until') or ''))}")
    else:
        lines.append(f"📥 Пользователи {_n(name)}: на его сервере")
        if not in_ok and in_why:
            lines.append("└ ⚠️ " + _why(in_why))
        mem = d.get("pkg_mem") or {}
        if mem:
            lines.append(f"└ ⚠️ данные для переезда {_n(name)} — {_e(mem['size'])}: переезду сюда понадобится "
                         f"около {_e(mem['need'])} памяти")
        lines += quorum_peer_lines(d.get("quorum") or {})
        lines += rfpeer_peer_lines(d.get("rfpeer") or {}, name)
        if d.get("await_merge"):
            lines.append(f"└ 🟡 изменения из бота на {_n(name)} придут, когда он выйдет на связь" if d.get("jrn_moved")
                         else f"└ ⏳ жду реплику {_n(name)} — изменения из неё применятся, когда он выйдет на связь")
        if d.get("code_open") and d.get("code_by") == "link":
            lines.append(f"└ 🆘 {_n(name)} не на связи, а голосование о нём не включено — жду аварийный код его админа")
        elif d.get("code_open"):
            lines.append(f"└ 🆘 {_n(name)} не отвечает, голосов для переезда не хватает — жду аварийный код его админа")
        elif d.get("auto_block"):
            lines.append("└ ⚠️ автоматический аварийный переезд невозможен: " + _why(d["auto_block"]))
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
    # Т-Ж8: каким путём идут данные для переезда
    path = (f"Данные для переезда уходят к {_n(name)} по мере изменений, раз в сутки — полный слепок контрольно"
            if d.get("jrn_on") else f"Данные для переезда уходят к {_n(name)} полным слепком после каждого изменения"
            + (f" — версия бота на {_n(name)} не поддерживает журнал правок"
               if (d.get("peer_state") or {}).get("version") and "jrn1" not in ((d.get("peer_state") or {}).get("caps") or [])
               else ""))
    lines += ["", details("▸ подробнее: Свои цифры каждый сервер считает сам; месяц — по часовому поясу своей "
                          f"стороны. Возврат пользователей: {ret}. {path}")]
    return "\n".join(lines)
