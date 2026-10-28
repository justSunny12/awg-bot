"""Снимки агента шлюза: экран «весь список» (bot/lists, виды агента) — откуда
SSH открыт изнутри, имена SSH-доступа, которые не резолвятся, адреса в
туннеле своей сети. Худший случай: 64 подсети локальной сети и 64 подсети
соседей, 40 имён в 64 знака с «<&>» и 40 прошлых адресов, 64 адреса в туннеле.
Живое меню перед ссылкой — шаг («start», «»): панель."""
from __future__ import annotations

from awgbot.bot.callbacks import GwCB, ListCB, PageCB

from tests.screens.shots_gateway_agent import _p, _shot
from tests.screens.shots_gateway_common import _TICK_LAN, _ssh

_LAN = [f"192.168.{i}.0/24" for i in range(64)]
_PEERS = ["10.0.0.0/8<b>"] + [f"10.24{1 + i // 16}.{i}.0/24" for i in range(63)]
_NAMES = [(f"h{i}.dyn.example <дача> & «гараж» ").ljust(64, "ё")[:64] for i in range(40)]
_HELD = [f"203.0.113.{i}" for i in range(1, 41)]
_SSH = _ssh(filter=True, allow=list(_NAMES), lan=list(_LAN), peer_nets=list(_PEERS), unresolved=list(_NAMES),
            held=list(_HELD))
_VIRTUAL = tuple(f"10.240.{i}.0/24" for i in range(64))

_MAIN = ("start", "")
_SSH_OPEN = _p(GwCB(action="ssh"))
_LAN_OPEN = _p(GwCB(action="lan"))


def _page(kind: str, page: int, new: int = 0) -> tuple:
    return _p(PageCB(screen=f"ls_{kind}", ref=0, page=page, back=ListCB(kind=kind, new=new).pack()))


LISTS = [
    # ── «🛡 SSH-доступ»: хвосты ссылками ────────────────────────────────────
    _shot("gw.list.ssh", press=[GwCB(action="ssh")], ssh=_SSH,
          title="«🛡 SSH-доступ» в худшем случае — хвосты «и ещё N» ссылками на списки"),
    # ── откуда SSH открыт изнутри ───────────────────────────────────────────
    _shot("gw.list.sshfrom", steps=[_MAIN, _SSH_OPEN, ("start", "list-sshfrom")], ssh=_SSH,
          title="SSH изнутри: две группы с подписями, стр. 1 — на месте раздела"),
    *[_shot(f"gw.list.sshfrom.p{p + 1}",
            steps=[_MAIN, _SSH_OPEN, ("start", "list-sshfrom"), *[_page("sshfrom", k) for k in range(1, p + 1)]],
            ssh=_SSH, title=f"SSH изнутри, стр. {p + 1}") for p in range(1, 5)],
    _shot("gw.list.sshfrom.back", steps=[_MAIN, _SSH_OPEN, ("start", "list-sshfrom"), _SSH_OPEN], ssh=_SSH,
          title="«⬅️ Назад» — «🛡 SSH-доступ» на месте"),
    _shot("gw.list.sshfrom.lan_only", steps=[_MAIN, ("start", "list-sshfrom")], ssh=_ssh(lan=list(_LAN[:40])),
          title="подсетей других шлюзов нет — их группа не выводится"),
    _shot("gw.list.sshfrom.peers", steps=[_MAIN, _SSH_OPEN, ("start", "list-sshfrom-2")], ssh=_SSH,
          title="хвост «Из подсетей других шлюзов» — сразу страница с их подписью"),
    # ── имена, которые не резолвятся ────────────────────────────────────────
    _shot("gw.list.sshunres", steps=[_MAIN, _SSH_OPEN, ("start", "list-sshunres")], ssh=_SSH,
          title="40 имён в 64 знака и 40 прошлых адресов, стр. 1"),
    *[_shot(f"gw.list.sshunres.p{p + 1}",
            steps=[_MAIN, _SSH_OPEN, ("start", "list-sshunres"), *[_page("sshunres", k) for k in range(1, p + 1)]],
            ssh=_SSH, title=f"не резолвятся, стр. {p + 1}") for p in range(1, 3)],
    _shot("gw.list.sshunres.held", steps=[_MAIN, _SSH_OPEN, ("start", "list-sshunres-2")], ssh=_SSH,
          title="хвост «держу прошлый адрес» — сразу страница с подписью «Держу прошлые адреса»"),
    _shot("gw.list.sshunres.empty", steps=[_MAIN, ("start", "list-sshunres")],
          title="все имена резолвятся — пустое состояние словами"),
    # ── адреса в туннеле своей сети ─────────────────────────────────────────
    _shot("gw.list.lan", press=[GwCB(action="lan")], tick=_TICK_LAN, lan_virtual=_VIRTUAL,
          title="«🔀 VPN-транзит» с 64 адресами в туннеле — «и ещё 58» ссылкой"),
    _shot("gw.list.tunaddr", steps=[_MAIN, _LAN_OPEN, ("start", "list-tunaddr")], tick=_TICK_LAN,
          lan_virtual=_VIRTUAL, title="адреса в туннеле, стр. 1 из 3"),
    _shot("gw.list.tunaddr.p3", steps=[_MAIN, _LAN_OPEN, ("start", "list-tunaddr"), _page("tunaddr", 1),
                                       _page("tunaddr", 2)],
          tick=_TICK_LAN, lan_virtual=_VIRTUAL, title="адреса в туннеле, последняя страница"),
    _shot("gw.list.tunaddr.back", steps=[_MAIN, _LAN_OPEN, ("start", "list-tunaddr"), _LAN_OPEN], tick=_TICK_LAN,
          lan_virtual=_VIRTUAL, title="«⬅️ Назад» — «🔀 VPN-транзит» на месте"),
    _shot("gw.list.tunaddr.new", steps=[_MAIN, ("start", "list-tunaddr-n")], tick=_TICK_LAN, lan_virtual=_VIRTUAL,
          title="ссылка из уведомления — новым сообщением, «· сейчас», панель погашена"),
    _shot("gw.list.tunaddr.empty", steps=[_MAIN, ("start", "list-tunaddr")], tick=_TICK_LAN,
          title="сервер адресов не прислал — пустое состояние словами"),
    # ── чужое ───────────────────────────────────────────────────────────────
    _shot("gw.list.foreign", steps=[_MAIN, ("start", "list-gwnets-2")],
          title="вид основного бота у агента — панель"),
    _shot("gw.list.press.foreign", press=[ListCB(kind="gwnets", ref=2)],
          title="кнопка вида основного бота у агента — молча"),
]
