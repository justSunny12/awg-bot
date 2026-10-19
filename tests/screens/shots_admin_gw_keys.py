"""Снимки админа: документ конфигурации (cfg1), смена ключей линка, аварийная смена токена бота шлюза."""
from __future__ import annotations

import base64
import datetime as _dt

from awgbot.bot.callbacks import CancelCB, GwSlotCB, SetCB
from awgbot.core import config

from tests.screens.base import NOW, Shot, in_zone
from tests.screens.shots_admin_common import _iso
from tests.screens.shots_admin_common_gw import (TOKEN, _card, _ch, _ch_base, _chan, _gw, _installed, _label,
                                                 _slots)


# ── документ конфигурации (cfg1) в карточке слота — ветки из e2e ────────────
# Агент слота 1 принимает документ: сессия открыта с возможностью cfg1,
# снимок несёт поле cfg — отпечатки желаемого ВПС с правками по сценарию.
# Конфиг линка в снимках не собирается (источника /root/gw-awglink.conf нет);
# почта ВПС — подстановкой там, где о ней сценарий.

_CFG_MAIL = {"login": "box@example.org", "password": "DUMMY", "imap_host": "imap.example.org",
             "imap_port": 993, "smtp_host": "smtp.example.org", "smtp_port": 465}


def _cfg_base(services, mp):
    services.gwcfg_link_conf = lambda gw, env: ""


def _cfg_mail(services, mp):
    """На ВПС есть почта."""
    services.gwcfg_mail = lambda: dict(_CFG_MAIL)


def _cfg(*, online: bool = True, closed: bool = False, age_h: float = 0, caps=("cfg1", "confirm1", "token1"),
         agent: str = "3.3.0", **over):
    """Снимок агента с документом: установленное — как выдаёт ВПС, поле cfg —
    отпечатки желаемого с правками over; online — сессия открыта (агент с
    возможностями caps; пусто — ниже 3.3.0); closed — закрыта после снимка;
    age_h — снимок принят столько часов назад."""
    def step(services, mp):
        at = NOW - _dt.timedelta(hours=age_h)
        doc = services.gwcfg_doc(services.db.gateway(1))
        snap = {"bundle": _installed(services), "link_contract": "1",
                "mark_status": "confirmed", "agent_version": agent, "awg_generation": 1, "egress_ok": True,
                "boot_id": "b" * 36, "ts": _iso(at), "rev": 1,
                "cfg": {"gen": doc["gen"], **doc["prints"], **over}}
        services.gwlink_snapshot_in(1, snap, 1, True)
        services.db.set_state(services._gwlink_key(services._GWLINK_SNAP_AT_KEY, 1), _iso(at))
        if online or closed:
            services.gwlink_session_opened(1, agent, 2, list(caps))
        if closed:
            services.gwlink_session_closed(1)
    return step


def _cfg_ack(*, applied=(), wait=(), declined=(), failed=None):
    """Ответ шлюза на текущий документ: wait и declined — разделы, отпечатки
    берутся у желаемого ВПС (о каком значении спросили)."""
    def step(services, mp):
        doc = services.gwcfg_doc(services.db.gateway(1))
        services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"], "applied": list(applied),
                                  "wait": {s: doc["prints"][s] for s in wait},
                                  "declined": {s: doc["prints"][s] for s in declined},
                                  "failed": dict(failed or {})})
    return step


def _cfg_chan(*steps):
    return _chan(1, _cfg_base, *steps)


