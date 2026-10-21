"""
compat.py — часть пакета reserve (см. __init__): возможности пары и матрица
направлений.

Возможности уходят в `hello`/`st`: по ним сторона понимает, что умеет вторая,
без разбора номеров версий. Направление «мои клиенты → к резерву» готово, когда
резерв умеет прочитать мой пакет (старший общий формат `pkg*`), поднять зону,
бот основы и голосование, его выпуск не младше моего и линия совместимости
одна, поколение AWG одно, порты клиентов не пересекаются, у меня есть шлюз с
`rsv1`, обе стороны готовы (тумблер) и пакет у резерва свежий. Одинаковые
версии — оба направления; разные — «новый принимает старого». Списка
совместимых версий нет: правило вычисляется.
"""
from __future__ import annotations

from awgbot.core import config
from awgbot.core.compat import COMPAT_FLOOR, version_key

# возможности основного бота в паре (контракт канала, не версия)
CAPS: tuple[str, ...] = ("pair1", "pkg2", "zone1", "guest1", "vote1", "keys1", "peer1", "hook1")
PKG_FORMATS: tuple[str, ...] = ("pkg2",)           # по старшинству: последний — самый новый
_NEEDED_TO_HOST: tuple[str, ...] = ("zone1", "guest1", "vote1")


def pkg_format(mine: list[str], theirs: list[str]) -> str:
    """Старший общий формат пакета; пусто — общего нет."""
    common = [f for f in PKG_FORMATS if f in mine and f in theirs]
    return common[-1] if common else ""


def side(name: str, version: str, caps: list[str], gen: int, ports: list[int], ready: bool,
         has_gateway: bool, floor: str = COMPAT_FLOOR, zone: str = "") -> dict:
    """Описание стороны для матрицы — одно и то же для своей и чужой. `zone` —
    почему сторона не может принять клиентов (нет порта вебхука, dnsmasq…);
    пусто — может."""
    return {"name": name, "version": str(version or ""), "caps": list(caps or []), "gen": int(gen or 0),
            "ports": [int(p) for p in (ports or [])], "ready": bool(ready), "gw": bool(has_gateway),
            "floor": str(floor or ""), "zone": str(zone or "")[:120]}


def zone_why() -> str:
    """Чего не хватает гостевой зоне на этом сервере — по плану скрипта зоны;
    пусто — всё есть. Кэш на 10 минут: план — вызов скрипта."""
    import time
    from awgbot.infra import rsvzone
    cache = zone_why.__dict__
    now = time.monotonic()
    if cache.get("at") and now - cache["at"] < 600:
        return str(cache.get("why") or "")
    plan = rsvzone.plan()
    why = ""
    if plan.get("error"):
        why = "план зоны не прочитан"
    elif str(plan.get("dnsmasq") or "yes") == "no":
        why = "на сервере нет dnsmasq — резолвер зоны не поднимется"
    elif str(plan.get("python") or "yes") == "no":
        why = "на сервере нет python для состояния зоны"
    elif str(plan.get("webhook_port") or "") in ("", "none"):
        why = "нет свободного порта для вебхука (443, 80, 88, 8443 заняты)"
    cache.update(at=now, why=why)
    return why


def my_side(ready: bool, has_gateway: bool, ports: list[int]) -> dict:
    from awgbot.infra import awglock
    try:
        gen = int(awglock.generation())
    except Exception:                                     # noqa: BLE001 — манифеста нет (стенд без модуля)
        gen = 0
    try:
        zone = zone_why() if ready else ""
    except Exception:                                     # noqa: BLE001 — план недоступен
        zone = ""
    return {**side(name=str(config.SERVER_NAME or ""), version=str(config.INSTALLED_VERSION).lstrip("v"),
                   caps=list(CAPS), gen=gen, ports=ports, ready=ready, has_gateway=has_gateway, zone=zone), "me": True}


def _not_ready(s: dict) -> str:
    """Причина «готовность выключена» — с указанием стороны; своя сторона — по
    признаку `me` (имена серверов могут совпадать)."""
    return "здесь выключено «Готов быть резервом»" if s.get("me") else f"у «{s.get('name') or 'резерв'}» выключено «Готов быть резервом»"


def direction_ready(src: dict, dst: dict, *, pkg_fresh: bool = True) -> tuple[bool, str]:
    """Готово ли направление «клиенты src → на dst». (True, "") или (False, причина
    для строки карточки)."""
    vs, vd = version_key(src.get("version", "")), version_key(dst.get("version", ""))
    if vs is None or vd is None:
        return False, "версия второго сервера не прочитана"
    if not src.get("ready"):
        return False, _not_ready(src)
    if not dst.get("ready"):
        return False, _not_ready(dst)
    if not pkg_format(src.get("caps", []), dst.get("caps", [])):
        return False, f"«{dst.get('name') or 'резерв'}» не читает формат данных переезда — нужен выпуск новее"
    missing = [c for c in _NEEDED_TO_HOST if c not in dst.get("caps", [])]
    if missing:
        return False, f"«{dst.get('name') or 'резерв'}» не умеет принимать клиентов — нужен выпуск новее"
    if dst.get("zone"):
        return False, (f"здесь: {dst['zone']}" if dst.get("me") else f"у «{dst.get('name') or 'резерв'}»: {dst['zone']}")
    if not src.get("gw"):
        return False, "нужен хотя бы один шлюз с агентом новой линии"
    if vd[:3] < vs[:3]:
        return False, (f"у «{dst.get('name') or 'резерв'}» v{dst.get('version')}, здесь v{src.get('version')} — "
                       "нужен резерв не младше")
    if src.get("floor") != dst.get("floor"):
        return False, "у серверов разные линии совместимости"
    if src.get("gen") and dst.get("gen") and src["gen"] != dst["gen"]:
        return False, "у серверов разные поколения AmneziaWG"
    clash = sorted(set(src.get("ports", [])) & set(dst.get("ports", [])))
    if clash:
        return False, f"порт клиентов {clash[0]} занят у обеих сторон"
    if not pkg_fresh:
        return False, "данные для переезда у резерва устарели"
    return True, ""
