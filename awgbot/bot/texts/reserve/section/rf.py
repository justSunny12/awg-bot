"""rf.py — часть пакета section (см. __init__): РФ-доступ через второй сервер —
строки карточки (Т-РТ1…Т-РТ3) и диалог выключения «Мои шлюзы для резерва»
(Т-РТ4)."""
from __future__ import annotations

from ...fmt import _e
from ..common import _n, _when, _why


# ── РФ-доступ через второй сервер: строки карточки (Т-РТ1…Т-РТ3, на вычитку) ─

def rfpeer_mine_lines(r: dict, peer: str) -> list[str]:
    """Блок «📤»: мой РФ-доступ через шлюзы второго сервера или почему запасного пути нет."""
    if not r:
        return []
    name = _n(peer)
    if r.get("use"):
        since = _when((r.get("use") or {}).get("since") or "")
        return [f"└ 🇷🇺 РФ-доступ — через шлюзы {name}" + (f" с {_e(since)}" if since else "") + ": свои шлюзы не отвечают"]
    if r.get("offer_missing"):
        mark = "☑️" if str(r.get("offer_missing_code") or "off") == "off" else "⚠️"   # ☑️ — выключенная настройка; невыполненное условие — ⚠️
        return [f"└ {mark} Запасного РФ-доступа через шлюзы {name} нет: {_why(r['offer_missing'])}"]
    if r.get("probe_down") is not None:
        since = _when(r.get("probe_down") or "")
        return [f"└ ⚠️ Шлюзы {name} для РФ-доступа не отвечают" + (f" с {_e(since)}" if since else "") + " — запасного пути сейчас нет"]
    return []


def rfpeer_peer_lines(r: dict, peer: str) -> list[str]:
    """Блок «📥»: РФ-доступ пользователей второго сервера через мои шлюзы (Т-РТ3)."""
    ep = (r or {}).get("peer_use") or {}
    if not ep:
        return []
    from awgbot.util.textfmt.base import _updown
    since = _when(ep.get("since") or "")
    up, dn = int(ep.get("up") or 0), int(ep.get("dn") or 0)
    vol = f" · {_updown(up, dn)}" if up or dn else ""        # вид как на главной; нули не выводим
    return [f"└ 🇷🇺 РФ-доступ пользователей {_n(peer)} идёт через мои шлюзы" + (f" с {_e(since)}" if since else "") + vol]


def rfmine_off_dialog(peer: str, since: str) -> str:
    """Т-РТ4: выключить «Мои шлюзы для резерва», пока второй сервер ходит через мои шлюзы."""
    at = _when(since)
    return (f"🇷🇺 <b>Выключить «Мои шлюзы для резерва»?</b>\n\n"
            f"Сейчас через шлюзы этого сервера идёт РФ-доступ пользователей {_n(peer)}" + (f" — с {_e(at)}" if at else "")
            + f", шлюзы {_n(peer)} не отвечают. После выключения российские сервисы у них откроются с зарубежного адреса "
            "и могут ругаться")