CFG = [
    _card(1, _cfg_chan(_cfg(), _cfg_ack(applied=("server", "env"))),
          "агент с документом, всё совпадает — «актуален», без «📤» и без строки агента 3.2",
          "adm.gw.card.cfg.actual"),
    _card(1, _cfg_chan(_cfg(env="0" * 16, server="0" * 16)),
          "агент с документом на связи, расходится — изменения уходят каналом", "adm.gw.card.cfg.delivering"),
    _card(1, _cfg_chan(_cfg(env="0" * 16), _cfg_ack(failed={"env": "юнит обвязки <b>не</b> отработал за 600 с"})),
          "шлюз не применил документ — причина экранирована, вернул прежнюю", "adm.gw.card.cfg.refused"),
    _card(1, _cfg_chan(_cfg(online=False, closed=True, age_h=11 * 24, env="0" * 16, server="0" * 16)),
          "агент с документом не на связи, расходится — разделы по именам", "adm.gw.card.cfg.drift.dead"),
    _card(1, _cfg_chan(_cfg_mail, _cfg(mail="ab" * 8),
                       _cfg_ack(applied=("server", "env"), wait=("mail",))),
          "почта ждёт решения в чате бота шлюза", "adm.gw.card.cfg.wait"),
    _card(1, _cfg_chan(_cfg_mail, _cfg(mail="ab" * 8), _cfg_ack(applied=("server", "env"),
                                                                declined=("mail",))),
          "«подробнее»: номер применённого документа и оставленная своя почта", "adm.gw.card.cfg.more"),
    _card(1, _chan(1, _ch(agent_version="3.2.1")), "агент 3.2 со снимком — обнови агента, «📤» на месте",
          "adm.gw.card.cfg.agent32"),
    _card(1, in_zone(_cfg_chan(_cfg_mail, _cfg(mail="ab" * 8), _cfg_ack(applied=("server", "env"),
                                                                         declined=("mail",))), "Europe/Moscow"),
          "сервер в другом поясе (Europe/Moscow): моменты от шлюза и свои — в поясе сервера, «(UTC+3)»",
          "adm.gw.card.cfg.more.zone"),
]


# ── подтверждение: ключи линка и конфиг аплинка (К1–К4), диалог смены ключей,
# финал переезда со шлюзом на прежнем аплинке ─────────────────────────────────
# Агент слота умеет подтверждение (confirm1): сессия открыта с cfg1 и confirm1.
# Состояние перехода ключей — то, что пишет стадия на ВПС (gwlink_rekey_<слот>);
# сами режимы скрипта линка в снимках не выполняются.

_REKEY_FP = "ab12 cd34"


def _x25519_pub(seed: bytes) -> tuple[str, str]:
    """Пара X25519 из постоянного зерна (снимок детерминирован): (приватный, публичный)."""
    import hashlib
    from nacl.public import PrivateKey
    key = PrivateKey(hashlib.sha256(seed).digest())
    return base64.b64encode(bytes(key)).decode(), base64.b64encode(bytes(key.public_key)).decode()


_UP_DEV_PRIV, _UP_DEV_PUB = _x25519_pub(b"DUMMY-uplink-device")
_UP_SRV_PRIV, _UP_SRV_PUB = _x25519_pub(b"DUMMY-uplink-server")


def _confirm1(slot: int = 1):
    """Сессия слота открыта агентом с документом и подтверждением."""
    def step(services, mp):
        services.gwlink_session_opened(slot, "3.3.0", 2, ["cfg1", "confirm1"])
    return step


def _real_uplink_keys(services, mp):
    """Устройства-шлюзы — с настоящими ключами X25519 (из постоянного зерна;
    слот 1 — те же, что у приглашения в снимках агента): сервер прогоняет
    раздел uplink через ту же проверку, что сделает шлюз, а ключи фейка awg —
    не ключи (раздел не поехал бы, карточка показала бы «не едет»). Отпечаток
    пары в строке К3 считается от публичных ключей."""
    import re
    privs = {}
    for gw in services.db.gateways():
        if not gw.device_id:
            continue
        seed = b"DUMMY-uplink-device" if gw.id == 1 else b"DUMMY-uplink-device-%d" % gw.id
        priv, pub = _x25519_pub(seed)
        privs[pub] = priv
        with services.db._tx() as cur:
            cur.execute("UPDATE devices SET public_key = ? WHERE id = ?", (pub, gw.device_id))
    orig = services.gateway_uplink_conf

    def uplink_conf(dev):
        conf = orig(dev)
        conf = re.sub(r"(?m)^PrivateKey = .*$", f"PrivateKey = {privs.get(dev.public_key, '')}", conf)
        return re.sub(r"(?m)^PublicKey = .*$", f"PublicKey = {_UP_SRV_PUB}", conf)
    services.gateway_uplink_conf = uplink_conf


