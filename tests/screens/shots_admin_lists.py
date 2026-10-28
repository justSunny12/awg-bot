"""Снимки админа: экран «весь список» — куда ведёт хвост «и ещё N» обрезанного
списка (bot/lists), и метка «-n» у ссылок из уведомлений.

Худшие случаи — по виду: 64 подсети слота, 15 слотов по 64 адреса в
туннеле, расхождение конфигурации с 25 устройствами и длинным значением,
имена по 64 знака с «<&>». Ссылки /start с меткой «-n» (из уведомления) —
новым сообщением внизу чата, прежнее меню гаснет; без метки — на месте
живого меню. Живое меню в чате перед ссылкой — шаг («start», «»): главная.

PAGED — экраны, которые сами список («📶 Онлайн», «🟡 Истекают», «📊 Трафик»,
«🚚 Переезд», списки переезда): листание на месте, на 1, 25 и 61 запись.
"""
from __future__ import annotations

from awgbot.bot.callbacks import ClientCB, GwSlotCB, ListCB, Menu, PageCB, SetCB
from awgbot.core import config

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


def _space_off(services, mp):
    """Служебное пространство задевает подсети пользователей: адреса в туннеле
    выключены целиком (lan_space_problem), хотя транзит у обоих слотов включён."""
    mp.setattr(config, "ROUTING_LAN_SPACE", "10.0.0.0/12")


_SPACE_OFF = _gw(2, _space_off, lan=True, lan2=True)


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
         title="соседей нет — пустое состояние словами: транзит меньше чем у двух"),
    Shot("adm.list.gwpeers.transit_off", role="admin", start="list-gwpeers-1", data=_slots(1, subnets=True),
         title="у самого слота VPN-транзит выключен — пустое состояние называет это"),
    Shot("adm.list.gwpeers.space_off", role="admin", start="list-gwpeers-1", data=_SPACE_OFF,
         title="адреса в туннеле выключены (пространство задевает подсети) — связь выключена вместе с ними"),
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


# ── экраны, которые сами список: листание на месте ─────────────────────────
# Каждый экран — на 1, 25 и 61 запись: одна страница без «стр.», граница
# прежнего предела в 25 строк, три страницы (у «🟡 Истекают» — по 16 на
# страницу, столько кнопок «⏱» влезает в десять рядов). Профили «Профиль NN»
# по одному устройству «Тел NN»; номера профилей — с 3 (после админа).

FIRST = 3                               # первый профиль после служебного и админа


def _profiles(services, n: int, *, kind: str = "month") -> list[tuple[int, int]]:
    """n профилей по одному устройству → [(профиль, устройство)]."""
    from tests.screens.shots_admin_common import _base
    _base(services)
    out = []
    for i in range(n):
        created = services.create_client(f"Профиль {i:02d}", 1, kind)
        assert services.activate_client(created.invite_code, 3000 + i).ok
        out.append((created.client_id, services.add_device(created.client_id, f"Тел {i:02d}").device_id))
    assert not out or out[0][0] == FIRST, out[0]
    return out


def _on(n: int):
    """n устройств на связи."""
    def build(services, mp):
        from tests.screens.shots_admin_common import _online
        _online(services, *[d for _c, d in _profiles(services, n)])
        return _who()
    return build


def _exp(n: int):
    """n месячных подписок, каждая кончается в свой час — ближайшая сверху."""
    import datetime as _dt

    def build(services, mp):
        for i, (c, _d) in enumerate(_profiles(services, n)):
            services.db.update_client_fields(c, period_start=_iso(NOW - _dt.timedelta(days=29)),
                                             period_end=_iso(NOW + _dt.timedelta(hours=2, minutes=30 * i)))
        return _who()
    return build


