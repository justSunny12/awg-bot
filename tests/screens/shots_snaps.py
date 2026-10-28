"""Снимки всех ролей: снимки событий — итог, отчёт или уведомление, чей
список не влез, оставляют хвост «и ещё N» ссылкой на экран «весь список»
(bot/lists, вид snap; infra/db/listsnap).

Источники: итог «Свои сайты» на 40 отказов (админ, пользователь, гость),
итог «🗺 Подсети» с 10 отказами, отчёт рассылки на 30 недоставленных (-n),
итог завершения переезда на 50 потерявших связь (-n), итог «➕ Адрес»
SSH-доступа на 15 адресов (основной бот и агент), Т-Ж4 на 9 спорных правок
(авария и С9, -n), отчёт восстановления на 8 не вставших устройств (-n),
итог агента «➕ В туннель» на 400 доменов; протухший и несуществующий снимок;
сбой записи снимка — хвост текстом.

Ключ снимка в эталоне — по счёту внутри снимка (snapkey001, snapkey002, …):
обвязка подменяет listsnap.new_key (harness._reset_module_state).
Поведенческие утверждения по этим снимкам — test_list_snapshot_sources.py.
"""
from __future__ import annotations

import datetime as _dt
import inspect
import json
import sqlite3

from awgbot.bot.callbacks import BroadcastCB, FriendCB, GwCB, GwSlotCB, ListCB, PageCB, RoutingCB, SetCB
from awgbot.core import config

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import KS, _bc_ready, _fw, _people
from tests.screens.shots_admin_common_gw import _slots
from tests.screens.shots_admin_events import _ev_restore_done
from tests.screens.shots_admin_lists import _mig
from tests.screens.shots_admin_rf import _ks_sites
from tests.screens.shots_client import GUEST_TG, RF_SITES, guest, rt
from tests.screens.shots_gateway_agent import _shot as _gw_shot
from tests.screens.shots_gateway_common import _TICK_LAN, _ssh

BAD40 = "\n".join(f"bad{i:02d}..ru" for i in range(40))
NETS10 = "192.168.1.0/24 " + " ".join(f"10.241.{i}.0/24" for i in range(10))
ADDR15 = " ".join(f"198.51.100.{n}" for n in range(1, 16))
DOMAINS400 = " ".join([f"shop{i:03d}.example.com" for i in range(200)] + [f"bad-name-{i:03d}" for i in range(200)])


def snap(key: str = "snapkey001", new: bool = False) -> tuple:
    """Переход по ссылке хвоста: /start list-snap-<ключ>[-n]."""
    return ("start", f"list-snap-{key}" + ("-n" if new else ""))


def page(key: str, n: int, new: int = 0) -> tuple:
    return ("press", PageCB(screen="ls_snap", ref=0, page=n, back=ListCB(kind="snap", key=key, new=new).pack()))