def _rekey(state: str, slot: int = 1, fp: str = _REKEY_FP):
    """Переход ключей линка слота в состоянии state (staged | asked | switched)."""
    def step(services, mp):
        services._rekey_save(slot, {"id": "0a1b2c3d4e5f", "state": state, "fp": fp, "at": _iso(NOW)})
    return step


def _keys(*steps, n: int = 1, **kw):
    """Слоты с документом и подтверждением у слота 1, всё доехало (раздел
    uplink — тоже: сессия с confirm1 открыта раньше, чем считается желаемое);
    поверх — шаги."""
    return _gw(n, _ch_base, _cfg_base, _confirm1(), _real_uplink_keys, _cfg(online=False),
               _cfg_ack(applied=("server", "env", "uplink")), *steps, lan=True, **kw)


def _uplink(*, wait=(), declined=()):
    """Аплинк двойника расходится со стоящим: документ несёт раздел uplink,
    шлюз держит его в wait или declined."""
    return _chan(1, _cfg_base, _confirm1(), _real_uplink_keys, _cfg(online=False, uplink="0" * 16),
                 _cfg_ack(applied=("server", "env"), wait=wait, declined=declined))


def _stage_ok(services, mp):
    """Стадия на ВПС удалась: новые ключи в .next, переход staged."""
    services.gateway_rekey_stage = lambda slot: services._rekey_save(
        slot, {"id": "0a1b2c3d4e5f", "state": "staged", "fp": _REKEY_FP, "at": _iso(NOW)})


class _Listener:
    """Слушатель канала с открытой сессией: link_next уходит."""

    async def deliver_rekey(self, slot_id: int) -> bool:
        return True


def _online_listener(services, mp):
    from awgbot.runtime import linkserver
    mp.setattr(linkserver, "current", lambda: _Listener())


def _old_uplink(token: bool):
    """Переезд идёт; слот NASPi с агентом, умеющим подтверждение, — двойник
    его устройства ещё не поднимался (новый аплинк ждёт «Применить»)."""
    def build(services, mp):
        from awgbot.infra import awglock
        who = _slots(1, token=token)(services, mp)
        mp.setattr(config, "AWG_INTERFACE", "awg0")
        mp.setattr(config, "MIGRATION_INTERFACE", "awg1")
        mp.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.1")
        mp.setattr(config, "SERVER_PORT", 43125)
        mp.setattr(awglock, "applied_generation", lambda: 1)
        mp.setattr(awglock, "target_generation", lambda: 1)
        services.migration_start()
        services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1", "confirm1"])
        return who
    return build


