"""Снимки админа: экран «весь список» — куда ведёт хвост «и ещё N» обрезанного
списка (bot/lists), и метка «-n» у ссылок из уведомлений.

Худшие случаи — по виду: 64 подсети слота, 15 слотов по 64 адреса в
туннеле, расхождение конфигурации с 25 устройствами и длинным значением,
имена по 64 знака с «<&>». Ссылки /start с меткой «-n» (из уведомления) —
новым сообщением внизу чата, прежнее меню гаснет; без метки — на месте
живого меню. Живое меню в чате перед ссылкой — шаг («start», «»): главная.
"""
from __future__ import annotations

from awgbot.bot.callbacks import GwSlotCB, ListCB, PageCB, SetCB

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import ADM, IPHONE, KS, _fw, _iso, _mig_running, _people
from tests.screens.shots_admin_common_gw import NASPI, PI4, _ch, _ch_base, _chan, _gw, _installed, _slots


def _long(base: str) -> str:
    """Имя в 64 знака с разметкой, которую текст обязан экранировать."""
    return (base + " <дача> & «гараж» ").ljust(64, "ё")[:64]


def _rename(dev: int, base: str):
    def step(services, mp):
        services.rename_device(dev, _long(base))
    return step


# ── подсети слота: 60 в таблице и 4 в служебном пространстве ────────────────

_NETS64 = [f"172.20.{i}.0/24" for i in range(60)] + [f"10.24{i}.0.0/24" for i in range(1, 5)]


def _nets64(services, mp):
    services.db.gateway_update(2, lan_subnets=list(_NETS64), lan_mode=1)


_NETS = _gw(2, _nets64, _rename(PI4, "Pi4"), lan2=True)
_MAIN = ("start", "")          # живое меню в чате: главная (без неё ссылке нечего править на месте)
_CARD2 = ("press", GwSlotCB(action="card", slot=2))
_GWNETS = ("start", "list-gwnets-2")


def _page(kind: str, ref: int, page: int, new: int = 0) -> tuple:
    return ("press", PageCB(screen=f"ls_{kind}", ref=ref, page=page, back=ListCB(kind=kind, ref=ref, new=new).pack()))


# ── «🛰 Шлюзы»: 15 слотов с VPN-транзитом, по 64 подсети у каждого ──────────

def _fifteen(services, mp):
    for slot in (1, 2):
        services.db.gateway_update(slot, lan_subnets=[f"172.{15 + slot}.{i}.0/24" for i in range(64)], lan_mode=1)
    for slot in range(3, 16):
        created = services.add_device(ADM, _long(f"Gw{slot}"))
        services.db.gateway_add(created.device_id, f"awglink{slot}", 9000 + slot,
                                f"10.99.99.{4 * (slot - 1)}/30", slot_id=slot)
        services.db.gateway_update(slot, lan_subnets=[f"172.{15 + slot}.{i}.0/24" for i in range(64)], lan_mode=1)


_FIFTEEN = _gw(2, _fifteen, _rename(NASPI, "NASPi"), _rename(PI4, "Pi4"), lan2=True)
_PEERNETS = ("start", "list-peernets")


# ── связь подсетей слота: пять соседей и 12 подсетей, которых на шлюзе нет ──

def _transit5(services, mp):
    for slot in range(3, 6):
        created = services.add_device(ADM, _long(f"Gw{slot}"))
        services.db.gateway_add(created.device_id, f"awglink{slot}", 9000 + slot,
                                f"10.99.99.{4 * (slot - 1)}/30", slot_id=slot)
        services.db.gateway_update(slot, lan_subnets=[f"192.168.{slot}.0/24"], lan_mode=1)


_MISSING = {"ok": False, "missing": [f"10.24{1 + i // 4}.{i}.0/24" for i in range(12)]}
_PEERS = _gw(2, _ch_base, _transit5, _ch(peer_nets=_MISSING), lan=True, lan2=True)


# ── расхождение конфигурации слота 1: шлюз отверг текущий документ ─────────

_EXTRA = [f"10.8.1.{a}" for a in range(100, 140)]
_PEER_LONG = " ".join(f"192.168.{n}.0/24" for n in range(100, 140))


