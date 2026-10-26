"""Снимки админа: «🛟 Резерв» — раздел без пары (Т-Р1: с доменом и без, с IP,
со шлюзом на агенте с rsv1 и на прежнем), приглашение и его ход (Т-Р2),
ввод кода (Т-Р7), отмена, принятие строки (Т-Р3 → Т-Р4/Т-Р5), код у
принявшего (Т-Р6), карточка резерва (Т-Р9), диалоги готовности и удаления,
«🆘 На крайний случай» (Т-Р18) и экран роли админа второго сервера (Т-РН1)
— во всех вариантах этапа пары.

Хост не трогается: скрипт линка пары — подмена с постоянными ключами-
заглушками, файервол — пустая подмена, id приглашения и ключи переезда —
постоянные (иначе строка приглашения и аварийный код менялись бы от
прогона к прогону). «Резерв подключился» — запись сопряжения так, как её
сделал бы слушатель канала по hello, и событие тем же крючком.
"""
from __future__ import annotations

import base64
import types

from awgbot.bot.callbacks import PeerCB, RsvCB, SetCB

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import _iso, _quiet

PEER_ADMIN = 424242
OWN_IP = "203.0.113.5"          # адрес этого сервера в снимках
PID = "0123456789abcdef"
_K = lambda b: base64.b64encode(bytes([b]) * 32).decode()            # noqa: E731 — ключ-заглушка из одного байта
STAGE = {"PAIR_VPS_PRIV": _K(1), "PAIR_VPS_PUB": _K(2), "PAIR_PEER_PRIV": _K(3), "PAIR_PEER_PUB": _K(4),
         "JC": "5", "JMIN": "10", "JMAX": "50", "S1": "20", "S2": "30", "S3": "40", "S4": "50",
         "H1": "100-200", "H2": "600000000-600100000", "H3": "1100000000-1100100000", "H4": "1600000000-1600100000"}
KEYS = {"m": "11" * 16, "a": "22" * 16, "m_gen": 1, "k": "33" * 32, "k_gen": 1, "compromised": False,
        "created_at": "2026-09-14T12:00:00+03:00"}


def _host(services, mp):
    """Хост пары подменой: скрипт линка пары, файервол, поколение AWG, id приглашения."""
    from awgbot.bot.handlers.settings.reserve import events
    from awgbot.domain.services.reserve import invite as invitemod
    mp.setattr(services, "_run_pair_script", lambda mode, env=None: dict(STAGE) if mode == "--stage" else {})
    mp.setattr(services, "_gw_firewall_refresh", lambda strict=False: None)
    mp.setattr(services, "_awg_generation", lambda: 1)
    mp.setattr(invitemod, "secrets", types.SimpleNamespace(token_hex=lambda n: PID[:2 * n]))
    # адрес сервера и запись DNS — без сети: резолвер и авторитетный сервер отвечают заглушкой
    from awgbot.util import dnsapi
    mp.setattr(dnsapi, "read_system", lambda name, timeout=3.0: [OWN_IP])
    mp.setattr(dnsapi, "read_authoritative", lambda name: [OWN_IP])
    mp.setattr(dnsapi, "read_authoritative_ttl", lambda name: (list(dnsapi.read_authoritative(name)), 60))
    # пакет второй стороны у этого сервера есть (направление «ко мне» не упирается в «данных нет»)
    mp.setattr(services, "reserve_pkg_in_present", lambda: True)

    async def no_delete(*a, **k):
        return None
    mp.setattr(events, "_delete_later", no_delete)


def _rsv(*, host: str = "vpn.example.org", ready: bool = False, gw: str = "none", paired: bool = False,
         then=()):
    """Сервер без пары (или с ней — paired): адрес для пользователей host, тумблер
    готовности, шлюз слота 1 — none (нет), rsv (агент с rsv1), old (агент
    3.4.0 без rsv1); then — шаги (services, mp) поверх."""
    def build(services, mp):
        from awgbot.core import settings
        who = _quiet(services)
        _host(services, mp)
        settings.set_value("app.network.server_host", host)
        settings.set_value("app.reserve.ready", ready)
        if gw != "none":
            from tests.screens.shots_admin_common import ADM
            dev = services.add_device(ADM, "NASPi").device_id
            services.db.gateway_add(dev, "awglink", 443, "10.99.99.0/30", slot_id=1)
            if gw == "rsv":
                services.db.set_state("gwlink_caps_1", "cfg1,own1,rsv1")
            else:
                services.db.set_state("gwlink_session_1", f"since={_iso(NOW)} agent=3.4.0 proto=2 caps=cfg1,own1")
                services.db.set_state("gwlink_caps_1", "cfg1,own1")
        if paired:
            _pair(services)
        for step in then:
            step(services, mp)
        return who
    return build