REKEY = [
    _card(1, _keys(_rekey("asked")), "К1: ключи линка ждут подтверждения в чате бота шлюза — с отпечатком",
          "adm.gw.card.rekey.asked"),
    _card(1, _keys(_rekey("staged", fp="")), "К1 без отпечатка: стадия есть, отпечаток не прочитан",
          "adm.gw.card.rekey.staged"),
    _card(1, _keys(_rekey("switched")), "К2: ключи линка — переключение идёт", "adm.gw.card.rekey.switched"),
    _card(1, _uplink(wait=("uplink",)), "К3: конфиг аплинка ждёт подтверждения — с отпечатком пары",
          "adm.gw.card.uplink.wait"),
    _card(1, _uplink(declined=("uplink",)), "К4: шлюз отклонил новый конфиг аплинка", "adm.gw.card.uplink.declined"),
    Shot("adm.gw.edit.confirm1", role="admin", press=[GwSlotCB(action="edit", slot=1)], data=_keys(),
         title="«✏️ Изменить» агента с подтверждением — без «🔑»: смена ключей только из «🔧 Сервис»"),
    Shot("adm.set.svc.keys", role="admin", press=[SetCB(sec="svc")], data=_keys(),
         title="«🔧 Сервис» при шлюзе, умеющем смену каналом — «🔑 Ключи линка» последним рядом"),
    Shot("adm.set.svc.nokeys", role="admin", press=[SetCB(sec="svc")],
         data=_chan(1, _cfg_base, _cfg(), _cfg_ack(applied=("server", "env"))),
         title="«🔧 Сервис», агент без подтверждения — кнопки ключей нет"),
    Shot("adm.lk.pick.none", role="admin", press=[SetCB(sec="lk", act="do", key="pick")],
         data=_chan(1, _cfg_base, _cfg(), _cfg_ack(applied=("server", "env"))),
         title="устаревшая «🔑 Ключи линка», шлюза с подтверждением нет — всплывашка"),
    Shot("adm.lk.pick.one", role="admin", press=[SetCB(sec="svc"), SetCB(sec="lk", act="do", key="pick")], data=_keys(n=2),
         title="один шлюз с подтверждением — сразу диалог; активный при живом резерве — совет"),
    Shot("adm.lk.pick.two", role="admin", press=[SetCB(sec="svc"), SetCB(sec="lk", act="do", key="pick")],
         data=_keys(_confirm1(2), _label(2, "север"), n=2),
         title="два шлюза с подтверждением — экран выбора, имена с подписью"),
    Shot("adm.lk.pick.two.back", role="admin", press=[SetCB(sec="svc"), SetCB(sec="lk", act="do", key="pick"), SetCB(sec="svc")],
         data=_keys(_confirm1(2), n=2), title="«⬅️ Назад» с выбора — в «🔧 Сервис»"),
    Shot("adm.lk.ask", role="admin", press=[SetCB(sec="lk", act="do", key="ask", val="1")], data=_keys(),
         title="смена ключей активного слота без резерва — РФ-доступ прервётся"),
    Shot("adm.lk.ask.standby_down", role="admin", press=[SetCB(sec="lk", act="do", key="ask", val="1")],
         data=_keys(n=2, standby="down"), title="активный слот, резерв лежит — совета нет"),
    Shot("adm.lk.ask.reserve", role="admin", press=[SetCB(sec="lk", act="do", key="pick"), SetCB(sec="lk", act="do", key="ask", val="2")],
         data=_keys(_confirm1(2), n=2), title="резервный слот с экрана выбора — без «РФ-доступ прервётся»"),
    Shot("adm.lk.ask.busy", role="admin", press=[SetCB(sec="lk", act="do", key="ask", val="1")],
         data=_keys(_rekey("asked")), title="смена ключей уже идёт у этого слота — всплывашка, не диалог"),
    Shot("adm.lk.ask.other", role="admin", press=[SetCB(sec="lk", act="do", key="ask", val="2")],
         data=_keys(_confirm1(2), _rekey("switched"), n=2), title="идёт смена ключей у другого слота — всплывашка"),
    Shot("adm.lk.ask.cancel", role="admin", press=[SetCB(sec="lk", act="do", key="ask", val="1"), SetCB(sec="svc")],
         data=_keys(), title="«⬅️ Отмена» диалога — назад в «🔧 Сервис»"),
    Shot("adm.lk.yes", role="admin", press=[SetCB(sec="lk", act="do", key="yes", val="1")],
         data=_keys(_stage_ok, _online_listener), title="«🔑 Сменить», шлюз на связи — «🔧 Сервис» с итогом первой строкой"),
    Shot("adm.lk.yes.offline", role="admin", press=[SetCB(sec="lk", act="do", key="yes", val="1")],
         data=_keys(_stage_ok), title="«🔑 Сменить», канала нет — итог и всплывашка «едут по упр. каналу»"),
    Shot("adm.lk.yes.refused", role="admin", press=[SetCB(sec="lk", act="do", key="yes", val="1")],
         data=_chan(1, _cfg_base, _cfg(caps=(), agent="3.2.1")),
         title="агент ниже 3.3.0 — отказ стадии всплывашкой"),
    Shot("adm.set.mig.finish.old_uplink", role="admin", press=[SetCB(sec="mig", act="do", key="finish")],
         data=_old_uplink(True), title="завершить переезд? шлюз на прежнем аплинке — со ссылкой на бота"),
    Shot("adm.set.mig.finish.old_uplink.nobot", role="admin", press=[SetCB(sec="mig", act="do", key="finish")],
         data=_old_uplink(False), title="шлюз на прежнем аплинке, бот неизвестен — без ссылки"),
]