def _call(builder, services, mp):
    n = len([p for p in inspect.signature(builder).parameters.values()
             if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
    return builder(services, mp) if n >= 2 else builder(services)


def broken(builder):
    """Тот же построитель, но запись снимка падает (база заперта): итог обязан прийти
    с хвостом текстом, а не упасть."""
    def build(services, mp):
        from awgbot.infra.db import listsnap

        def locked(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")
        mp.setattr(listsnap.ListSnapMixin, "_snap_keep", locked)
        return _call(builder, services, mp)
    return build


# ── построители ──────────────────────────────────────────────────────────────

def _bc_thirty(services, mp):
    """Профили _people и 30 «Подписчик NN»; все 30 подписчиков
    заблокировали бота — доставка им не проходит."""
    from awgbot.bot.handlers.admin import broadcast as bc
    who = _bc_ready(services)
    dead = []
    for i in range(30):
        created = services.create_client(f"Подписчик {i:02d}", 1, "year", 0)
        tg = 30_000 + i
        assert services.activate_client(created.invite_code, tg).ok
        dead.append(tg)
    real = bc.broadcast

    async def broadcast(bot, tg_ids, text, photos=(), by_tg=None):
        ok, failed = await real(bot, [t for t in tg_ids if t not in dead], text, photos, by_tg)
        return ok, list(failed) + [t for t in tg_ids if t in dead]
    mp.setattr(bc, "broadcast", broadcast)
    return who


def _mig50(services, mp):
    """Переезд идёт, 50 недавно активных устройств не переехали; смена интерфейса на
    хосте — заглушками: завершение уронит всех пятьдесят."""
    from awgbot.infra import awglock
    who = _mig(50)(services, mp)
    services._retire_interface = lambda *a, **k: None
    mp.setattr(awglock, "write_state", lambda **k: None)
    mp.setattr(awglock, "needs_migration", lambda: False)
    return who


def _restore8(services):
    """Восстановление: восемь устройств не встали — их адреса уже у устройств шлюза."""
    who = _people(services)
    lost = [{"device": f"Телефон {i}", "client": f"Профиль <{i}>", "address": f"10.8.1.{50 + i}"} for i in range(8)]
    services.pop_restore_done = lambda: {"created_at": "2026-09-01T03:00:00+05:00",
                                         "merge": {"line": "same", "slots": [], "lost": lost}}
    return who


DISPUTED9 = [{"table": "clients", "col": "name", "tail": f"Профиль {i} здесь", "kept": f"Профиль {i} там",
              "name": f"Профиль {i}"} for i in range(9)]


def _tj4(variant: str):
    """Т-Ж4 — уведомление основы о спорном хвосте: 9 правок (variant — crash | c9)."""
    async def fire(services, bot):
        from awgbot.bot.notifier import send_notifications
        await send_notifications(bot, services._tail_notes(variant, 3, [dict(d) for d in DISPUTED9], 9))
    return fire


def _with_snap(builder, chat: int, *, at: _dt.datetime | None = None, rows: int = 3, back: str = ""):
    """Построитель роли и снимок в чате chat (at — время события, по умолчанию сейчас)."""
    def build(services, mp):
        who = _call(builder, services, mp)
        key = services.db.list_snap_keep(chat, "admin", "⚠️ Не добавлено",
                                         [f"<code>bad{i:02d}..ru</code> — не похоже на адрес" for i in range(rows)], back)
        if at is not None:
            from awgbot.util import timeutil
            data = json.loads(services.db.get_state("list_snap:" + key))
            data["at"] = timeutil.to_iso(at)
            services.db.set_state("list_snap:" + key, json.dumps(data, ensure_ascii=False))
        return who
    return build


def _evicted(builder, chat: int):
    """Построитель роли и 21 снимок в чате chat за одну минуту: первый
    (snapkey001) вытеснен двадцать первым — не протух."""
    def build(services, mp):
        who = _call(builder, services, mp)
        for n in range(21):
            services.db.list_snap_keep(chat, "admin", f"⚠️ Не добавлено №{n + 1}",
                                       [f"<code>bad{n:02d}..ru</code> — не похоже на адрес"],
                                       RoutingCB(action="sites", ref=KS).pack())
        return who
    return build


_MAIN = ("start", "")
_SITES = ("press", RoutingCB(action="sites", ref=KS))
_ADD = ("press", RoutingCB(action="add", ref=KS))
_LAN = ("press", GwSlotCB(action="lan_nets", slot=1))
_CARD = ("press", GwSlotCB(action="card", slot=1))
_BC = [("press", BroadcastCB(action="pick")), ("press", BroadcastCB(action="all")),
       ("press", BroadcastCB(action="next")), ("text", "Плановые работы на сервере"),
       ("press", BroadcastCB(action="send"))]
_FINISH = ("press", SetCB(sec="mig", act="do", key="finish!"))
_FW_ADD = [("press", SetCB(sec="fw")), ("press", SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow")),
           ("text", ADDR15)]
_FW_BACK = ("press", SetCB(sec="fw"))
_RESTORE = ("call", _ev_restore_done, "отчёт восстановления")


ADMIN = [
    # ── итог «Свои сайты»: 40 отказов ───────────────────────────────────────
    Shot("adm.snap.sites.report", role="admin", steps=[_SITES, _ADD, ("text", BAD40)], data=_ks_sites,
         title="40 неадресов — «и ещё 35» ссылкой на снимок всех отказов"),
    Shot("adm.snap.sites.open", role="admin", steps=[_SITES, _ADD, ("text", BAD40), snap()], data=_ks_sites,
         title="снимок отказов: на месте живого меню, стр. 1 из 2"),
    Shot("adm.snap.sites.p2", role="admin", steps=[_SITES, _ADD, ("text", BAD40), snap(), page("snapkey001", 1)],
         data=_ks_sites, title="снимок отказов, стр. 2"),
    Shot("adm.snap.sites.back", role="admin",
         steps=[_SITES, _ADD, ("text", BAD40), snap(), ("press", RoutingCB(action="sites", ref=KS))],
         data=_ks_sites, title="«⬅️ Назад» — «📋 Свои сайты» профиля"),
    Shot("adm.snap.sites.failed", role="admin", steps=[_SITES, _ADD, ("text", BAD40)], data=broken(_ks_sites),
         title="снимок не записан — итог тот же, «и ещё 35» текстом"),
    # ── итог «🗺 Подсети»: 10 отказов ───────────────────────────────────────
    Shot("adm.snap.lan.report", role="admin", steps=[_LAN, ("text", NETS10)], data=_slots(1),
         title="10 подсетей в служебном пространстве — «• и ещё 7» ссылкой"),
    Shot("adm.snap.lan.open", role="admin", steps=[_LAN, ("text", NETS10), snap()], data=_slots(1),
         title="снимок отказов подсетей — заголовок с именем устройства шлюза"),
    Shot("adm.snap.lan.back", role="admin", steps=[_LAN, ("text", NETS10), snap(), _CARD], data=_slots(1),
         title="«⬅️ Назад» — карточка слота"),
    Shot("adm.snap.lan.failed", role="admin", steps=[_LAN, ("text", NETS10)], data=broken(_slots(1)),
         title="снимок не записан — «• и ещё 7» текстом"),
    # ── отчёт рассылки: 30 недоставленных ───────────────────────────────────
    Shot("adm.snap.bc.report", role="admin", steps=_BC, data=_bc_thirty,
         title="32 профиля, 30 не доставлено — оба хвоста ссылками с «-n»"),
    Shot("adm.snap.bc.undelivered", role="admin", steps=[*_BC, snap("snapkey002", new=True)], data=_bc_thirty,
         title="снимок недоставленных — новым сообщением"),
    Shot("adm.snap.bc.audience", role="admin", steps=[*_BC, snap("snapkey001", new=True)], data=_bc_thirty,
         title="снимок адресатов рассылки — новым сообщением"),
    Shot("adm.snap.bc.failed", role="admin", steps=_BC, data=broken(_bc_thirty),
         title="снимки не записаны — адресаты «[+20]», недоставленные «и ещё 27» текстом"),
    # ── итог завершения переезда: 50 потеряли связь ─────────────────────────
    Shot("adm.snap.mig.finish", role="admin", steps=[_FINISH], data=_mig50,
         title="переезд завершён, 50 потеряли связь — «и ещё 30» ссылкой с «-n»"),
    Shot("adm.snap.mig.open", role="admin", steps=[_FINISH, snap(new=True)], data=_mig50,
         title="снимок потерявших связь — «профиль — устройство», «⬅️ В меню»"),
    # ── «➕ Адрес» SSH-доступа сервера: 15 адресов ──────────────────────────
    Shot("adm.snap.fw.add", role="admin", steps=_FW_ADD, data=_fw(),
         title="15 адресов — двенадцать и «и ещё 3» ссылкой"),
    Shot("adm.snap.fw.open", role="admin", steps=[*_FW_ADD, snap()], data=_fw(),
         title="снимок добавленных адресов"),
    Shot("adm.snap.fw.back", role="admin", steps=[*_FW_ADD, snap(), _FW_BACK], data=_fw(),
         title="«⬅️ Назад» — «🛡 SSH-доступ»"),
    # ── Т-Ж4: 9 спорных правок ──────────────────────────────────────────────
    Shot("adm.snap.tj4.crash", role="admin", steps=[_MAIN, ("call", _tj4("crash"), "Т-Ж4 после аварии")],
         data=_people, title="Т-Ж4 после аварии — пять строк и «… и ещё 4» ссылкой с «-n»"),
    Shot("adm.snap.tj4.crash.open", role="admin",
         steps=[_MAIN, ("call", _tj4("crash"), "Т-Ж4 после аварии"), snap(new=True)], data=_people,
         title="снимок Т-Ж4 после аварии — девять строк без «• »"),
    Shot("adm.snap.tj4.c9", role="admin", steps=[_MAIN, ("call", _tj4("c9"), "Т-Ж4 С9")], data=_people,
         title="Т-Ж4 С9 — пять строк и «… и ещё 4» ссылкой"),
    Shot("adm.snap.tj4.c9.open", role="admin", steps=[_MAIN, ("call", _tj4("c9"), "Т-Ж4 С9"), snap(new=True)],
         data=_people, title="снимок Т-Ж4 С9 — заголовок варианта"),
    # ── отчёт восстановления: 8 устройств не встали ─────────────────────────
    Shot("adm.snap.restore", role="admin", steps=[_MAIN, _RESTORE], data=_restore8,
         title="отчёт восстановления — пять устройств и «… и ещё 3» ссылкой с «-n»"),
    Shot("adm.snap.restore.open", role="admin", steps=[_MAIN, _RESTORE, snap(new=True)], data=_restore8,
         title="снимок не вставших устройств"),
    # ── протухшее и чужое ───────────────────────────────────────────────────
    Shot("adm.snap.stale", role="admin", steps=[_MAIN, snap()],
         data=_with_snap(_people, config.ADMIN_ID, at=NOW - _dt.timedelta(days=31),
                         back=RoutingCB(action="sites", ref=KS).pack()),
         title="снимок старше 30 дней — «больше не хранится», «⬅️ В меню»"),
    Shot("adm.snap.evicted", role="admin", steps=[_MAIN, snap()], data=_evicted(_people, config.ADMIN_ID),
         title="21-й снимок чата вытеснил первый — «бот держит 20 последних списков чата», «⬅️ В меню»"),
    Shot("adm.snap.gone", role="admin", steps=[_MAIN, snap("zzzzzzzzzz")], data=_people,
         title="снимка нет — «Этого списка больше нет»"),
    Shot("adm.snap.foreign", role="admin", steps=[_MAIN, snap()], data=_with_snap(_people, 42),
         title="снимок чужого чата — так же, как нет"),
]

_CL_STEPS = [("press", rt("sites")), ("press", rt("add")), ("text", BAD40)]
_GST_STEPS = [("press", rt("sites", 3)), ("press", rt("add", 3)), ("text", BAD40)]
_GST = guest(1, rf=True)

CLIENT = [
    Shot("cl.snap.sites.report", role="client", steps=_CL_STEPS, data=RF_SITES,
         title="40 неадресов — «и ещё 35» ссылкой"),
    Shot("cl.snap.sites.open", role="client", steps=[*_CL_STEPS, snap()], data=RF_SITES,
         title="снимок отказов у пользователя"),
    Shot("cl.snap.sites.back", role="client", steps=[*_CL_STEPS, snap(), ("press", rt("sites"))], data=RF_SITES,
         title="«⬅️ Назад» — «📋 Свои сайты»"),
]

GUEST = [
    Shot("gst.snap.sites.report", role="guest", steps=_GST_STEPS, data=_GST,
         title="гость: 40 неадресов — «и ещё 35» ссылкой"),
    Shot("gst.snap.sites.open", role="guest", steps=[*_GST_STEPS, snap()], data=_GST,
         title="гость: снимок отказов"),
    Shot("gst.snap.menu", role="guest", steps=[("start", ""), snap()], data=_with_snap(_GST, GUEST_TG),
         title="гость: снимок без источника — «⬅️ В меню» на главную гостя"),
    Shot("gst.snap.menu.press", role="guest", steps=[("start", ""), snap(), ("press", FriendCB(action="refresh"))],
         data=_with_snap(_GST, GUEST_TG), title="гость: «⬅️ В меню» — его главная"),
]

_GW_SSH = [("press", GwCB(action="ssh")), ("press", GwCB(action="ssh_add")), ("text", ADDR15)]
_GW_LAN = [("press", GwCB(action="lan")), ("press", GwCB(action="lan_add")), ("text", DOMAINS400)]

GATEWAY = [
    _gw_shot("gw.snap.ssh.open", steps=[*_GW_SSH, snap()], ssh=_ssh(filter=True),
             title="снимок добавленных адресов SSH-доступа"),
    _gw_shot("gw.snap.ssh.back", steps=[*_GW_SSH, snap(), ("press", GwCB(action="ssh"))], ssh=_ssh(filter=True),
             title="«⬅️ Назад» — «🛡 SSH-доступ»"),
    _gw_shot("gw.snap.transit", steps=_GW_LAN, tick=_TICK_LAN,
             title="«➕ В туннель» на 400 доменов — «…и ещё N строк» ссылкой"),
    _gw_shot("gw.snap.transit.open", steps=[*_GW_LAN, snap()], tick=_TICK_LAN,
             title="снимок итога «в туннель» — все пропущенные строки"),
    _gw_shot("gw.snap.transit.back", steps=[*_GW_LAN, snap(), ("press", GwCB(action="lan"))], tick=_TICK_LAN,
             title="«⬅️ Назад» — «🔀 VPN-транзит»"),
    _gw_shot("gw.snap.menu", steps=[("start", ""), ("start", "list-snap-zzzzzzzzzz")],
             title="агент: снимка нет — «⬅️ В меню» на панель"),
]

SHOTS = [*ADMIN, *CLIENT, *GUEST, *GATEWAY]
