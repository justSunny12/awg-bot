"""votes.py — часть пакета section (см. __init__): кворум — блок голосов
карточки резерва (Т-К1…Т-К3, Т-К20): за этот сервер («📤») и за второй («📥»).
Отдельно — самый ветвистый кусок карточки, его тесты бьют сюда напрямую."""
from __future__ import annotations

from ...fmt import _e
from ..common import _n, _when


# ── кворум: блок голосов карточки (Т-К1…Т-К3, Т-К20; на вычитку) ───────────

_W = {"see": "видит", "held": "видит", "uns": "не видит", "sus": "сомневается", "new": "ещё не смотрел", "silent": "молчит"}


def _qn(name: str) -> str:
    """Имя узла кворума: чужой шлюз приходит уже с кавычками («шлюз 3 «Beta»») — не удваивать."""
    return _e(name) if "«" in str(name) else _n(name)


def _quorum_off_why(codes: list, peer: str) -> str:
    for c in codes or []:
        if c == "no_gateways":
            return "у этого сервера нет шлюзов с агентом v4.2.0 — переезд кнопкой и по аварийному коду, автоматического нет"
        if c.startswith("timing:"):
            return "цифры времени не сходятся (reserve.quorum в app.yaml)"
        if c.startswith("overlap:"):
            return f"блок зондов <code>169.254.88.0/24</code> пересекается с <code>{_e(c[8:])}</code>"
        if c.startswith("slot:"):
            return f"«Шлюз {_e(c[5:])}»: номер слота больше 58 — в голосовании не участвует"
        if c == "not_acked":
            return "состав голосующих ждёт подтверждения"
        if c == "keys":
            return "ключи голосования ещё не у всех"
    return "голосующих нет"


def quorum_mine_lines(q: dict) -> list[str]:
    """Строки блока «📤» о голосах за этот сервер."""
    if not q:
        return []
    peer = _n(q.get("peer_name") or "резерв")
    if not q.get("on"):
        return ["└ 🗳 Голосование не включено: " + _quorum_off_why(q.get("why") or [], peer)]
    mine = q.get("mine") or {}
    if not mine:
        return []
    v, t = int(mine.get("v") or 0), int(mine.get("t") or 0)
    out = []
    if q.get("pending"):
        out.append(f"└ 🟡 Состав голосующих меняется: жду подтверждения {peer}")
    if v < 2:                                                 # Т-К1а: голосующий один — второй сервер
        if mine.get("leader_here") and "y" in mine:
            out.append(f"└ 🗳 Этот сервер видят {int(mine.get('y') or 0)} из {v} — голосует только {peer}: "
                       "переезд кнопкой и по аварийному коду, автоматического нет")
        else:
            out.append(f"└ 🗳 Голосует только {peer}: шлюзов с агентом v4.2.0 нет — переезд кнопкой и по аварийному коду, "
                       "автоматического нет")
        return out
    if mine.get("leader_here") and "y" in mine:
        y = int(mine.get("y") or 0)
        if mine.get("role") == "fenced":
            since = _when(mine.get("fenced_since") or "")
            out.append(f"└ ⚠️ Без кворума{(' с ' + _e(since)) if since else ''} — этот сервер видят {y} из {v}: бот не принимает "
                       "решений и не отвечает пользователям, VPN работает")
        elif mine.get("tie_since"):
            since = _when(mine.get("tie_since") or "")
            out.append(f"└ ⚠️ Ничья{(' с ' + _e(since)) if since else ''}: этот сервер видят {y}, не видят {v - y} — сам бот "
                       "пользователей не переведёт")
        else:
            out.append(f"└ 🗳 Этот сервер видят {y} из {v} — пользователи переедут, если не увидят {t}")
    elif not mine.get("leader_here") and "y" in mine:
        out.append(f"└ 🗳 Бот на {peer} видят {int(mine.get('y') or 0)} из {v}"
                   + (" — пользователи вернутся сюда сами, если его не увидят большинство" if mine.get("back") else ""))
    else:
        out.append(f"└ 🗳 Голосующих об этом сервере: {v}, порог переезда — {t}")
    rows = []
    for vt in (mine.get("voters") or [])[:10]:
        w = "silent" if vt.get("silent") and vt.get("w") not in ("uns", "sus") else (vt.get("w") or "silent")
        since = _when(vt.get("since") or "")
        rows.append(f"{_qn(vt.get('name') or vt.get('node') or '?')} — {_W.get(w, w)}" + (f" с {_e(since)}" if since and w in ("uns", "silent") else ""))
    if rows:
        out.append("└ " + " · ".join(rows))
    for names, net in mine.get("correlation") or []:          # Т-К15
        out.append(f"└ ⚠️ {' и '.join(_n(n) for n in names)} выходят в интернет из одной сети <code>{_e(net)}</code> — "
                   "её отказ снимет " + ("оба голоса" if len(names) == 2 else "все их голоса"))
    return out


def quorum_peer_lines(q: dict) -> list[str]:
    """Строки блока «📥» о голосах за второй сервер (мой взгляд и взгляды его шлюзов)."""
    if not q or not q.get("peer"):
        return []
    p = q["peer"]
    peer = _n(q.get("peer_name") or "резерв")
    if not p.get("active"):
        return []
    w = p.get("my_w") or ""
    cand = p.get("cand") or {}
    unseen = [g for g in p.get("gw_views") or [] if g.get("w") == "uns"]
    if cand.get("tie"):                                       # Т-К4 у кандидата: ничья о втором сервере
        since = _when(p.get("tie_since") or "")
        return [f"└ ⚠️ Ничья{(' с ' + _e(since)) if since else ''}: {peer} видят {int(cand.get('see') or 0)}, "
                f"не видят {int(cand.get('no') or 0)} — сам бот пользователей не переведёт"]
    if unseen and cand.get("deadline_at") and w in ("see", "held"):   # Т-К3б: случай 3
        first = min((g.get("since") or "" for g in unseen), default="")
        return [f"└ 🟡 Шлюзы {peer} не видят его{(' с ' + _e(_when(first))) if first else ''}, а этот сервер видит — если так "
                f"останется до {_e(_when(cand['deadline_at']))}, пользователи переедут сюда"]
    if w in ("see", "held"):
        y, v = int(p.get("y") or 0), int(p.get("yv") or p.get("v") or 0)
        others = [g for g in p.get("gw_views") or [] if g.get("w") in ("see", "held")]
        tail = f" — {y} из {v}" if v else ""
        return [f"└ 🗳 {peer} вижу я" + (f" и {', '.join(_qn(g['name']) for g in others)}" if others else "") + tail]
    if w in ("uns", "sus"):
        since = _when(p.get("my_since") or "")
        unseen = [g for g in p.get("gw_views") or [] if g.get("w") == "uns"]
        seen = [g for g in p.get("gw_views") or [] if g.get("w") in ("see", "held")]
        if not unseen and seen:
            return [f"└ 🟡 Этот сервер не видит {peer}{(' с ' + _e(since)) if since else ''}, а его шлюзы видят — похоже, дело в сети этого сервера"]
        n = 1 + len(unseen)
        return [f"└ 🟡 {peer} не видят {n} из {int(p.get('v') or 0)}{(' с ' + _e(since)) if since else ''}" + _cand_tail(p)]
    return []


def _cand_tail(p: dict) -> str:
    """Т-К3: большинство «нет» набрано — когда переедут."""
    cand = p.get("cand") or {}
    if cand.get("deadline_at") and cand.get("own") == "uns":
        return f" — если так останется до {_e(_when(cand['deadline_at']))}, пользователи переедут сюда"
    return ""