def _guest(*, alive: bool = False):
    """Бот основы на резерве: config.GUEST на снимок и состояние от бота резерва."""
    def build(services, mp):
        from awgbot.core import config
        who = _quiet(services)
        mp.setattr(config, "GUEST", True)
        services.guest_state_in({"name": "Beta", "since": _iso(NOW.replace(hour=3, minute=12)), "reason": "dead",
                                 "own_name": "Alpha", "return": "button" if alive else "immediate",
                                 "primary": {"status": "alive", "healthy": True, "can_return": True,
                                             "since": _iso(NOW.replace(hour=9, minute=40))} if alive else
                                 {"status": "dead", "since": _iso(NOW.replace(hour=3, minute=10))}})
        return who
    return build


def _pair(services):
    """Пара записана (как после сопряжения): «Beta» на v1.2.3 с шлюзом,
    готова, ключи переезда первого поколения — постоянные."""
    from awgbot.domain.services.reserve import compat
    services.db.reserve_peer_set(name="Beta", host="198.51.100.7", pair_if="awgpair", pair_port=47101, peer_port=0,
                                 pair_cidr="10.99.98.0/30", listener=True, bot_username="beta_vpn_bot",
                                 peer_admin_id=PEER_ADMIN)
    services.rsv_put("rsv_peer_state", {**compat.side(name="Beta", version="1.2.3", caps=list(compat.CAPS), gen=1,
                                                      ports=[45871], ready=True, has_gateway=True),
                                        "session": True, "at": _iso(NOW)})
    services.rsv_put("rsv_peer_admin", {"admin_id": PEER_ADMIN, "opened": False, "bound_at": _iso(NOW)})
    services.rsv_put("rsv_keys", dict(KEYS))
    services.reserve_set_out("home")
    services.reserve_set_in("idle")


def _connected(*addrs: str):
    """Резерв подключился к приглашению с адресов addrs (как записал бы слушатель
    канала по hello), событие — в живое сообщение тем же крючком."""
    async def step(services, bot):
        from awgbot.bot.handlers.settings.reserve import events
        from awgbot.domain.services.reserve import invite as invitemod
        from tests.integration._reserve_common import Acceptor
        out = {}
        for a in addrs:
            out = services.reserve_pair_hello(Acceptor(PID).hello(addr=a))
        invitemod._live_set({**invitemod._live(), "code": "K7F2QX"})
        await events.on_event(bot, services, out["event"], out["st"], {})
    return step


def _fixed_generation(services, mp):
    """Новое поколение — с постоянными ключами (иначе новый код менялся бы от прогона к прогону)."""
    from awgbot.util import rsvkeys, shamir
    from awgbot.util.rsvpkg import seal

    class _Gen:
        @staticmethod
        def new(n):
            return type("G", (), {"m": bytes([0x44]) * 16, "gen": n,
                                  "line": shamir.Line(bytes([0x44]) * 16, bytes([0x55]) * 16)})()
    mp.setattr(rsvkeys, "Generation", _Gen)
    mp.setattr(seal, "new_k", lambda: bytes([0x66]) * 32)


def _peer_down(services, mp):
    from awgbot.domain.services.reserve import liveness
    services.__dict__["_reserve_liveness"] = liveness.Liveness(state="down")
    services.rsv_update("rsv_peer_state", session=False, down_since=_iso(NOW.replace(hour=11, minute=40)))


def _away(services, mp):
    services.reserve_set_out("away")


def _serving(services, mp):
    services.reserve_set_in("serving")
    services.rsv_put("rsv_pkg_in", {"at": _iso(NOW.replace(hour=11, minute=30)), "gen": 3, "fp": "ab" * 8})
    services.rsv_put("rsv_traffic", {"in": {"month": NOW.strftime("%Y-%m"), "rx": 6_100_000_000, "tx": 400_000_000}})


def _not_ready(services, mp):
    from awgbot.core import settings
    settings.set_value("app.reserve.ready", False)