def _refused(services, mp):
    """Снимок слота 1: на шлюзе 40 лишних адресов устройств админа и чужие
    подсети соседей (значение длиннее 160 знаков); шлюз отверг текущий документ."""
    services.gwcfg_link_conf = lambda gw, env: ""
    gw = services.db.gateway(1)
    mine = services.gwlink_issued_env(gw)["ADMIN_IPS"]
    doc = services.gwcfg_doc(gw)
    bundle = {**_installed(services), "admin_ips": " ".join([mine, *_EXTRA]), "peer_lan_nets": _PEER_LONG}
    snap = {"bundle": bundle, "link_contract": "1", "mark_status": "confirmed", "agent_version": "3.3.0",
            "awg_generation": 1, "egress_ok": True, "boot_id": "b" * 36, "ts": _iso(NOW), "rev": 1,
            "cfg": {"gen": doc["gen"], **doc["prints"], "env": "0" * 16}}
    services.gwlink_snapshot_in(1, snap, 1, True)
    services.db.set_state(services._gwlink_key(services._GWLINK_SNAP_AT_KEY, 1), _iso(NOW))
    services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1", "confirm1", "token1"])
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"], "applied": [],
                              "failed": {"env": "dnsmasq <b>не</b> поднялся"}})


_DRIFT = _chan(1, _rename(NASPI, "NASPi"), _refused)
_SETTLED = _chan(1, _ch())


async def _drift_note(services, bot):
    """Тик монитора: уведомление о конфигурации — тем же путём, что в бою."""
    from awgbot.bot.notifier import send_notifications
    await send_notifications(bot, services.gw_bundle_drift_notes())


# ── переезд: 25 недавно активных не подключились на новой конфигурации ─────

def _dropped(services, mp):
    who = _mig_running(services, mp)
    names = [_long(f"Ксюша — iPhone {i}") for i in range(25)]
    services.migration_finish_preview = lambda: ([], list(names))
    services.gwcfg_old_uplink = lambda: []
    return who


_FW_NAMES = [_long(f"h{i}.example") for i in range(15)]


