"""
compat.py — часть пакета reserve (см. __init__): возможности пары и матрица
направлений.

Возможности уходят в `hello`/`st`: по ним сторона понимает, что умеет вторая,
без разбора номеров версий. Направление «мои пользователи → к резерву» готово, когда
резерв умеет прочитать мой пакет (старший общий формат `pkg*`), поднять зону,
бот основы и голосование, его выпуск не младше моего и линия совместимости
одна, поколение AWG одно, порты пользователей не пересекаются, у меня есть шлюз с
`rsv1`, обе стороны готовы (тумблер) и пакет у резерва свежий. Одинаковые
версии — оба направления; разные — «новый принимает старого». Списка
совместимых версий нет: правило вычисляется.
"""
from __future__ import annotations

from awgbot.core import config
from awgbot.core.compat import COMPAT_FLOOR, version_key

# возможности основного бота в паре (контракт канала, не версия)
# seal1 — ключ пакета при переезде кнопкой принимается только конвертом к разовому ключу
# процесса открытия (`go_req` → `open_eph` → `go{sealed}`)
# quo1 — кворум: состав линии, зонды, взгляды и решения по большинству (концепт «кворум», 4.2.0)
CAPS: tuple[str, ...] = ("pair1", "pkg2", "zone1", "guest1", "keys1", "peer1", "hook1", "seal1", "quo1")
# журнал данных (util/rsvjrn): события вместо пакета на каждое изменение, снимок `pkg3`, возврат
# без слияния — объявляется, только если у SQLite есть JSON-функции и захват событий встал
JOURNAL_CAPS: tuple[str, ...] = ("pkg3", "jrn1")
PKG_FORMATS: tuple[str, ...] = ("pkg2", "pkg3")    # по старшинству: последний — самый новый


_JSON_OK: dict = {}


def journal_capable() -> bool:
    """Умеет ли этот процесс журнал: JSON-функции SQLite есть, захват не отказал.
    Проба JSON — один раз за процесс (зовётся из `st` раз в 5 с)."""
    import sqlite3
    from awgbot.util import rsvjrn
    from awgbot.util.rsvjrn import triggers
    if triggers.capture_failed():
        return False
    probe = rsvjrn.json_ok                                   # ключ — сама проба (подмена в тестах — своя запись)
    if probe not in _JSON_OK:
        con = sqlite3.connect(":memory:")
        try:
            _JSON_OK[probe] = probe(con)
        finally:
            con.close()
    return bool(_JSON_OK[probe])


def tg_path_capable() -> bool:
    """Оболочка зоны с veth `awggx` стоит (state.json: tg=up) — агенты шлюзов
    основы могут выходить в Telegram через мою зону (`rsvtg1`, концепт «кворум» §4.11)."""
    try:
        from awgbot.infra import rsvzone
        return str(rsvzone.state().get("tg") or "") == "up"
    except Exception:                                         # noqa: BLE001
        return False


def caps() -> list[str]:
    """Возможности этого процесса в паре: постоянные, если умеет — журнал, если
    оболочка зоны с `awggx` стоит — выход агентов в Telegram."""
    return list(CAPS) + (list(JOURNAL_CAPS) if journal_capable() else []) + (["rsvtg1"] if tg_path_capable() else [])


def journal_pair(mine: list[str], theirs: list[str]) -> bool:
    """Направление идёт журналом, когда `jrn1` у обеих сторон."""
    return "jrn1" in (mine or []) and "jrn1" in (theirs or [])


_NEEDED_TO_HOST: tuple[str, ...] = ("zone1", "guest1", "quo1")   # quo1 — переезд решает кворум: без него направления не готовы


def pkg_format(mine: list[str], theirs: list[str]) -> str:
    """Старший общий формат пакета; пусто — общего нет."""
    common = [f for f in PKG_FORMATS if f in mine and f in theirs]
    return common[-1] if common else ""


def side(name: str, version: str, caps: list[str], gen: int, ports: list[int], ready: bool,
         has_gateway: bool, floor: str = COMPAT_FLOOR, zone: str = "") -> dict:
    """Описание стороны для матрицы — одно и то же для своей и чужой. `zone` —
    почему сторона не может принять пользователей (нет порта вебхука, dnsmasq…);
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
                   caps=caps(), gen=gen, ports=ports, ready=ready, has_gateway=has_gateway, zone=zone), "me": True}


def _not_ready(s: dict) -> str:
    """Причина «готовность выключена» — с указанием стороны; своя сторона — по
    признаку `me` (имена серверов могут совпадать)."""
    return "здесь выключено «Готов быть резервом»" if s.get("me") else f"у «{s.get('name') or 'резерв'}» выключено «Готов быть резервом»"


def direction_ready(src: dict, dst: dict, *, pkg_fresh: bool = True) -> tuple[bool, str]:
    """Готово ли направление «пользователи src → на dst». (True, "") или (False, причина
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
    if missing == ["quo1"]:
        return False, f"«{dst.get('name') or 'резерв'}» без кворума (ниже v4.2.0) — нужен выпуск новее"
    if missing:
        return False, f"«{dst.get('name') or 'резерв'}» не умеет принимать пользователей — нужен выпуск новее"
    if "quo1" not in src.get("caps", []):                    # переезд решает кворум: без quo1 у любой стороны направлений нет
        return False, f"«{src.get('name') or 'резерв'}» без кворума (ниже v4.2.0) — нужен выпуск новее"
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
        return False, f"порт AWG {clash[0]} занят на обоих серверах"
    if not pkg_fresh:
        return False, "данные для переезда у резерва устарели"
    return True, ""
