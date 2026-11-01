"""reserve_gw.py — часть пакета notices (см. __init__): шлюзы и резерв — связь
шлюзов с резервом (Т-РУ18, админу основы) и уведомления агента шлюза (Т-РА3,
Т-РА5, Т-К24). Правила — как у reserve."""
from __future__ import annotations

from .reserve_fmt import _e, _n, _when


# ── шлюзы: связь с резервом (этап 5) ────────────────────────────────────────

def gateway_reserve_down(gw: str, reserve: str, since: str = "") -> str:
    """Т-РУ18: шлюз не на связи с резервом."""
    at = _when(since)
    return (f"⚠️ Шлюз {_n(gw)} не на связи с резервом {_n(reserve)}" + (f" с {at}" if at else "")
            + " — пока так, при падении сервера он не голосует")


def gateways_reserve_all_down(reserve: str, since: str = "") -> str:
    at = _when(since)
    return (f"⚠️ Ни один шлюз не на связи с резервом {_n(reserve)}" + (f" с {at}" if at else "")
            + " — если сервер упадёт, пользователи переедут только по твоему аварийному коду")


def gateway_reserve_up(gw: str, reserve: str) -> str:
    return f"🟢 Шлюз {_n(gw)} снова на связи с резервом {_n(reserve)}"


# ── агент шлюза ─────────────────────────────────────────────────────────────

def gateway_moved(reserve: str, case: str, since: str, record: str = "", ip: str = "", others: dict | None = None) -> str:
    """Т-РА3: пользователи переехали на резерв; case — dead (этот шлюз сервер не видит) | rf (не видит,
    а резерв видит) | seen (Т-РА3б: этому шлюзу сервер отвечает — так решило большинство)."""
    at = _when(since)
    rec = f": запись <code>{_e(record)}</code> → <code>{_e(ip)}</code>" if record and ip else ""
    if case == "rf":
        return (f"🚫 Сервер AWG недоступен отсюда с {at}, а {_n(reserve)} его видит — похоже, сервер закрыт из РФ; "
                f"пользователи переехали на {_n(reserve)}{rec}")
    if case == "seen":
        return (f"🟡 Пользователи переехали на резерв {_n(reserve)} — так решило большинство голосующих; "
                f"этому шлюзу сервер AWG отвечает")
    return f"🔴 Сервер AWG не отвечает{(' с ' + at) if since else ''} — пользователи переехали на резерв {_n(reserve)}{rec}"


def gateway_tg_via(primary: str, reserve: str, since: str) -> str:
    """Т-К24 (админу шлюза, без звука, один раз на эпизод обхода): аплинк до своего
    сервера AWG молчит, бот шлюза на связи через второй сервер; отбоя нет — строка панели пропадает сама."""
    return f"📨 Аплинк до {_n(primary)} молчит с {_when(since)} — бот шлюза на связи через {_n(reserve) if reserve else 'резерв'}"


def gateway_move_failed(reserve: str, since: str, reason: str) -> str:
    """Т-РА5."""
    return f"⚠️ Сервер AWG не отвечает с {_when(since)}, переезд на {_n(reserve)} не состоялся: {_e(reason)}"
