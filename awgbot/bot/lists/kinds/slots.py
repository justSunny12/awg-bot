"""slots.py — виды слотов шлюзов (основной бот, админ): все подсети слота,
связь подсетей слота и всех слотов, расхождение конфигурации целиком."""
from __future__ import annotations

from awgbot.bot import ui
from awgbot.bot.texts import lists as tl
from awgbot.bot.callbacks import GwSlotCB, SetCB
from awgbot.bot.handlers.common import call
from awgbot.domain.services import ServiceError

from .. import Kind, View, register

ADMIN = ("admin",)


def _gone() -> View:
    return View(title="", empty=tl.SLOT_GONE, back=ui.back(SetCB(sec="rt")))


def _slot_name(st: dict) -> str:
    """Имя шлюза сырым (экранирует текст вида): имя устройства слота."""
    dev, gw = st.get("device"), st.get("gateway")
    return dev.name if dev is not None else f"шлюз {gw.id}"


def _card(slot: int) -> tuple:
    return ui.back(GwSlotCB(action="card", slot=slot))


async def _state(services, slot: int):
    try:
        return await call(services.gateway_screen_state, slot, lazy_ping=False)
    except ServiceError:
        return None


async def gwnets(services, ref: int, key: str, chat_id: int) -> View | None:
    """Все подсети слота парами «настоящая · в туннеле» и те, что в таблицу не попали."""
    from awgbot.bot.texts.routing.card import lan_issue_line
    from awgbot.bot.texts.routing.slots import lan_pair
    st = await _state(services, ref)
    if st is None:
        return _gone()
    name = _slot_name(st)
    nets = list(st["gateway"].lan_subnets or [])
    tw = dict(st.get("lan_twins") or [])
    rows = [lan_pair(n, tw) for n in nets]
    rows += [x for x in (lan_issue_line(it) for it in (st.get("lan_space") or {}).get("issues") or []) if x]
    return View(title=tl.gwnets_title(name), rows=rows, count=str(len(nets)) if nets else "",
                empty=tl.gwnets_empty(name), back=_card(ref))


async def gwpeers(services, ref: int, key: str, chat_id: int) -> View | None:
    """Соседи слота по связи подсетей: имя-ссылка, подпись, адреса в туннеле; подсети,
    которых на этом шлюзе нет, — строками ⚠️."""
    from awgbot.bot.texts.fmt import _e
    st = await _state(services, ref)
    if st is None:
        return _gone()
    name = _slot_name(st)
    peers = await call(_peer_rows, services, ref) if st.get("peer_nets") else []
    rows = list(peers)
    pn = (st.get("channel") or {}).get("peer_nets") or {}
    if isinstance(pn, dict) and pn and not pn.get("ok"):
        rows += [f"⚠️ На шлюзе нет <code>{_e(str(n))}</code>" for n in pn.get("missing") or []]
    return View(title=tl.gwpeers_title(name), rows=rows,
                count=tl.gateways_count(len(peers)) if peers else "",
                empty=tl.gwpeers_empty(name), back=_card(ref))


def _peer_rows(services, slot: int) -> list[str]:
    from awgbot.bot.texts.fmt import _e
    out = []
    for g in services.gateway_transit_slots():
        if g.id == int(slot):
            continue
        twins = ", ".join(f"<code>{_e(t)}</code>" for _r, t in services.lan_twins(g))
        label = f" · {_e(g.label)}" if g.label else ""
        out.append(services._link(f"gw-{g.id}", services._gw_name(g)) + label + (f": {twins}" if twins else ""))
    return out


async def peernets(services, ref: int, key: str, chat_id: int) -> View | None:
    """Строка на слот с VPN-транзитом: имя и все адреса в туннеле."""
    from awgbot.bot.texts.fmt import _e
    info = await call(services.gateway_peer_nets_info)
    if info.get("state") != "ok":
        return View(title=tl.PEERNETS_TITLE, empty=tl.PEERNETS_EMPTY, back=ui.back(SetCB(sec="rt")))
    named = info.get("pairs_named") or []
    rows = [f"{_e(n)}" + (f" · {_e(label)}" if label else "") + (": " + ", ".join(f"<code>{_e(x)}</code>" for x in nets)
                                                                if nets else "")
            for n, label, nets in named]
    return View(title=tl.PEERNETS_TITLE, rows=rows, count=tl.gateways_count(len(named)),
                empty=tl.PEERNETS_EMPTY, back=ui.back(SetCB(sec="rt")))


async def drift(services, ref: int, key: str, chat_id: int) -> View | None:
    """Расхождение конфигурации слота целиком (без обрезки значений и групп)."""
    try:
        d = await call(services.gw_drift_view, ref)
    except ServiceError:
        return _gone()
    lines = list(d["lines"])
    # подписи групп устройств админа («На шлюзе не помечены …:») — строки без « · » в начале, с двоеточием в конце
    labels = tuple(i for i, x in enumerate(lines) if x.endswith(":") and not x.startswith(" · "))
    return View(title=tl.drift_title(d["name"]), rows=lines,
                count=tl.drift_count(int(d["n"])) if d["n"] else "",
                empty=tl.drift_empty(d["name"]), back=_card(ref), labels=labels)


register(Kind("gwnets", "main", ADMIN, gwnets))
register(Kind("gwpeers", "main", ADMIN, gwpeers))
register(Kind("peernets", "main", ADMIN, peernets))
register(Kind("drift", "main", ADMIN, drift))
