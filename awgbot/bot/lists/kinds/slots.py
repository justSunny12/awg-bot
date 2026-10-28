"""slots.py — виды слотов шлюзов (основной бот, админ): все подсети слота,
связь подсетей слота и всех слотов, расхождение конфигурации целиком."""
from __future__ import annotations

from awgbot.bot import ui
from awgbot.bot.texts import lists as tl
from awgbot.bot.texts.routing.slots import slot_raw_name
from awgbot.bot.callbacks import GwSlotCB, SetCB
from awgbot.bot.handlers.common import call
from awgbot.domain.services import ServiceError

from .. import Kind, View, register

ADMIN = ("admin",)


def _gone() -> View:
    return View(title="", empty=tl.SLOT_GONE, back=ui.back(SetCB(sec="rt")))


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
    name = slot_raw_name(st)
    nets = list(st["gateway"].lan_subnets or [])
    tw = dict(st.get("lan_twins") or [])
    issues = list((st.get("lan_space") or {}).get("issues") or [])
    bad = {str(it.get("net") or "") for it in issues}
    # подсети вне таблицы — первыми и один раз (строкой с причиной, а не ещё и парой «вне туннеля»):
    # к ним ведёт «весь список» из «⚠️ Ещё N подсети …», и править надо их
    rows = [x for x in (lan_issue_line(it) for it in issues) if x]
    rows += [lan_pair(n, tw) for n in nets if n not in bad]
    return View(title=tl.gwnets_title(name), rows=rows, count=str(len(nets)) if nets else "",
                empty=tl.gwnets_empty(name), back=_card(ref))


async def gwpeers(services, ref: int, key: str, chat_id: int) -> View | None:
    """Соседи слота по связи подсетей: имя-ссылка, подпись, адреса в туннеле; подсети,
    которых на этом шлюзе нет, — строками ⚠️. Пусто — по причине: адреса в
    туннеле выключены, у слота выключен VPN-транзит, слотов с транзитом меньше двух."""
    from awgbot.bot.texts.fmt import _e
    st = await _state(services, ref)
    if st is None:
        return _gone()
    name = slot_raw_name(st)
    peers = [_peer_row(p) for p in await call(services.gateway_peer_cards, ref)] if st.get("peer_nets") else []
    rows = list(peers)
    pn = (st.get("channel") or {}).get("peer_nets") or {}
    if isinstance(pn, dict) and pn and not pn.get("ok"):
        rows += [f"⚠️ На шлюзе нет <code>{_e(str(n))}</code>" for n in pn.get("missing") or []]
    empty = tl.gwpeers_empty(name, await call(services.gateway_peers_off, ref) if not rows else "")
    return View(title=tl.gwpeers_title(name), rows=rows,
                count=tl.gateways_count(len(peers)) if peers else "", empty=empty, back=_card(ref))


def _peer_row(peer: dict) -> str:
    """«<a>NASPi</a> · дача: <code>10.240.1.0/24, …</code>» — адреса одной
    сущностью: на 64 адреса — одна, а не 64 (предел сущностей в сообщении)."""
    from awgbot.bot.texts.fmt import _e
    twins = ", ".join(_e(t) for t in peer.get("twins") or [])
    label = f" · {_e(peer['label'])}" if peer.get("label") else ""
    return str(peer["link"]) + label + (f": <code>{twins}</code>" if twins else "")


async def peernets(services, ref: int, key: str, chat_id: int) -> View | None:
    """Строка на слот с VPN-транзитом: имя и все адреса в туннеле."""
    from awgbot.bot.texts.fmt import _e
    info = await call(services.gateway_peer_nets_info)
    if info.get("state") != "ok":
        empty = tl.PEERNETS_OFF if info.get("state") == "off" else tl.PEERNETS_EMPTY
        return View(title=tl.PEERNETS_TITLE, empty=empty, back=ui.back(SetCB(sec="rt")))
    named = info.get("pairs_named") or []
    # адреса слота одной сущностью: 15 слотов по 64 адреса — 15 сущностей, а не 960
    rows = [f"{_e(n)}" + (f" · {_e(label)}" if label else "") + (f": <code>{', '.join(_e(x) for x in nets)}</code>"
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