def _token(**over) -> str:
    """Строка приглашения «Beta» с постоянными ключами-заглушками."""
    from awgbot.util import pairtoken
    kw = dict(version="1.2.3", pair_id="a1b2c3d4e5f60718", expires=int(NOW.timestamp()) + 3600,
              host="vpn-b.example.net", pair_port=47101, peer_addr="10.99.98.1", inviter_pub=bytes([5]) * 32,
              peer_priv=bytes([6]) * 32, jc=5, jmin=10, jmax=50, s=(20, 30, 40, 50),
              h=((100, 200), (600000000, 600100000), (1100000000, 1100100000), (1600000000, 1600100000)), awg_gen=1)
    kw.update(over)
    return pairtoken.encode(pairtoken.PairToken(**kw))


def _accept_code(services, bot):
    """Код у принявшего — событие хода в его живое сообщение."""
    async def run():
        from awgbot.bot.handlers.settings.reserve import events
        from awgbot.core import config
        await events.on_event(bot, services, "code", {"role": "acceptor", "peer_name": "Beta",
                                                      "msg": [config.ADMIN_ID, 1]}, {"code": "K7F2QX"})
    return run()


def _peer_admin(*then):
    """Экран роли: действует админ «Beta» (PEER_ADMIN) в боте этого сервера."""
    def build(services, mp):
        _rsv(ready=True, gw="rsv", paired=True, then=then)(services, mp)
        mp.setattr(services, "server_ok", lambda: True)
        return PEER_ADMIN, "Админ Beta"
    return build


RSV = SetCB(sec="rsv")
CODE = RsvCB(action="code", pid=PID)

