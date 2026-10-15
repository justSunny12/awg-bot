"""Снимки админа: канал до шлюза в карточке слота, бот шлюза, файл конфигурации слота 2."""
from __future__ import annotations

from awgbot.bot.callbacks import CancelCB, DeviceCB, GwSlotCB, SetCB
from awgbot.core import config

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import _iso
from tests.screens.shots_admin_common_gw import (LAN2, NASPI, TOKEN2, _active1, _card, _ch, _chan,
                                                 _drop_slot2, _gw, _label, _slots)


def _ch_ack(ok: bool, error: str = ""):
    """Ответ шлюза на текущий набор настроек: отпечаток — как его ставит слушатель
    (набор, ушедший в сессии); отказ на прежний набор карточка не показывает."""
    def step(services, mp):
        from awgbot.util import gwlink
        digest = gwlink.settings_hash(services.gwlink_settings_want(services.db.gateway(1)))
        services.gwlink_ack_in(1, {"ok": ok, "error": error, "hash": digest})
    return step


def _ch_old_agent(delta: bool = False):
    """Снимок агента, который не сообщает установленную конфигурацию."""
    def step(services, mp):
        services.gwlink_snapshot_in(1, {"agent_version": "3.0.0", "egress_ok": True, "ts": _iso(NOW), "rev": 1},
                                    1, True)
        services.gwlink_session_opened(1, "3.0.0", 1)
        if delta:
            services.gwlink_snapshot_in(1, {"plumbing_gen": "old", "rev": 2}, 2, False)
    return step


def _neighbour(services, mp):
    """Слот 2 с VPN-транзитом и подсетью 192.168.70.0/24 — связь подсетей
    его часть: у слота 1 появляется префикс соседа, которого на нём ещё нет."""
    services.db.gateway_update(2, lan_subnets=["192.168.70.0/24"], lan_mode=1)


_PEER_MISSING = {"ok": False, "missing": ["192.168.70.0/24", "192.168.71.0/24"]}


def _lan_off1(services, mp):
    """Слот 1 с подсетью, но VPN-транзит выключен: включение покажет настройку роутера."""
    services.db.gateway_update(1, lan_mode=0)


def _lan_off2(services, mp):
    """Слот 2 с подсетью, но без VPN-транзита: включение его свяжет с сетью слота 1."""
    services.db.gateway_update(2, lan_subnets=["192.168.70.0/24"], lan_mode=0)


# ── шаг 5: бот шлюза и ссылки /start gw-N — ветки из e2e ────────────────────

def _bot(slot: int, username: str, name: str, token: str = TOKEN2):
    """Бот слота известен: токен есть, ответ getMe в кэше."""
    def step(services, mp):
        services._shot_tokens[slot] = token
        services.set_gw_bot_identity(slot, username, name)
    return step


def _token_replaced(services, mp):
    """Токен слота 2 сменили — прежний бот уже не тот."""
    services._shot_tokens[2] = "2222222222:DUMMYANOTHERDUMMYANOTHER"


def _snap_bot(username: str, name: str, rev: int = 1, full: bool = True):
    """Агент слота 2 прислал себя снимком канала (токена на сервере нет)."""
    def step(services, mp):
        body = {"agent_bot": {"username": username, "name": name}, "rev": rev}
        if full:
            body.update(agent_version="3.1.0", mark_status="confirmed", egress_ok=True)
        services.gwlink_snapshot_in(2, body, rev, full)
    return step


def _nav_live(services, mp):
    """В чате уже живое меню (#1) — карточка встанет на его место."""
    services.db.set_nav_message_id(config.ADMIN_ID, 1)


# ── шаг 5: файл конфигурации слота 2 в чате и итоги со шлюза ─────────────────

def _file2(plain: bool):
    """В чате админа лежит файл слота 2 (#40) под карточкой или инструкцией (#39)."""
    def step(services, mp):
        services.gw_bundle_msg_set(2, config.ADMIN_ID, 40, 39, "DUMMYFP", plain)
    return step


def _applied2(ok: bool, reason: str = ""):
    async def ev(services, bot):
        from awgbot.bot.handlers.settings import bundle_applied
        await bundle_applied(bot, services, 2, ok, reason, "DUMMYFP")
    return ev


async def _installed2(services, bot):
    from awgbot.bot.handlers.settings import bundle_installed
    await bundle_installed(bot, services, 2)


def _drop2(services, mp):
    services.db.gateway_delete(2)


_LABEL_CANCEL = CancelCB(kind="gwedit", ref=2)
_CARD2 = GwSlotCB(action="card", slot=2)