def _rekey_declined(services, mp):
    """Шлюз отклонил новые ключи (link_ack declined): переход снят, отказ — строкой сутки."""
    services._rekey_save(1, {"state": "declined", "at": _iso(NOW), "error": ""})


def _rekey_auto(services, mp):
    """Смена ключей без вопроса (I1 живому линку): ждать подтверждения некого."""
    services._rekey_save(1, {"id": "0a1b2c3d4e5f", "state": "staged", "fp": _REKEY_FP, "at": _iso(NOW), "auto": "i1"})


def _i1_failed(services, mp):
    """Смена ключей ради I1 не встала трижды — строка карточки."""
    services._i1_plan_save(1, {"want": "<b 0x01>", "tries": 3, "next_at": _iso(NOW)})


REKEY_DECLINED = [
    _card(1, _keys(_rekey_declined), "К4 ключей: шлюз отклонил новые ключи линка — линк на прежних",
          "adm.gw.card.rekey.declined"),
    _card(1, _keys(_rekey_auto), "смена ключей без вопроса (I1): «переключение…», без «ждут подтверждения»",
          "adm.gw.card.rekey.auto"),
    _card(1, _keys(_i1_failed), "новые ключи и обфускация линка не встали трижды", "adm.gw.card.i1.failed"),
    _card(1, _chan(1, _cfg_base, _confirm1(), _cfg(online=False), _cfg_ack(applied=("server", "env"))),
          "раздел uplink не прошёл проверку на сервере — не едет, причина в карточке", "adm.gw.card.uplink.err"),
    Shot("adm.lk.ask.label", role="admin", press=[SetCB(sec="lk", act="do", key="ask", val="1")],
         data=_keys(_label(1, "север")), title="смена ключей слота с подписью — имя с подписью в вопросе"),
]


# ── аварийная смена токена бота шлюза: «🔧 Сервис → 🤖 Токен бота» ──────────
# Агент слота умеет принимать токен каналом (token1); getMe — двойник
# Telegram вместо aiogram.Bot: свой токен слота и новый того же бота живые,
# токен другого бота — живой, но чужой.

TOKEN_NEW = "1234567890:DUMMYNEWDUMMYNEWDUMMYNEW"        # тот же бот, что TOKEN
TOKEN_OTHER = "9876543210:DUMMYOTHERDUMMYOTHERDUMMY"     # новый бот (прежнего удалили)
TOKEN_SERVER = "1111111111:DUMMYSERVERDUMMYSERVERDU"    # бот сервера AWG
TOKEN_TAKEN = "2222222222:DUMMYTAKENDUMMYTAKENDUMMY"    # бот второго шлюза


def _token1(slot: int = 1, confirm: bool = True):
    """Сессия слота открыта агентом с token1 (и confirm1, если confirm)."""
    def step(services, mp):
        caps = ["cfg1", "confirm1", "token1"] if confirm else ["cfg1", "token1"]
        services.gwlink_session_opened(slot, "3.3.0", 2, caps)
    return step


def _tg_getme(services, mp):
    """getMe новым токеном и обновление имени бота — без сети."""
    import types
    import aiogram
    bots = {TOKEN: (1234567890, "naspi_gw_bot"), TOKEN_NEW: (1234567890, "naspi_gw_bot"),
            TOKEN_OTHER: (9876543210, "other_gw_bot"), TOKEN_SERVER: (1111111111, "awg_srv_bot"),
            TOKEN_TAKEN: (2222222222, "pi4_gw_bot")}
    mp.setattr(config, "BOT_TOKEN", TOKEN_SERVER)

    def factory(token, *a, **k):
        async def get_me():
            bot_id, username = bots[token]
            return types.SimpleNamespace(id=bot_id, username=username, first_name="NASPi", is_bot=True)

        async def close():
            pass
        return types.SimpleNamespace(get_me=get_me, session=types.SimpleNamespace(close=close))
    mp.setattr(aiogram, "Bot", factory)