RESERVE = [
    # ── раздел без пары (Т-Р1) ───────────────────────────────────────────
    Shot("adm.rsv", role="admin", press=[RSV], data=_rsv(), title="«🛟 Резерв» без пары: домен, шлюзов нет"),
    Shot("adm.rsv.nohost", role="admin", press=[RSV], data=_rsv(host=""), title="адрес для пользователей не задан"),
    Shot("adm.rsv.ip", role="admin", press=[RSV], data=_rsv(host="203.0.113.5"), title="адрес для пользователей — IP"),
    Shot("adm.rsv.gw", role="admin", press=[RSV], data=_rsv(ready=True, gw="rsv"),
         title="шлюз на агенте с rsv1, готовность включена"),
    Shot("adm.rsv.gw_old", role="admin", press=[RSV], data=_rsv(ready=True, gw="old"),
         title="шлюз на прежнем агенте — обнови"),
    Shot("adm.rsv.ready", role="admin", press=[RSV, RsvCB(action="ready")], data=_rsv(),
         title="тумблер готовности без пары — сразу"),
    # ── приглашение (Т-Р2) и код (Т-Р7) ─────────────────────────────────
    Shot("adm.rsv.invite", role="admin", press=[RSV, RsvCB(action="invite")], data=_rsv(ready=True, gw="rsv"),
         title="«📤 Пригласить»: живое сообщение и раздел"),
    Shot("adm.rsv.invite.connected", role="admin", steps=[("press", RSV), ("press", RsvCB(action="invite")),
                                                          ("call", _connected("198.51.100.7"), "подключился")],
         data=_rsv(ready=True, gw="rsv"), title="резерв подключился"),
    Shot("adm.rsv.invite.two", role="admin",
         steps=[("press", RSV), ("press", RsvCB(action="invite")),
                ("call", _connected("198.51.100.7", "203.0.113.44"), "с двух адресов")],
         data=_rsv(ready=True, gw="rsv"), title="к приглашению подключались с двух адресов"),
    Shot("adm.rsv.code.wait", role="admin", press=[RSV, RsvCB(action="invite"), CODE],
         data=_rsv(ready=True, gw="rsv"), title="«✏️ Ввести код» до подключения резерва — всплывашка"),
    Shot("adm.rsv.code.ask", role="admin",
         steps=[("press", RSV), ("press", RsvCB(action="invite")), ("call", _connected("198.51.100.7"), "подключился"),
                ("press", CODE)], data=_rsv(ready=True, gw="rsv"), title="приглашение кода"),
    Shot("adm.rsv.code.format", role="admin",
         steps=[("press", RSV), ("press", RsvCB(action="invite")), ("call", _connected("198.51.100.7"), "подключился"),
                ("press", CODE), ("text", "K7F")], data=_rsv(ready=True, gw="rsv"), title="не тот формат кода"),
    Shot("adm.rsv.code.wrong", role="admin",
         steps=[("press", RSV), ("press", RsvCB(action="invite")), ("call", _connected("198.51.100.7"), "подключился"),
                ("press", CODE), ("text", "AAAAAA")], data=_rsv(ready=True, gw="rsv"), title="код не совпал"),
    Shot("adm.rsv.code.refused", role="admin",
         steps=[("press", RSV), ("press", RsvCB(action="invite")), ("call", _connected("198.51.100.7"), "подключился"),
                ("press", CODE), ("text", "AAAAAA"), ("text", "BBBBBB"), ("text", "CCCCCC")],
         data=_rsv(ready=True, gw="rsv"), title="третий неверный код — приглашение закрыто"),
    Shot("adm.rsv.code.ok", role="admin",
         steps=[("press", RSV), ("press", RsvCB(action="invite")), ("call", _connected("198.51.100.7"), "подключился"),
                ("press", CODE), ("text", "k7f-2qx")], data=_rsv(ready=True, gw="rsv"), title="код верный"),
    Shot("adm.rsv.cancel", role="admin", press=[RSV, RsvCB(action="invite"), RsvCB(action="cancel", pid=PID)],
         data=_rsv(ready=True, gw="rsv"), title="«⬅️ Отменить» приглашение"),
    # ── принятие (Т-Р3 → Т-Р4 / Т-Р5, Т-Р6) ─────────────────────────────
    Shot("adm.rsv.accept", role="admin", press=[RSV, RsvCB(action="accept")], data=_rsv(ready=True, gw="rsv"),
         title="«📥 Принять приглашение»: приглашение строки"),
    Shot("adm.rsv.accept.dialog", role="admin",
         steps=[("press", RSV), ("press", RsvCB(action="accept")), ("text", _token())],
         data=_rsv(ready=True, gw="rsv"), title="диалог принятия: «Отмена» первой, РФ-доступ"),
    Shot("adm.rsv.accept.rf", role="admin",
         steps=[("press", RSV), ("press", RsvCB(action="accept")), ("text", _token()), ("press", RsvCB(action="rf"))],
         data=_rsv(ready=True, gw="rsv"), title="диалог принятия: РФ-доступ выключен"),
    Shot("adm.rsv.accept.nogw", role="admin",
         steps=[("press", RSV), ("press", RsvCB(action="accept")), ("text", _token(version="1.3.0"))],
         data=_rsv(ready=True), title="у принявшего шлюзов нет, пригласивший новее — предупреждения"),
    Shot("adm.rsv.accept.garbage", role="admin",
         steps=[("press", RSV), ("press", RsvCB(action="accept")), ("text", "привет")],
         data=_rsv(ready=True, gw="rsv"), title="не приглашение — переспрос"),
    Shot("adm.rsv.accept.code", role="admin", steps=[("press", RSV), ("call", _accept_code, "код у принявшего")],
         data=_rsv(ready=True, gw="rsv"), title="код у принявшего в живом сообщении"),
    # ── карточка (Т-Р9) и действия ──────────────────────────────────────
    Shot("adm.rsv.card", role="admin", press=[RSV], data=_rsv(ready=True, gw="rsv", paired=True),
         title="карточка резерва"),
    Shot("adm.rsv.card.down", role="admin", press=[RSV], data=_rsv(ready=True, gw="rsv", paired=True,
                                                                   then=[_peer_down]),
         title="карточка: резерв недоступен"),
    Shot("adm.rsv.card.nogw", role="admin", press=[RSV], data=_rsv(ready=True, paired=True),
         title="карточка: своих шлюзов нет — направление не готово, РФ-тумблера нет"),
    Shot("adm.rsv.ready_off", role="admin", press=[RSV, RsvCB(action="ready")],
         data=_rsv(ready=True, gw="rsv", paired=True), title="выключить готовность при паре — диалог"),
    Shot("adm.rsv.ready_off.yes", role="admin", press=[RSV, RsvCB(action="ready"), RsvCB(action="ready_off_yes")],
         data=_rsv(ready=True, gw="rsv", paired=True), title="готовность выключена"),
    Shot("adm.rsv.rfmine", role="admin", press=[RSV, RsvCB(action="rfmine")],
         data=_rsv(ready=True, gw="rsv", paired=True), title="Мои шлюзы для его РФ-доступа: выкл"),
    Shot("adm.rsv.ret", role="admin", press=[RSV, RsvCB(action="ret")],
         data=_rsv(ready=True, gw="rsv", paired=True), title="рычаг возврата: по кнопке"),
    Shot("adm.rsv.unpair", role="admin", press=[RSV, RsvCB(action="unpair")],
         data=_rsv(ready=True, gw="rsv", paired=True), title="удалить резерв? — диалог"),
    Shot("adm.rsv.unpair.away", role="admin", press=[RSV, RsvCB(action="unpair")],
         data=_rsv(ready=True, gw="rsv", paired=True, then=[_away]), title="удаление при «мои у него» — отказ"),
    Shot("adm.rsv.unpair.yes", role="admin", press=[RSV, RsvCB(action="unpair"), RsvCB(action="unpair_yes")],
         data=_rsv(ready=True, gw="rsv", paired=True), title="резерв удалён"),
    # ── на крайний случай (Т-Р18) ───────────────────────────────────────
    Shot("adm.rsv.emerg", role="admin", press=[RSV, RsvCB(action="emerg")],
         data=_rsv(ready=True, gw="rsv", paired=True), title="«🆘 На крайний случай»"),
    Shot("adm.rsv.emerg.none", role="admin", press=[RSV, RsvCB(action="emerg")], data=_rsv(ready=True),
         title="«🆘 На крайний случай» без пары — кода нет"),
    Shot("adm.rsv.emerg.show", role="admin", press=[RSV, RsvCB(action="emerg"), RsvCB(action="show_code")],
         data=_rsv(ready=True, gw="rsv", paired=True), title="«🔑 Показать код» — отдельным сообщением"),
    Shot("adm.rsv.new_code", role="admin", press=[RSV, RsvCB(action="emerg"), RsvCB(action="new_code")],
         data=_rsv(ready=True, gw="rsv", paired=True), title="«🔄 Сменить код» — диалог"),
    Shot("adm.rsv.new_code.yes", role="admin",
         press=[RSV, RsvCB(action="emerg"), RsvCB(action="new_code"), RsvCB(action="new_code_yes")],
         data=_rsv(ready=True, gw="rsv", paired=True, then=[_fixed_generation]), title="код сменён"),
    # ── бот основы на резерве (гостевой режим): главная, корень настроек, карточка ──
    Shot("adm.rsv.guest.main", role="admin", start="", data=_guest(), title="бот основы на резерве: главная с первой строкой"),
    Shot("adm.rsv.guest.root", role="admin", press=[SetCB(sec="root")], data=_guest(), title="резерв: «⚙️ Настройки» — одна карточка"),
    Shot("adm.rsv.guest.card", role="admin", press=[RSV], data=_guest(), title="резерв: карточка, основа не отвечает"),
    Shot("adm.rsv.guest.return", role="admin", press=[RSV], data=_guest(alive=True),
         title="резерв: основа в строю, рычаг «по кнопке» — «↩️ Вернуть пользователей»"),
    Shot("adm.rsv.guest.refused", role="admin", press=[SetCB(sec="backup")], data=_guest(), title="резерв: раздел хоста — всплывашка"),
    # ── роль админа второго сервера (Т-РН1) ─────────────────────────────
    Shot("adm.rsv.peer", role="admin", start="", data=_peer_admin(),
         title="админ «Beta» в этом боте: всё готово"),
    Shot("adm.rsv.peer.down", role="admin", start="", data=_peer_admin(_peer_down),
         title="админ «Beta»: линк пары молчит, сервер не отвечает"),
    Shot("adm.rsv.peer.not_ready", role="admin", start="", data=_peer_admin(_not_ready),
         title="админ «Beta»: этот сервер не готов быть резервом"),
    Shot("adm.rsv.peer.serving", role="admin", start="", data=_peer_admin(_serving),
         title="админ «Beta»: его пользователи здесь, данные и трафик"),
    Shot("adm.rsv.peer.refresh", role="admin", steps=[("start", ""), ("press", PeerCB(action="refresh"))],
         data=_peer_admin(), title="админ «Beta»: «🔄 Обновить»"),
    Shot("adm.rsv.peer.text", role="admin", steps=[("start", ""), ("text", "что тут?")],
         data=_peer_admin(), title="админ «Beta»: любой текст — тот же экран"),
]
