"""move.py — часть пакета section (см. __init__): переезд и возврат кнопкой —
перевод на резерв и его ход (Т-Р10), возврат с резерва у основы (Т-Р11б),
возврат без резерва (С9) и возврат пользователей к основе у резерва (Т-Р12)."""
from __future__ import annotations

from ...fmt import _e
from ..common import _n, _when


# ── Т-Р10: переезд кнопкой ───────────────────────────────────────────────────

def move_dialog(d: dict) -> str:
    name, record, target = _n(d.get("name") or "резерв"), _e(d.get("record") or ""), _e(d.get("target") or "")
    if d.get("manual"):
        first = f"Смени у провайдера A-запись <code>{record}</code> на <code>{target}</code> — бот увидит смену сам"
    else:
        first = f"Запись <code>{record}</code> переключится на <code>{target}</code> сама"
    return (f"🔀 <b>Перевести пользователей на {name}?</b>\n\n"
            f"{first}, администратор сервера {name} получит уведомление. Активные устройства перейдут при "
            f"переподключении VPN; новые подключения сразу пойдут через {name}.\n"
            "Твои шлюзы продолжат работать с резервным сервером.\n\n"
            "Функционал бота на резерве ограничен: настройки бота, шлюзов, бэкапов и обновлений будут недоступны.\n\n"
            f"Вернуться обратно можно в любой момент, пока {name} на связи — «↩️ Вернуть пользователей»; изменения "
            "данных за время нахождения на резерве будут сохранены и перенесены."
            + (f"\n\nЭтот сервер сейчас видят {int(d['quorum'].get('y') or 0)} из {int(d['quorum'].get('v') or 0)}."
               if d.get("quorum") else ""))                       # Т-К18


def move_live(d: dict) -> str:
    """Т-Р10б: ход перевода в живом сообщении; итоги — по состоянию."""
    name = _n(d.get("name") or "резерв")
    state, step = str(d.get("state") or ""), str(d.get("step") or "")
    if state == "away":
        there, here = int(d.get("there") or 0), int(d.get("online") or 0)
        tail = ", ".join(x for x in ((f"здесь ещё {here}" if here else ""), (f"там {there}" if there else "")) if x)
        return f"✅ Пользователи и бот — на {name}: устройства переходят при переподключении" + (f" — {tail}" if tail else "")
    if state == "home" and d.get("reason"):
        return f"⚠️ Перевод не состоялся: {_e(d['reason'])}"
    if state != "leaving":
        return f"ℹ️ Перевод пользователей на {name} завершён"
    order = ["pkg", "eph", "up", "record", "release", "released"]
    pos = order.index(step) + 1 if step in order else 1      # 1 — пакет, 2 — ключ, 3 — резерв поднимает, 4 — запись, 5 — бот
    def mark(i):
        return "✅" if i < pos else "⏳"
    record, target = _e(d.get("record") or ""), _e(d.get("target") or "")
    lines = [f"⏳ <b>Перевод пользователей на {name}</b>", "",
             f"{mark(1)} данные для переезда у {name}" + (f" — от {_e(_when(d.get('pkg_at') or ''))}" if d.get("pkg_at") else ""),
             "✅ зашифрованный ключ переезда передан" if pos > 2 else f"{mark(2)} передаю зашифрованный ключ переезда…",
             f"{mark(3)} {name} поднял профили",
             f"{mark(4)} " + (f"запись <code>{record}</code> → <code>{target}</code>" if pos > 4
                               else (f"смени запись <code>{record}</code> на <code>{target}</code> у провайдера — жду…"
                                     if d.get("manual") else f"жду смену DNS-записи <code>{record}</code> → <code>{target}</code>…")),
             f"{mark(5)} передаю бот на {name}…" if pos < 6 else f"✅ бот передан на {name}"]
    return "\n".join(lines)


def return_dialog(d: dict) -> str:
    """Т-Р11б: вернуть пользователей с резерва (у основы, рычаг «по кнопке»)."""
    name = _n(d.get("name") or "резерв")
    rec = ""
    if d.get("record") and d.get("own"):
        rec = (f"; смени у провайдера запись <code>{_e(d['record'])}</code> на <code>{_e(d['own'])}</code> сам — бот увидит смену"
               if d.get("manual") else f"; запись <code>{_e(d['record'])}</code> вернётся на <code>{_e(d['own'])}</code>")
    return (f"↩️ <b>Вернуть пользователей с {name}?</b>\n\n"
            f"Сервер в строю. Бот вернётся сюда, всё, что изменилось на {name}, — тоже{rec}. Устройства, "
            f"которые ещё ходят через {name}, он будет пересылать сюда, пока они не переподключатся, — у каждого "
            "связь прервётся на секунды")


# ── Т-Р12: вернуть пользователей к основе (у резерва) ────────────────────────

def force_dialog(d: dict) -> str:
    """С9: вернуть пользователей без резерва."""
    name = _n(str(d.get("name") or "резерв"))
    since = _when(str(d.get("since") or ""))
    rec = ""
    if d.get("record") and d.get("own"):
        rec = (f"смени у провайдера запись <code>{_e(d['record'])}</code> на <code>{_e(d['own'])}</code> сам"
               if d.get("manual") else f"запись <code>{_e(d['record'])}</code> вернётся на <code>{_e(d['own'])}</code>")
    return (f"⚠️ <b>Вернуть пользователей без {name}?</b>\n\n"
            f"{name} не на связи" + (f" с {since}" if since else "") + ". Этот сервер заберёт Telegram и примет устройства сам"
            + (f"; {rec}" if rec else "") + ". "
            + (f"Изменения от бота на {name} за это время придут, когда он выйдет на связь; если здесь успеешь внести "
               f"конфликтующие со снимком с {name} правки — останутся они, список пришлю." if d.get("jrn")   # Т-Ж7
               else f"Изменения с {name} за это время применятся, когда он выйдет на связь.")
            # одно поведение — одна фраза в обеих ветках (вычитка 29.10.2026)
            + f"\nПока {name} не на связи, пересылки устройств с него нет — устройствам, которые ещё ходят через него, "
            "необходимо переподключиться к VPN" + _force_quorum_line(d))


def _force_quorum_line(d: dict) -> str:
    """Т-К19: сколько голосующих видят бот на резерве."""
    q = d.get("quorum") or {}
    if not q:
        return ""
    name = _n(str(d.get("name") or "резерв"))
    y, v = int(q.get("y") or 0), int(q.get("v") or 0)
    if not y:
        return f"\nБот на {name} не видит никто."
    return f"\nБот на {name} видят {y} из {v} — если он жив, правки, сделанные там после этой минуты, придут хвостом."


def evict_dialog(name: str) -> str:
    n = _n(name)
    return (f"↩️ <b>Вернуть пользователей и бот {n} к нему?</b>\n\n"
            f"{n} на связи — он примет свой бот и данные, сам переключит запись на себя. Пока устройства {n} не "
            f"переподключатся, этот сервер пересылает их трафик к нему; данные {n} здесь снимаются сразу. {n} получит "
            "уведомление")