class _TokenListener:
    """Слушатель канала с открытой сессией слота: документ уйдёт сразу."""

    def online(self, slot_id: int) -> bool:
        return True

    def deliver_soon(self) -> None:
        pass


def _token_online(services, mp):
    from awgbot.runtime import linkserver
    mp.setattr(linkserver, "current", lambda: _TokenListener())


def _taken2(services, mp):
    """У второго шлюза — свой бот (TOKEN_TAKEN)."""
    services._shot_tokens[2] = TOKEN_TAKEN


_GTK_PICK = SetCB(sec="gtk", act="do", key="pick")
_GTK_ASK = SetCB(sec="gtk", act="do", key="ask", val="1")


TOKEN_SWAP = [
    Shot("adm.set.svc.token", role="admin", press=[SetCB(sec="svc")], data=_keys(_token1()),
         title="«🔧 Сервис», агент умеет и ключи, и токен — «🔑 Ключи линка» и «🤖 Токен бота» одним рядом"),
    Shot("adm.set.svc.token.only", role="admin", press=[SetCB(sec="svc")],
         data=_chan(1, _cfg_base, _cfg(), _cfg_ack(applied=("server", "env")), _token1(confirm=False)),
         title="«🔧 Сервис», агент с token1 без подтверждения — только «🤖 Токен бота»"),
    Shot("adm.gtk.pick.none", role="admin", press=[_GTK_PICK], data=_keys(),
         title="устаревшая «🤖 Токен бота», агента с token1 нет — всплывашка"),
    Shot("adm.gtk.pick.one", role="admin", press=[SetCB(sec="svc"), _GTK_PICK], data=_keys(_token1()),
         title="один шлюз с token1 — сразу приглашение к вводу токена"),
    Shot("adm.gtk.pick.two", role="admin", press=[SetCB(sec="svc"), _GTK_PICK],
         data=_keys(_token1(), _token1(2), _label(2, "север"), n=2),
         title="два шлюза с token1 — экран выбора, имена с подписью"),
    Shot("adm.gtk.ask", role="admin", press=[_GTK_ASK], data=_keys(_token1()),
         title="приглашение к вводу токена слота"),
    Shot("adm.gtk.cancel", role="admin", press=[_GTK_ASK, CancelCB(kind="set_svc")], data=_keys(_token1()),
         title="«✖️ Отмена» под приглашением — «🔧 Сервис»"),
    Shot("adm.gtk.refused", role="admin", press=[_GTK_ASK], text=TOKEN_SERVER, data=_keys(_token1(), _tg_getme),
         title="токен бота сервера — сообщение удалено, переспрос, ввод открыт"),
    Shot("adm.gtk.refused.taken", role="admin", press=[_GTK_ASK], text=TOKEN_TAKEN,
         data=_keys(_token1(), _tg_getme, _taken2, n=2),
         title="бот уже у другого шлюза — переспрос с его именем"),
    Shot("adm.gtk.done.newbot", role="admin", press=[_GTK_ASK], text=TOKEN_OTHER,
         data=_keys(_token1(), _tg_getme, _token_online),
         title="новый бот, шлюз на связи — итог и строка «открой @бота и нажми «Старт»»"),
    Shot("adm.gtk.done", role="admin", press=[_GTK_ASK], text=TOKEN_NEW,
         data=_keys(_token1(), _tg_getme, _token_online),
         title="тот же бот, шлюз на связи — «🔧 Сервис» с итогом «ушёл шлюзу»"),
    Shot("adm.gtk.done.offline", role="admin", press=[_GTK_ASK], text=TOKEN_NEW,
         data=_keys(_token1(), _tg_getme),
         title="тот же бот, шлюза нет на связи — итог «получит при подключении»"),
]