CHANNEL = [
    # ── канал: связь, возраст, сверка ────────────────────────────────────
    _card(1, _chan(1, _ch()), "канал жив, конфигурация актуальна — одной строкой", "adm.gw.card.ch.live"),
    _card(1, _chan(1, _ch(online=False, closed=True, age_h=13 * 24)),
          "канал лежит — последнее известное с возрастом снимка", "adm.gw.card.ch.dead"),
    _card(1, _chan(1, _ch({"home_subnets": LAN2, "resolver": ""}, online=False, closed=True, age_h=2)),
          "канал лежит, конфигурация разошлась — пункты и возраст", "adm.gw.card.ch.drift.dead"),
    _card(1, _chan(1, _ch({"home_subnets": "<b>x</b> & y"}, online=False, closed=True)),
          "пункт расхождения со шлюза экранирован", "adm.gw.card.ch.drift.escaped"),
    _card(1, _chan(1, _ch({"home_subnets": LAN2, "resolver": ""})),
          "канал жив, расхождение уходит каналом", "adm.gw.card.ch.drift.live"),
    _card(1, _chan(1, _ch({"home_subnets": LAN2}), _ch_ack(False, "<b>LAN-интерфейс</b> не найден")),
          "шлюз не применил настройки — причина текстом", "adm.gw.card.ch.refused"),
    _card(1, _chan(1, _ch({"home_subnets": LAN2}, online=False), _ch_ack(False, "exit 1"), _ch(
        {"home_subnets": LAN2}, online=False, closed=True)),
          "старый отказ при лежащем канале — снова «расходится»", "adm.gw.card.ch.refused.dead"),
    _card(1, _chan(1, _ch(), _ch_ack(False, "exit 1"), _ch_ack(True)),
          "следующее применение прошло — отказа нет", "adm.gw.card.ch.refused.cleared"),
    _card(1, _chan(1, _ch(plumbing_gen="old", link_contract="")),
          "обвязка старого образца — строкой под каналом", "adm.gw.card.ch.plumbing.old"),
    _card(1, _chan(1, _ch(plumbing_gen="none")), "обвязка не развёрнута", "adm.gw.card.ch.plumbing.none"),
    _card(1, _chan(1, _ch(plumbing_gen="")), "образец обвязки не сообщён — строки нет",
          "adm.gw.card.ch.plumbing.unknown"),
    _card(1, _chan(1, _ch_old_agent()), "снимок без установленной конфигурации — не галочка",
          "adm.gw.card.ch.old_agent"),
    _card(1, _chan(1, _ch_old_agent(delta=True)), "снимок без установленного — и об обвязке не судим",
          "adm.gw.card.ch.old_agent.plumbing"),
    _card(1, _chan(1, _ch(awg_generation=7)), "поколение AWG на шлюзе другое", "adm.gw.card.ch.generation"),
    _card(1, _chan(1, _ch(agent_version="<b>3.1.0</b>")), "версия агента с разметкой — текстом",
          "adm.gw.card.ch.html"),
    _card(1, _chan(1, _ch(agent_version="3.1.0-" + "x" * 200)), "длинная версия агента обрезана",
          "adm.gw.card.ch.long_version"),
    _card(1, _chan(1, _ch(skew_min=10)), "часы шлюза спешат на 10 мин", "adm.gw.card.ch.clock"),
    Shot("adm.gw.router.addr", role="admin", press=[GwSlotCB(action="router", slot=1)],
         data=_chan(1, _ch(lan_addr="192.168.1.10")),
         title="настройка роутера: адрес шлюза из снимка канала — без плейсхолдера и подсказки"),
    Shot("adm.gw.lan_yes.router", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="1")],
         data=_chan(1, _ch(lan_addr="192.168.1.10"), _lan_off1),
         title="VPN-транзит включён — сразу «🛜 Настройка роутера» с адресом шлюза"),
    _card(1, _chan(1, _ch(peer_nets=_PEER_MISSING)), "на шлюзе нет подсетей соседа — названы",
          "adm.gw.card.ch.peer_missing"),
    _card(1, _chan(1, _ch(peer_nets={"ok": True, "missing": []})), "вердикт связи подсетей исправен — строки нет",
          "adm.gw.card.ch.peer_ok"),
    _card(1, _chan(2, _neighbour, _ch()),
          "подсети соседа не применены, канал жив — только файлом", "adm.gw.card.ch.neighbour.live"),
    _card(1, _chan(2, _neighbour, _ch(online=False, closed=True)),
          "подсети соседа не применены, канал лежит — пункт", "adm.gw.card.ch.neighbour.dead"),
    _card(1, _chan(2, _neighbour, _ch({"home_subnets": LAN2})),
          "смешанное расхождение: что везёт канал, что — файл", "adm.gw.card.ch.neighbour.mixed"),
    # ── тумблеры при живом и лежащем канале ──────────────────────────────
    Shot("adm.gw.lan_ask.ch.live", role="admin", press=[GwSlotCB(action="lan_ask", slot=1)],
         data=_chan(1, _ch()), title="выключить VPN-транзит при живом канале — без перевыпуска"),
    Shot("adm.gw.lan_yes.ch.live", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="0")],
         data=_chan(1, _ch()), title="VPN-транзит выключен — доедет каналом"),
    Shot("adm.gw.lan_ask.ch.dead", role="admin", press=[GwSlotCB(action="lan_ask", slot=1)],
         data=_chan(1, _ch(online=False)), title="выключить VPN-транзит без канала — перевыпуск"),
    Shot("adm.gw.lan_yes.ch.dead", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="0")],
         data=_chan(1, _ch(online=False)), title="VPN-транзит выключен без канала — перевыпусти"),
    Shot("adm.gw.lan_ask.ch.peers", role="admin", press=[GwSlotCB(action="lan_ask", slot=1)],
         data=_chan(2, _neighbour, _ch()), title="выключить VPN-транзит: кому снять связь подсетей файлом"),
    Shot("adm.gw.lan_yes.ch.peers", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="0")],
         data=_chan(2, _neighbour, _ch()), title="VPN-транзит выключен каналом, но подсети соседей — файлом"),
    Shot("adm.gw.lan_ask.peers", role="admin", press=[GwSlotCB(action="lan_ask", slot=2)],
         data=_chan(2, _ch(), _lan_off2), title="включить VPN-транзит рядом с другим — сети свяжутся, названы"),
    # ── бот шлюза в карточках и подписях файла ───────────────────────────
    _card(2, _gw(2, _bot(2, "pi2_gw_bot", "Шлюз <Pi2>"), token=False),
          "бот слота известен — ссылка последней строкой, имя экранировано", "adm.gw.card.bot"),
    _card(1, _gw(2, _bot(2, "pi2_gw_bot", "Шлюз <Pi2>"), token=False),
          "бот известен только у соседнего слота — здесь строки нет", "adm.gw.card.nobot"),
    _card(2, _gw(2, _bot(2, "pi2_gw_bot", "")), "у бота нет имени — ссылка подписана username",
          "adm.gw.card.bot.username"),
    _card(2, _gw(2, _token_replaced), "токен слота сменили — ссылки на прежнего бота нет",
          "adm.gw.card.token_replaced"),
    _card(2, _gw(2, _snap_bot("pi2_gw_bot", "Шлюз <Pi2>"), token=False),
          "бот из снимка канала (токена на сервере нет)", "adm.gw.card.snapbot"),
    _card(2, _gw(2, _snap_bot("pi2_gw_bot", "Шлюз <Pi2>"), _snap_bot("pi2_gw_bot", "Новое имя", 2, False),
                 token=False), "бот из снимка переименован — дельтой", "adm.gw.card.snapbot.renamed"),
    _card(2, _gw(2, _snap_bot("pi2_gw_bot", "Шлюз <Pi2>"), _snap_bot("", "", 2, False), token=False),
          "в снимке пустой username — строки нет", "adm.gw.card.snapbot.gone"),
    _card(2, _gw(2, _snap_bot("snap_bot", "Из снимка"), _bot(2, "pi2_gw_bot", "Шлюз <Pi2>"), token=False),
          "ответ getMe по токену важнее снимка", "adm.gw.card.snapbot.token"),
    Shot("adm.gw.dev.nobot", role="admin", press=[DeviceCB(action="open", device_id=NASPI)],
         data=_slots(1, token=False), title="устройство-шлюз, бот неизвестен — без строки бота"),
    Shot("adm.gw.bundle.escaped", role="admin", press=[GwSlotCB(action="bundle", slot=2)],
         data=_gw(2, _label(2, "дом <2>"), _bot(2, "pi2_gw_bot", "Шлюз <Pi2>")),
         title="подпись файла: подпись слота и имя бота экранированы"),
    Shot("adm.gw.bundle.username", role="admin", press=[GwSlotCB(action="bundle", slot=2)],
         data=_gw(2, _bot(2, "pi2_gw_bot", "")), title="подпись файла: у бота нет имени — username"),
    Shot("adm.gw.bundle.token_replaced", role="admin", press=[GwSlotCB(action="bundle", slot=2)],
         data=_gw(2, _token_replaced), title="подпись файла после смены токена — без ссылки"),
    Shot("adm.gw.bundle.snapbot", role="admin", press=[GwSlotCB(action="bundle", slot=2)],
         data=_gw(2, _snap_bot("pi2_gw_bot", "Шлюз <Pi2>"), token=False),
         title="подпись файла: бот из снимка канала"),
    # ── /start gw-N ──────────────────────────────────────────────────────
    Shot("adm.link.gw.nav", role="admin", steps=[("start", ""), ("start", "gw-2")], data=_slots(2),
         title="карточка по ссылке — на место живого меню"),
    Shot("adm.link.gw.standby", role="admin", start="gw-2", data=_slots(2),
         title="карточка резерва по ссылке — пинг не меряется, но и «не отвечает» нет"),
    Shot("adm.link.gw.zero", role="admin", start="gw-0", data=_slots(1), title="gw-0 — слот снят"),
    Shot("adm.link.gw.garbage", role="admin", start="gw-abc", data=_slots(1), title="gw-abc — обычный /start"),
    Shot("adm.link.gw.one", role="admin", start="gw-1", data=_slots(1),
         title="один шлюз по ссылке — «Назад» на главную"),
    Shot("adm.link.gw.ping", role="admin", steps=[("start", "gw-2"), ("press", GwSlotCB(action="ping", slot=2))],
         data=_slots(2), title="карточка с главной после пинга — «Назад» на главную"),
    Shot("adm.link.gw.pref", role="admin",
         steps=[("start", "gw-2"), ("press", GwSlotCB(action="edit", slot=2)),
                ("press", GwSlotCB(action="pref", slot=2)), ("press", _CARD2)],
         data=_gw(2, _active1), title="карточка с главной после галочки «При старте» — «Назад» на главную"),
    Shot("adm.link.gw.label", role="admin",
         steps=[("start", "gw-2"), ("press", GwSlotCB(action="label", slot=2)), ("text", "дача"),
                ("press", _CARD2)],
         data=_slots(2), title="карточка с главной после ввода подписи — «Назад» на главную"),
    Shot("adm.link.gw.label_cancel", role="admin",
         steps=[("start", "gw-2"), ("press", GwSlotCB(action="label", slot=2)), ("press", _LABEL_CANCEL),
                ("press", _CARD2)],
         data=_slots(2), title="карточка с главной после отмены подписи — «Назад» на главную"),
    # ── файл слота 2: «В меню» и итоги со шлюза ───────────────────────────
    Shot("adm.gw.bundle.card.gone", role="admin",
         steps=[("press", GwSlotCB(action="bundle", slot=2)), ("call", _drop_slot2, "слот снят"),
                ("press", SetCB(sec="rt", act="do", key="bundle_cancel", val="2"))],
         data=_slots(2), title="«🛰 В карточку» под файлом снятого слота — главная"),
    Shot("adm.ev.gw_applied.label", role="admin", call=("bundle_applied", _applied2(True)),
         data=_gw(2, _label(2, "дом 2"), _file2(False)), title="файл слота 2 применён — имя с подписью"),
    Shot("adm.ev.gw_applied.gone", role="admin", call=("bundle_applied", _applied2(True)),
         data=_gw(2, _file2(False), _drop2), title="итог по снятому слоту — уведомление и главная"),
    Shot("adm.ev.gw_applied.fail.escaped", role="admin", call=("bundle_applied", _applied2(False, "нет <места> & прав")),
         data=_gw(2, _file2(False)), title="отказ шлюза — причина экранирована"),
    Shot("adm.ev.gw_applied.fail.bare", role="admin", call=("bundle_applied", _applied2(False)),
         data=_gw(2, _file2(False)), title="отказ шлюза без причины — без двоеточия"),
    Shot("adm.ev.gw_installed.label", role="admin", call=("bundle_installed", _installed2),
         data=_gw(2, _label(2, "дом 2"), _bot(2, "pi2_gw_bot", "Шлюз <Pi2>"), _file2(True)),
         title="шлюз слота 2 настроен — подпись слота и экранированное имя бота"),
    Shot("adm.ev.gw_installed.nobot", role="admin", call=("bundle_installed", _installed2),
         data=_gw(2, _file2(True), token=False), title="шлюз настроен, бот неизвестен — без строки бота"),
    Shot("adm.ev.gw_installed.nofile", role="admin", call=("bundle_installed", _installed2),
         data=_gw(2, _nav_live, token=False), title="шлюз настроен, файла в чате нет — одно уведомление"),
    Shot("adm.ev.gw_installed.enc", role="admin", call=("bundle_installed", _installed2),
         data=_gw(2, _file2(False), token=False),
         title="шлюз настроен, а в чате уже шифрованный файл — файл остаётся"),
]
