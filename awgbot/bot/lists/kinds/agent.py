"""agent.py — виды агента шлюза (в чате только админ): откуда SSH открыт
изнутри, имена SSH-доступа, которые не резолвятся, адреса в туннеле своей сети."""
from __future__ import annotations

from awgbot.bot import ui
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers.common import call
from awgbot.bot.texts import lists as tl

from .. import Kind, View, register

ADMIN = ("admin",)


def _codes(items) -> list[str]:
    from awgbot.bot.texts.fmt import _e
    return [f"<code>{_e(str(x))}</code>" for x in items]


async def sshfrom(services, ref: int, key: str, chat_id: int) -> View | None:
    """Две группы с подписями: «Из локальной сети», «Из подсетей других шлюзов»;
    группа без подсетей не выводится."""
    st = await call(services.ssh_screen)
    lan, peers = list(st.get("lan") or []), list(st.get("peer_nets") or [])
    rows: list[str] = []
    labels: list[int] = []
    for label, nets in ((tl.SSHFROM_LAN, lan), (tl.SSHFROM_PEERS, peers)):
        if nets:
            labels.append(len(rows))
            rows.append(ui.label(label))
            rows += _codes(nets)
    n = len(lan) + len(peers)
    return View(title=tl.SSHFROM_TITLE, rows=rows, count=tl.subnets_count(n) if n else "", empty=tl.SSHFROM_EMPTY,
                back=ui.back(GwCB(action="ssh")), labels=tuple(labels))


async def sshunres(services, ref: int, key: str, chat_id: int) -> View | None:
    st = await call(services.ssh_screen)
    names, held = list(st.get("unresolved") or []), list(st.get("held") or [])
    rows = _codes(names)
    labels: tuple[int, ...] = ()
    if held:                                         # прошлые адреса этих имён — фильтр держит их
        labels = (len(rows),)
        rows += [ui.label(tl.SSHUNRES_HELD), *_codes(held)]
    return View(title=tl.SSHUNRES_TITLE, rows=rows, count=str(len(names)) if names else "", empty=tl.UNRES_EMPTY,
                back=ui.back(GwCB(action="ssh")), labels=labels)


async def tunaddr(services, ref: int, key: str, chat_id: int) -> View | None:
    virtual = list(await call(services.lan_virtual) or [])
    return View(title=tl.TUNADDR_TITLE, rows=_codes(virtual), count=str(len(virtual)) if virtual else "",
                empty=tl.TUNADDR_EMPTY, back=ui.back(GwCB(action="lan")))


register(Kind("sshfrom", "gateway", ADMIN, sshfrom, anchored=True))     # 1 — локальная сеть, 2 — другие шлюзы
register(Kind("sshunres", "gateway", ADMIN, sshunres, anchored=True))   # 2 — прошлые адреса
register(Kind("tunaddr", "gateway", ADMIN, tunaddr))