LISTS = [
    # ── подсети слота ───────────────────────────────────────────────────────
    Shot("adm.list.gwnets.card", role="admin", press=[_CARD2[1]], data=_NETS,
         title="карточка слота с 64 подсетями — хвосты «и ещё 58» и «весь список» ссылками"),
    Shot("adm.list.gwnets", role="admin", steps=[_MAIN, _CARD2, _GWNETS], data=_NETS,
         title="/start list-gwnets-2 из карточки — на месте, стр. 1 из 3"),
    Shot("adm.list.gwnets.p2", role="admin", steps=[_MAIN, _CARD2, _GWNETS, _page("gwnets", 2, 1)], data=_NETS,
         title="листание вперёд — стр. 2 из 3"),
    Shot("adm.list.gwnets.p3", role="admin", steps=[_MAIN, _CARD2, _GWNETS, _page("gwnets", 2, 1), _page("gwnets", 2, 2)],
         data=_NETS, title="последняя страница — подсети вне туннеля и строки ⚠️"),
    Shot("adm.list.gwnets.p2.back", role="admin",
         steps=[_MAIN, _CARD2, _GWNETS, _page("gwnets", 2, 2), _page("gwnets", 2, 1)], data=_NETS,
         title="листание назад — стр. 2 из 3"),
    Shot("adm.list.gwnets.back", role="admin", steps=[_MAIN, _CARD2, _GWNETS, _page("gwnets", 2, 1), _CARD2], data=_NETS,
         title="«⬅️ Назад» со списка — карточка слота на месте"),
    Shot("adm.list.gwnets.again", role="admin", steps=[_MAIN, _CARD2, _GWNETS, _page("gwnets", 2, 2), _GWNETS],
         data=_NETS, title="ссылка ещё раз после листания — снова стр. 1"),
    Shot("adm.list.gwnets.gone", role="admin", start="list-gwnets-9", data=_slots(1),
         title="слот из ссылки снят — «🛰 Выбранный шлюз снят», назад в «🛰 Шлюзы»"),
    Shot("adm.list.gwnets.empty", role="admin", start="list-gwnets-1", data=_slots(1),
         title="у слота подсетей нет — пустое состояние словами"),
    # ── связь подсетей слота ────────────────────────────────────────────────
    Shot("adm.list.gwpeers.card", role="admin", press=[_CARD2[1]], data=_PEERS,
         title="карточка: соседей четыре, «и ещё 1» ссылкой"),
    Shot("adm.list.gwpeers.card1", role="admin", press=[GwSlotCB(action="card", slot=1)], data=_PEERS,
         title="карточка: на шлюзе нет 12 подсетей соседей — восемь и «и ещё 4» ссылкой"),
    Shot("adm.list.gwpeers", role="admin", steps=[_MAIN, _CARD2, ("start", "list-gwpeers-1")], data=_PEERS,
         title="связь подсетей слота: соседи ссылками, подсети, которых на шлюзе нет, — ⚠️"),
    Shot("adm.list.gwpeers.none", role="admin", start="list-gwpeers-1", data=_slots(1, lan=True),
         title="соседей нет — пустое состояние словами"),
    # ── связь подсетей всех слотов ──────────────────────────────────────────
    Shot("adm.list.peernets.rt", role="admin", press=[SetCB(sec="rt")], data=_FIFTEEN,
         title="«🛰 Шлюзы» на 15 слотов по 64 адреса — у каждого «и ещё 61» ссылкой"),
    *[Shot(f"adm.list.peernets.p{p + 1}", role="admin",
           steps=[_MAIN, ("press", SetCB(sec="rt")), _PEERNETS, *[_page("peernets", 0, k) for k in range(1, p + 1)]],
           data=_FIFTEEN, title=f"связь подсетей, стр. {p + 1} — худший случай: имена в 64 знака, 64 адреса")
      for p in range(5)],
    Shot("adm.list.peernets.clamp", role="admin",
         steps=[_MAIN, ("press", SetCB(sec="rt")), _PEERNETS, _page("peernets", 0, 9)], data=_FIFTEEN,
         title="страница за концом (список сократился между нажатиями) — последняя, а не пустой экран"),
    Shot("adm.list.peernets.back", role="admin",
         steps=[_MAIN, ("press", SetCB(sec="rt")), _PEERNETS, ("press", SetCB(sec="rt"))], data=_FIFTEEN,
         title="«⬅️ Назад» — «🛰 Шлюзы»"),
    Shot("adm.list.peernets.few", role="admin", start="list-peernets", data=_slots(1, lan=True),
         title="VPN-транзит меньше чем у двух — пустое состояние словами"),
    # ── расхождение конфигурации ────────────────────────────────────────────
    Shot("adm.list.drift.note", role="admin", steps=[_MAIN, ("call", _drift_note, "тик монитора")],
         data=_DRIFT, title="уведомление: 20 устройств и «и ещё 20» ссылкой с меткой «-n»"),
    Shot("adm.list.drift", role="admin", steps=[_MAIN, ("start", "list-drift-1-n")], data=_DRIFT,
         title="ссылка из уведомления — новым сообщением внизу, «· сейчас», прежнее меню погашено"),
    Shot("adm.list.drift.p2", role="admin",
         steps=[_MAIN, ("start", "list-drift-1-n"), _page("drift", 1, 1, new=1)], data=_DRIFT,
         title="листание открытого из уведомления — на месте, «· сейчас» остаётся"),
    Shot("adm.list.drift.back", role="admin", steps=[_MAIN, ("start", "list-drift-1-n"),
                                                     ("press", GwSlotCB(action="card", slot=1))],
         data=_DRIFT, title="«⬅️ Назад» — карточка слота на месте открытого списка"),
    Shot("adm.list.drift.empty", role="admin", steps=[_MAIN, ("start", "list-drift-1-n")], data=_SETTLED,
         title="расхождений сейчас нет — так и сказано, а не пустой экран"),
    Shot("adm.list.drift.gone", role="admin", start="list-drift-7-n", data=_slots(1),
         title="слот из уведомления снят"),
    # ── SSH-доступ: имена, которые не резолвятся ───────────────────────────
    Shot("adm.list.fwunres.screen", role="admin", press=[SetCB(sec="fw")],
         data=_fw(enabled=True, present=True, allow=[], raw_allow=_FW_NAMES, unresolved=_FW_NAMES,
                  admin_ips=["x"], bot_username="test_bot"),
         title="«🛡 SSH-доступ»: 15 имён не резолвятся — «и ещё 3» ссылкой"),
    Shot("adm.list.fwunres", role="admin", steps=[_MAIN, ("press", SetCB(sec="fw")), ("start", "list-fwunres")],
         data=_fw(enabled=True, present=True, allow=[], raw_allow=_FW_NAMES, unresolved=_FW_NAMES,
                  admin_ips=["x"], bot_username="test_bot"),
         title="все имена, которые не резолвятся, — имена в 64 знака экранированы"),
    Shot("adm.list.fwunres.empty", role="admin", start="list-fwunres", data=_fw(),
         title="все имена резолвятся — пустое состояние словами"),
    # ── переезд: кто потеряет связь ─────────────────────────────────────────
    Shot("adm.list.migdrop.confirm", role="admin", press=[SetCB(sec="mig", act="do", key="finish")],
         data=_dropped, title="«🚚 Завершить переезд?» — 20 имён и «и ещё 5» ссылкой"),
    Shot("adm.list.migdrop", role="admin",
         steps=[_MAIN, ("press", SetCB(sec="mig", act="do", key="finish")), ("start", "list-migdrop")], data=_dropped,
         title="кто потеряет связь — все 25, «⬅️ Назад» на подтверждение"),
    Shot("adm.list.migdrop.idle", role="admin", start="list-migdrop", data=_people,
         title="переезд не идёт — «🚚 Переезд сейчас не идёт», назад в «🔧 Сервис»"),
    # ── чужое и незнакомое ──────────────────────────────────────────────────
    Shot("adm.list.unknown", role="admin", steps=[_MAIN, ("start", "list-nosuch")], data=_people,
         title="вид неизвестен (ссылка новее бота) — главная"),
    Shot("adm.list.agent_kind", role="admin", steps=[_MAIN, ("start", "list-sshfrom")], data=_people,
         title="вид агента у основного бота — главная"),
    Shot("adm.list.press.foreign", role="admin", press=[ListCB(kind="tunaddr")], data=_people,
         title="кнопка вида агента у основного бота — молча, экран не меняется"),
    # ── метка «-n» у ссылок на экраны ──────────────────────────────────────
    Shot("adm.link.cl.live", role="admin", steps=[_MAIN, ("start", f"cl-{KS}")], data=_people,
         title="/start cl-N при живом меню — на месте, как раньше"),
    Shot("adm.link.cl.new", role="admin", steps=[_MAIN, ("start", f"cl-{KS}-n")], data=_people,
         title="/start cl-N-n из уведомления — новым сообщением, прежнее меню погашено"),
    Shot("adm.link.dev.new", role="admin", steps=[_MAIN, ("start", f"dev-{IPHONE}-n")], data=_people,
         title="/start dev-N-n — новым сообщением"),
    Shot("adm.link.traffic.new", role="admin", steps=[_MAIN, ("start", f"traffic-{KS}-t-n")], data=_people,
         title="/start traffic-N-t-n — трафик профиля новым сообщением"),
    Shot("adm.link.gw.new", role="admin", steps=[_MAIN, ("start", "gw-2-n")], data=_slots(2),
         title="/start gw-2-n — карточка новым сообщением, «⬅️ В меню» как у карточки с главной"),
    Shot("adm.link.gw.new.back", role="admin",
         steps=[_MAIN, ("start", "gw-2-n"), ("press", "m:main")], data=_slots(2),
         title="«⬅️ В меню» с карточки, открытой из уведомления, — главная"),
    Shot("adm.link.gw.new.missing", role="admin", steps=[_MAIN, ("start", "gw-9-n")], data=_slots(1),
         title="/start gw-9-n — слота нет"),
]