def _tr(n: int):
    """n профилей с трафиком за месяц: чем больше номер, тем больше трафик."""
    def build(services, mp):
        from tests.screens.shots_admin_common import GB
        services.db.add_traffic_bulk([(d, (i + 1) * GB // 10, GB // 100)
                                      for i, (_c, d) in enumerate(_profiles(services, n))])
        return _who()
    return build


def _one(services, n: int, *, live: bool = False) -> int:
    """Профиль «Ксюша» с n устройствами «Тел NN» (номер профиля — FIRST)."""
    from tests.screens.shots_admin_common import _base
    _base(services)
    created = services.create_client("Ксюша", n, "year")
    assert services.activate_client(created.invite_code, 2001).ok and created.client_id == FIRST
    ids = [services.add_device(FIRST, f"Тел {i:02d}").device_id for i in range(n)]
    if live:
        hour_ago = int(NOW.timestamp()) - 3600
        for d in ids:
            services.db.update_device_fields(d, last_handshake=hour_ago)
    return ids


def _trdev(n: int):
    """У Ксюши n устройств с трафиком за месяц."""
    def build(services, mp):
        from tests.screens.shots_admin_common import GB
        ids = _one(services, n)
        services.db.add_traffic_bulk([(d, (i + 1) * GB // 10, GB // 100) for i, d in enumerate(ids)])
        return _who()
    return build


def _mig_conf(mp) -> None:
    """Переезд настроен (awg1, 10.9.1) — как в построителях переезда."""
    from awgbot.core import config
    from awgbot.infra import awglock
    mp.setattr(config, "AWG_INTERFACE", "awg0")
    mp.setattr(config, "MIGRATION_INTERFACE", "awg1")
    mp.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.1")
    mp.setattr(config, "SERVER_PORT", 43125)
    mp.setattr(awglock, "applied_generation", lambda: 1)
    mp.setattr(awglock, "target_generation", lambda: 1)


def _mig(n: int, *, cancelled: bool = False):
    """Переезд идёт: n профилей по одному недавно активному устройству,
    не переехал никто; cancelled — все переехали, и переезд отменён."""
    def build(services, mp):
        _mig_conf(mp)
        hour_ago = int(NOW.timestamp()) - 3600
        for _c, d in _profiles(services, n):
            services.db.update_device_fields(d, last_handshake=hour_ago)
        services.migration_start()
        if cancelled:
            for twin in services.db.twins_by_origin().values():
                services.db.update_device_fields(twin, last_handshake=int(NOW.timestamp()))
            services.migration_cancel()
        return _who()
    return build


def _mig_one(n: int):
    """Переезд идёт: у Ксюши n недавно активных устройств, не переехало ни одно."""
    def build(services, mp):
        _mig_conf(mp)
        _one(services, n, live=True)
        services.migration_start()
        return _who()
    return build


async def _went_offline(services, bot):
    """Между нажатиями 30 устройств отключились — осталась 31 запись, две страницы."""
    for d, _c in services.online_devices()[31:]:
        services.db.update_device_fields(d.id, last_handshake=0)


def _turn(screen: str, back, page: int, ref: int = 0) -> tuple:
    """Кнопка листания экрана: PageCB с колбэком, который рисует экран заново."""
    packed = back if isinstance(back, str) else back.pack()
    return ("press", PageCB(screen=screen, ref=ref, page=page, back=packed))


def _who() -> tuple:
    """Действует админ (как у _base)."""
    return config.ADMIN_ID, "Админ"


_ONLINE = ("press", Menu(action="online"))
_EXPIRING = ("press", Menu(action="expiring"))
_TRAFFIC = ("press", Menu(action="traffic"))
_MIGRATION = ("press", Menu(action="migration"))
_PENDING = ("press", SetCB(sec="mig", act="do", key="pending"))
_ORPHANS = ("press", SetCB(sec="mig", act="do", key="orphans"))
_TRDEV = ClientCB(action="traffic", client_id=FIRST)
_MIGCL = ClientCB(action="mig", client_id=FIRST)


def _on_pages(*pages: int) -> list:
    return [_turn("online", Menu(action="online"), p) for p in pages]


PAGED = [
    # ── «📶 Онлайн» ─────────────────────────────────────────────────────────
    Shot("adm.paged.online.1", role="admin", press=[Menu(action="online")], data=_on(1),
         title="онлайн, одно устройство — без «стр.» и листания"),
    Shot("adm.paged.online.25", role="admin", press=[Menu(action="online")], data=_on(25),
         title="онлайн, 25 — одна страница, хвоста «и ещё N» нет"),
    Shot("adm.paged.online.61", role="admin", press=[Menu(action="online")], data=_on(61),
         title="онлайн, 61 — стр. 1 из 3, «След. страница ▶️»"),
    Shot("adm.paged.online.61.p2", role="admin", steps=[_ONLINE, *_on_pages(1)], data=_on(61),
         title="листание вперёд — стр. 2 из 3, обе стрелки"),
    Shot("adm.paged.online.61.p3", role="admin", steps=[_ONLINE, *_on_pages(1, 2)], data=_on(61),
         title="последняя страница — одна запись, только «◀️ Пред. страница»"),
    Shot("adm.paged.online.61.back", role="admin", steps=[_ONLINE, *_on_pages(1, 2, 1)], data=_on(61),
         title="листание назад — снова стр. 2 из 3"),
    Shot("adm.paged.online.61.again", role="admin", steps=[_ONLINE, *_on_pages(1), ("start", "online")],
         data=_on(61), title="/start online после листания — та же стр. 2 из 3"),
    Shot("adm.paged.online.61.menu", role="admin", steps=[_ONLINE, *_on_pages(1), ("press", Menu(action="main"))],
         data=_on(61), title="«⬅️ В меню» со второй страницы — главная"),
    Shot("adm.paged.online.clamp", role="admin",
         steps=[_ONLINE, *_on_pages(1, 2), ("call", _went_offline, "30 устройств отключились"), ("start", "online")],
         data=_on(61), title="список сократился до двух страниц — /start online на стр. 3 открывает последнюю"),
    # ── «🟡 Истекают» ───────────────────────────────────────────────────────
    Shot("adm.paged.expiring.1", role="admin", press=[Menu(action="expiring")], data=_exp(1),
         title="истекает один — без листания"),
    Shot("adm.paged.expiring.25", role="admin", press=[Menu(action="expiring")], data=_exp(25),
         title="истекают 25 — стр. 1 из 2: 16 строк и 16 кнопок «⏱» тех же профилей, десять рядов"),
    Shot("adm.paged.expiring.25.p2", role="admin",
         steps=[_EXPIRING, _turn("expiring", Menu(action="expiring"), 1)], data=_exp(25),
         title="стр. 2 из 2 — девять строк и девять «⏱» тех же профилей"),
    Shot("adm.paged.expiring.61", role="admin", press=[Menu(action="expiring")], data=_exp(61),
         title="истекают 61 — стр. 1 из 4"),
    Shot("adm.paged.expiring.61.p2", role="admin",
         steps=[_EXPIRING, _turn("expiring", Menu(action="expiring"), 1)], data=_exp(61),
         title="стр. 2 из 4 — кнопки «⏱» профилей 16–31, обе стрелки"),
    Shot("adm.paged.expiring.61.p4", role="admin",
         steps=[_EXPIRING, *[_turn("expiring", Menu(action="expiring"), p) for p in (1, 2, 3)]], data=_exp(61),
         title="последняя страница — 13 строк и 13 «⏱»"),
    Shot("adm.paged.expiring.61.again", role="admin",
         steps=[_EXPIRING, _turn("expiring", Menu(action="expiring"), 1), ("start", "expiring")], data=_exp(61),
         title="/start expiring после листания — та же стр. 2 из 4"),
    # ── «📊 Трафик» по профилям ─────────────────────────────────────────────
    Shot("adm.paged.traffic.1", role="admin", press=[Menu(action="traffic")], data=_tr(1),
         title="трафик, один профиль — без листания"),
    Shot("adm.paged.traffic.25", role="admin", press=[Menu(action="traffic")], data=_tr(25),
         title="трафик, 25 профилей — одна страница"),
    Shot("adm.paged.traffic.61", role="admin", press=[Menu(action="traffic")], data=_tr(61),
         title="трафик, 61 профиль — стр. 1 из 3, итог в шапке"),
    Shot("adm.paged.traffic.61.p3", role="admin",
         steps=[_TRAFFIC, *[_turn("traffic", Menu(action="traffic"), p) for p in (1, 2)]], data=_tr(61),
         title="последняя страница — итог в шапке тот же"),
    Shot("adm.paged.traffic.61.again", role="admin",
         steps=[_TRAFFIC, _turn("traffic", Menu(action="traffic"), 1), ("start", "traffic")], data=_tr(61),
         title="/start traffic после листания — та же стр. 2 из 3"),
    # ── «📊 Трафик» профиля по устройствам ─────────────────────────────────
    Shot("adm.paged.trdev.1", role="admin", start=f"traffic-{FIRST}", data=_trdev(1),
         title="трафик профиля, одно устройство — без листания"),
    Shot("adm.paged.trdev.25", role="admin", start=f"traffic-{FIRST}", data=_trdev(25),
         title="трафик профиля, 25 устройств — одна страница"),
    Shot("adm.paged.trdev.61", role="admin", steps=[("start", ""), ("start", f"traffic-{FIRST}")], data=_trdev(61),
         title="трафик профиля, 61 устройство — стр. 1 из 3"),
    Shot("adm.paged.trdev.61.p2", role="admin",
         steps=[("start", ""), ("start", f"traffic-{FIRST}"), _turn("traffic_dev", _TRDEV, 1, FIRST)],
         data=_trdev(61), title="листание вперёд — стр. 2 из 3, профиль в шапке"),
    Shot("adm.paged.trdev.61.back", role="admin",
         steps=[("start", ""), ("start", f"traffic-{FIRST}"), _turn("traffic_dev", _TRDEV, 1, FIRST),
                ("press", Menu(action="traffic"))],
         data=_trdev(61), title="«⬅️ Назад» — трафик по профилям"),
    # ── «🚚 Переезд»: обзор ─────────────────────────────────────────────────
    Shot("adm.paged.migration.1", role="admin", press=[Menu(action="migration")], data=_mig(1),
         title="обзор переезда, один профиль — без листания"),
    Shot("adm.paged.migration.25", role="admin", press=[Menu(action="migration")], data=_mig(25),
         title="обзор переезда, 25 профилей — одна страница"),
    Shot("adm.paged.migration.61", role="admin", press=[Menu(action="migration")], data=_mig(61),
         title="обзор переезда, 61 профиль — стр. 1 из 3"),
    Shot("adm.paged.migration.61.p3", role="admin",
         steps=[_MIGRATION, *[_turn("migration", Menu(action="migration"), p) for p in (1, 2)]], data=_mig(61),
         title="последняя страница обзора"),
    # ── «🚚 Переезд» профиля ────────────────────────────────────────────────
    Shot("adm.paged.migcl.1", role="admin", start=f"migration-{FIRST}", data=_mig_one(1),
         title="переезд профиля, одно устройство — без листания"),
    Shot("adm.paged.migcl.25", role="admin", start=f"migration-{FIRST}", data=_mig_one(25),
         title="переезд профиля, 25 устройств — одна страница"),
    Shot("adm.paged.migcl.61", role="admin", steps=[("start", ""), ("start", f"migration-{FIRST}")],
         data=_mig_one(61), title="переезд профиля, 61 устройство — стр. 1 из 3"),
    Shot("adm.paged.migcl.61.p2", role="admin",
         steps=[("start", ""), ("start", f"migration-{FIRST}"), _turn("migration_cl", _MIGCL, 1, FIRST)],
         data=_mig_one(61), title="листание вперёд — стр. 2 из 3, счёт профиля в шапке"),
    Shot("adm.paged.migcl.61.back", role="admin",
         steps=[("start", ""), ("start", f"migration-{FIRST}"), _turn("migration_cl", _MIGCL, 1, FIRST),
                ("press", Menu(action="migration"))],
         data=_mig_one(61), title="«⬅️ Назад» — обзор переезда"),
    # ── «🚚 Ещё не переехали» ───────────────────────────────────────────────
    Shot("adm.paged.pending.1", role="admin", press=[SetCB(sec="mig", act="do", key="pending")], data=_mig(1),
         title="не переехал один — без листания"),
    Shot("adm.paged.pending.25", role="admin", press=[SetCB(sec="mig", act="do", key="pending")], data=_mig(25),
         title="не переехали 25 — одна страница, хвоста «и ещё N» нет"),
    Shot("adm.paged.pending.61", role="admin", press=[SetCB(sec="mig", act="do", key="pending")], data=_mig(61),
         title="не переехали 61 — стр. 1 из 3, подсказка под списком"),
    Shot("adm.paged.pending.61.p3", role="admin",
         steps=[_PENDING, *[_turn("mig_pending", SetCB(sec="mig", act="do", key="pending"), p) for p in (1, 2)]],
         data=_mig(61), title="последняя страница — подсказка та же"),
    Shot("adm.paged.pending.61.back", role="admin",
         steps=[_PENDING, _turn("mig_pending", SetCB(sec="mig", act="do", key="pending"), 1),
                ("press", SetCB(sec="svc"))],
         data=_mig(61), title="«⬅️ Назад» — «🔧 Сервис»"),
    # ── «⚠️ Переехали до отмены» ────────────────────────────────────────────
    Shot("adm.paged.orphans.1", role="admin", press=[SetCB(sec="mig", act="do", key="orphans")],
         data=_mig(1, cancelled=True), title="переехал до отмены один — без листания"),
    Shot("adm.paged.orphans.25", role="admin", press=[SetCB(sec="mig", act="do", key="orphans")],
         data=_mig(25, cancelled=True), title="переехали до отмены 25 — одна страница"),
    Shot("adm.paged.orphans.61", role="admin", press=[SetCB(sec="mig", act="do", key="orphans")],
         data=_mig(61, cancelled=True), title="переехали до отмены 61 — стр. 1 из 3"),
    Shot("adm.paged.orphans.61.p2", role="admin",
         steps=[_ORPHANS, _turn("mig_orphans", SetCB(sec="mig", act="do", key="orphans"), 1)],
         data=_mig(61, cancelled=True), title="листание вперёд — стр. 2 из 3"),
]
