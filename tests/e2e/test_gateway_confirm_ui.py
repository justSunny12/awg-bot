"""
Приглашения в чате агента шлюза — новые ключи линка и новый конфиг аплинка
с сервера AWG: экран приглашения (отпечаток, цена), «🔑/🔐 Применить» и
«⬅️ Отмена», ответ серверу, итог правкой сообщения, снятие ручной правкой,
очередь приглашений и вопросов.

Здесь человек решает, переключать ли линк и аплинк — единственные дороги
шлюза к серверу и к Telegram. Цена ошибки: «Применить», не дошедшее до
сервера, — переход висит сутки и снимается; «Отмена», которая не сняла
переход, — шлюз переключится по link_go, хотя человек отказался;
аплинк, применённый без предупреждения «бот пропадёт», — человек видит
мёртвый чат и перезагружает шлюз посреди пробы; «Применить» поверх
аплинка, поправленного руками, — затирает свежую правку; второе
приглашение, не открывшееся после ответа на первое, — висит незамеченным.

Агент — настоящий (`GatewayServices`) на подставном хосте `_Host` из теста
документа: конфиг линка и каталог /etc/awg-gw — во временном каталоге.
Новые ключи линка — настоящие X25519 (nacl), как их сгенерировал бы скрипт
линка ВПС. Аплинк: что стоит и как прошло применение (скрипт обвязки и
проба хендшейка — предмет теста документа) — подменой методов сервиса;
ответ серверу о ключах — через настоящий `linkclient.report_rekey_answer`
в записывающего клиента канала, о разделах документа — запись
`report_cfg_answer`.
"""
from __future__ import annotations

import base64

import pytest
from nacl.public import PrivateKey

import awgbot.core.config as cfg
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.bot.texts.gateway.ask import (LINK_KEYS_ACCEPTED, QUESTION_ANSWERED, QUESTION_STALE, UPLINK_APPLYING,
                                          UPLINK_BUSY)
from awgbot.domain import gwcfg
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra.db import Database
from awgbot.runtime import linkclient
from awgbot.util import wgconf
from tests.conftest import FakeCallback, FakeMessage, FakeState
from tests.unit import test_gw_cfg_apply as _apply
from tests.unit.test_wgconf_allowed import LINK

pytestmark = pytest.mark.e2e

# фикстуры теста документа: своя копия conf и подставной хост
conf_dir = _apply.conf_dir
host = _apply.host


def _pair() -> tuple[str, str]:
    k = PrivateKey.generate()
    return base64.b64encode(bytes(k)).decode(), base64.b64encode(bytes(k.public_key)).decode()


@pytest.fixture()
def svc(tmp_path, conf_dir, host, monkeypatch):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    s = GatewayServices(d)
    monkeypatch.setattr(s, "cached_status", lambda max_age: GwStatus(link_up=True, handshake_age=5.0))
    yield s
    d.close()


class _Client:
    """Клиент упр. канала с открытой сессией: ответы о ключах — в список."""

    def __init__(self):
        self.rekey: list[dict] = []
        self._writer = object()

    async def send_rekey_answer(self, reply):
        self.rekey.append(dict(reply))
        return True


@pytest.fixture()
def channel(monkeypatch):
    c = _Client()
    monkeypatch.setattr(linkclient, "_client", c)
    return c


@pytest.fixture()
def reported(monkeypatch):
    """Ответы человека о разделах документа, ушедшие серверу."""
    got: list[dict] = []

    async def report(services, answer):
        got.append(answer)
    monkeypatch.setattr(linkclient, "report_cfg_answer", report)
    return got


def _nav(fake_bot):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    return msg, FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row] if markup else []


def _edits(msg) -> list[str]:
    return [t for k, t, _ in msg.sent if k == "edit_text"]


def _sent(fake_bot) -> list[str]:
    return [r[2] for r in fake_bot.records if r[0] == "send_message"]


# ── ключи линка ──────────────────────────────────────────────────────────────

def _link_next(rid: str = "0a1b2c3d4e5f") -> tuple[dict, str]:
    """link_next так, как его шлёт сервер: конфиг шлюзовой стороны с новыми
    ключами обеих сторон (адреса, Endpoint, AllowedIPs — стоящие) и отпечаток."""
    gw_priv, gw_pub = _pair()
    _vps_priv, vps_pub = _pair()
    conf = LINK.replace("DUMMY-LINK-PRIVATE-KEY=", gw_priv).replace("DUMMY-VPS-PUBLIC-KEY=", vps_pub)
    fp = gwcfg.key_fp(gw_pub, vps_pub)
    return {"id": rid, "conf": conf, "fp": fp}, fp


def _asked(svc) -> str:
    body, fp = _link_next()
    res = svc.rekey_next_in(body)
    assert res["ask"] is True and res["reply"]["state"] == "asked", res
    return fp


async def test_link_invitation_shows_the_fingerprint_and_the_price(svc, host, fake_bot):
    """«⏳ Решить» открывает приглашение: отпечаток пары тот же, что сервер
    покажет в карточке слота (сверка глазами), «⬅️ Отмена» и «🔑 Применить»
    одним рядом, отмена первой. Шлюз трафик не несёт — без «РФ-доступ
    прервётся»; несёт (активный, линк жив) — с ним: человек должен знать цену
    до нажатия."""
    fp = _asked(svc)
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_ask(cb, svc, FakeState())
    kind, text, markup = msg.sent[-1]
    assert kind == "edit_text" and "Новые ключи линка с сервера AWG" in text, text
    assert f"Отпечаток <code>{fp}</code>" in text, text
    assert "РФ-доступ у всех прервётся" not in text, "шлюз трафик не несёт, а цена — с обрывом"
    assert [[b.text for b in r] for r in markup.inline_keyboard] == [["⬅️ Отмена", "🔑 Применить"], ["⬅️ В меню"]]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        GwCB(action="cfg_no", val="link").pack(), GwCB(action="cfg_ok!", val="link").pack()]
    svc.set_link_role(True, standby=False, name="NASPi")
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_ask(cb, svc, FakeState())
    assert "Линк переключится — РФ-доступ у всех прервётся на секунды" in msg.sent[-1][1], msg.sent[-1][1]


async def test_apply_link_keys_says_ready_to_the_server_and_waits_for_it(svc, host, fake_bot, channel):
    """«🔑 Применить»: шлюз в ready (ждёт link_go), серверу ушёл link_ack ready
    с id перехода; сообщение — «🔑 Новые ключи приняты…»: переключит сервер,
    итог придёт отдельно. Конфиг линка до link_go не тронут."""
    _asked(svc)
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="link"), svc, FakeState())
    assert channel.rekey == [{"id": "0a1b2c3d4e5f", "state": "ready", "error": ""}], channel.rekey
    assert svc.rekey_state()["state"] == "ready"
    assert _edits(msg)[0] == LINK_KEYS_ACCEPTED, _edits(msg)
    assert host.link.read_text(encoding="utf-8") == LINK, "линк переключён до команды сервера"
    assert host.link_runs == [], host.link_runs
    assert svc.cfg_questions() == [], "приглашение осталось в очереди после ответа"


async def test_cancel_link_keys_drops_the_transition_and_tells_the_server(svc, host, fake_bot, channel):
    """«⬅️ Отмена»: переход снят — новый конфиг убран (ключа на диске не
    остаётся), в link.txn остаётся только метка отказа с id; серверу —
    declined; итог «Ключи линка остались прежними», дальше панель. Метка
    нужна, когда отказ не дошёл (канал лежал): сервер повторит link_next с
    тем же id, и шлюз ответит «отказ», не спрашивая человека второй раз."""
    body, _ = _link_next()
    assert svc.rekey_next_in(body)["ask"] is True
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_no", val="link"), svc, FakeState())
    assert channel.rekey == [{"id": "0a1b2c3d4e5f", "state": "declined", "error": ""}], channel.rekey
    st = svc.rekey_state()
    assert st.get("id") == "0a1b2c3d4e5f" and st.get("state") == "declined", st
    assert not (host.etc / "rekey.next").exists() and not (host.etc / "rekey.prev").exists(), \
        "после отмены новый конфиг линка остался на диске"
    priv = wgconf.parse(body["conf"]).get("Interface", "PrivateKey")
    assert priv not in (host.etc / "link.txn").read_text(encoding="utf-8"), "в метке отказа — приватный ключ"
    assert _edits(msg)[0] == "ℹ️ Ключи линка остались прежними", _edits(msg)
    panels = [t for k, t, _ in msg.sent if k == "answer"] + _sent(fake_bot)
    assert panels and panels[-1].startswith("🛰 "), panels
    # тот же link_next после переподключения: отказ без второго приглашения
    again = svc.rekey_next_in(body)
    assert again["ask"] is False and again["reply"] == {"id": "0a1b2c3d4e5f", "state": "declined", "error": ""}, again
    assert svc.cfg_questions() == [], "отказанное приглашение снова в очереди"
    assert await gh.show_cfg_question(fake_bot, svc, cfg.ADMIN_ID) is False


async def test_a_decided_link_invitation_answers_with_a_toast(svc, host, fake_bot, channel):
    """Нажатие со старого приглашения (уже решено, отозвано сервером, двойное
    нажатие): всплывашка «Запрос уже решён», серверу ничего, панель — а не
    итог, будто что-то произошло."""
    _asked(svc)
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="link"), svc, FakeState())
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_no", val="link"), svc, FakeState())
    assert cb.answers and cb.answers[-1][0] == "Вопрос уже решён", cb.answers
    assert len(channel.rekey) == 1, f"второй ответ ушёл серверу: {channel.rekey}"
    assert svc.rekey_state()["state"] == "ready", "второе нажатие «Отмена» сняло уже согласованный переход"
    assert LINK_KEYS_ACCEPTED not in _edits(msg) and "ℹ️ Ключи линка остались прежними" not in _edits(msg)


async def test_the_same_link_next_after_reconnect_is_not_asked_twice(svc, host, fake_bot):
    """Сессия упала между asked и ready, сервер повторил link_next с тем же id:
    второго приглашения нет, серверу — своё состояние."""
    body, _ = _link_next()
    assert svc.rekey_next_in(body)["ask"] is True
    again = svc.rekey_next_in(body)
    assert again["ask"] is False and again["reply"]["state"] == "asked", again
    assert svc.cfg_questions() == ["link"]


# ── конфиг аплинка ───────────────────────────────────────────────────────────

def _uplink_value() -> dict:
    dev_priv, dev_pub = _pair()
    _srv_priv, srv_pub = _pair()
    conf = (f"[Interface]\nPrivateKey = {dev_priv}\nAddress = 10.8.1.7/32\nTable = off\n\n"
            f"[Peer]\nPublicKey = {srv_pub}\nPresharedKey = DUMMY-PSK=\nEndpoint = vpn.example.org:43125\n"
            "AllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n")
    return {"conf": conf, "pubkey": dev_pub, "prev_pubkey": "", "_fp": gwcfg.key_fp(dev_pub, srv_pub),
            "_priv": dev_priv}


class _Uplink:
    """Стоящий аплинк и его применение: mine — отпечаток стоящего (своё);
    apply — итог транзакции; calls — что применяли и что в этот момент
    человек уже видел в чате."""

    def __init__(self, svc, monkeypatch, msg_log: list):
        self.mine = "f00d" * 4
        self.result = {"ok": True, "error": "", "state": "applied"}
        self.calls: list[tuple[str, str]] = []
        self.msg_log = msg_log
        monkeypatch.setattr(svc, "cfg_uplink_print", lambda cached=False: self.mine)
        monkeypatch.setattr(svc, "cfg_uplink_apply", self._apply)

    def _apply(self, value):
        seen = self.msg_log[-1][1] if self.msg_log else ""
        self.calls.append((value["conf"], seen))
        return dict(self.result)


def _hold_uplink(svc, value) -> str:
    server = gwcfg.uplink_print(value["conf"], value["pubkey"], value["prev_pubkey"])
    clean = {k: v for k, v in value.items() if not k.startswith("_")}
    svc.cfg_hold("uplink", clean, server, 18, "9c41" * 4)
    return server


async def test_uplink_invitation_names_the_device_key_and_the_fingerprint(svc, host, fake_bot, monkeypatch):
    """Приглашение аплинка: ключ устройства (начало публичного) и отпечаток
    пары — тот же, что в строке К3 карточки на сервере; кнопки «⬅️ Отмена» и
    «🔐 Применить» одним рядом; приватного ключа в чате нет."""
    value = _uplink_value()
    msg, cb = _nav(fake_bot)
    _Uplink(svc, monkeypatch, msg.sent)
    _hold_uplink(svc, value)
    await gh.gw_cfg_ask(cb, svc, FakeState())
    _, text, markup = msg.sent[-1]
    assert "Новый конфиг аплинка с сервера AWG" in text, text
    assert f"Ключ устройства <code>{value['pubkey'][:4]}…</code>" in text, text
    assert f"отпечаток <code>{value['_fp']}</code>" in text, text
    assert value["_priv"] not in text and value["_priv"][:8] not in text, "приватный ключ аплинка в чате"
    assert [[b.text for b in r] for r in markup.inline_keyboard] == [["⬅️ Отмена", "🔐 Применить"], ["⬅️ В меню"]]


async def test_apply_uplink_warns_first_then_shows_the_result(svc, host, fake_bot, reported, monkeypatch):
    """«🔐 Применить»: сначала сообщение правится на «⏳ Применяю конфиг
    аплинка…» (бот пропадёт из Telegram на время пробы), и только потом
    применение; итог — «✅ Новый конфиг аплинка применён» правкой того же
    сообщения; серверу — принято; вопрос и памятка стёрты."""
    value = _uplink_value()
    msg, cb = _nav(fake_bot)
    up = _Uplink(svc, monkeypatch, msg.sent)
    server = _hold_uplink(svc, value)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="uplink"), svc, FakeState())
    assert len(up.calls) == 1 and up.calls[0][0] == value["conf"], up.calls
    assert up.calls[0][1] == UPLINK_APPLYING, f"применение началось до предупреждения: человек видел {up.calls[0][1]!r}"
    assert _edits(msg)[:2] == [UPLINK_APPLYING, "✅ Новый конфиг аплинка применён"], _edits(msg)
    assert len(reported) == 1 and reported[0]["section"] == "uplink" and reported[0]["accepted"] is True
    assert reported[0]["print"] == server and not reported[0].get("failed"), reported[0]
    assert svc.cfg_questions() == [] and "uplink" not in svc.cfg_keep()


@pytest.mark.parametrize("how,error,want", [
    ("reverted", "аплинк не поднялся за 60 с", "⚠️ Новый конфиг аплинка не поднялся — вернул прежний"),
    ("revert_failed", "прежний конфиг не встал",
     "🔴 Новый конфиг аплинка не поднялся, прежний вернуть не удалось: прежний конфиг не встал. "
     "Загляни в «🩺 Здоровье»; связь с сервером AWG может пропасть"),
    ("refused", "Table = off не задан", "⚠️ Новый конфиг аплинка не применён: Table = off не задан"),
])
async def test_apply_uplink_that_failed_says_what_stands_now(svc, host, fake_bot, reported, monkeypatch,
                                                             how, error, want):
    """Применение не вышло — итог говорит, что стоит сейчас: «вернул прежний»
    (откат удался — бот на старом аплинке, делать ничего не надо), «и прежний
    не вернулся: …» (шлюз, возможно, без аплинка — нужен человек у консоли),
    «не применён: …» (скрипт отказал, ничего не тронуто). Перепутать первые
    два — человек не пойдёт чинить шлюз, который остался без Telegram.
    Серверу — отказ с причиной (карточка покажет её), вопрос снят (повторит
    сервер новым документом)."""
    value = _uplink_value()
    msg, cb = _nav(fake_bot)
    up = _Uplink(svc, monkeypatch, msg.sent)
    up.result = {"ok": False, "error": error, "state": how}
    _hold_uplink(svc, value)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="uplink"), svc, FakeState())
    assert _edits(msg)[:2] == [UPLINK_APPLYING, want], _edits(msg)
    assert len(reported) == 1 and reported[0]["failed"] == error, reported
    assert svc.cfg_questions() == []


async def test_apply_uplink_while_the_unit_runs_keeps_the_question(svc, host, fake_bot, reported, monkeypatch):
    """Обвязка занята прогоном юнита (busy): ничего не тронуто — сообщение
    снова вопрос с «⬅️ Отмена» / «🔐 Применить» и строкой «нажми через
    минуту», серверу ничего (ни отказа, ни согласия), вопрос в очереди.
    Ответь агент серверу отказом — сервер считал бы аплинк отклонённым, а
    человек так и не смог бы нажать ещё раз."""
    value = _uplink_value()
    msg, cb = _nav(fake_bot)
    up = _Uplink(svc, monkeypatch, msg.sent)
    up.result = {"ok": False, "error": "обвязка занята", "state": "busy"}
    _hold_uplink(svc, value)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="uplink"), svc, FakeState())
    kind, text, markup = msg.sent[-1]
    assert kind == "edit_text" and "Новый конфиг аплинка с сервера AWG" in text, text
    assert text.endswith("\n" + UPLINK_BUSY), text
    assert [[b.text for b in r] for r in markup.inline_keyboard] == [["⬅️ Отмена", "🔐 Применить"], ["⬅️ В меню"]], \
        "после «занято» нечем нажать ещё раз"
    assert reported == [], f"серверу ушёл ответ на неприменённый аплинк: {reported}"
    assert svc.cfg_questions() == ["uplink"] and svc.cfg_keep() == {}
    # через минуту — то же нажатие применяет
    up.result = {"ok": True, "error": "", "state": "applied"}
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="uplink"), svc, FakeState())
    assert "✅ Новый конфиг аплинка применён" in _edits(msg), _edits(msg)
    assert len(up.calls) == 2 and len(reported) == 1


async def test_apply_uplink_twice_answers_that_the_question_is_decided(svc, host, fake_bot, reported, monkeypatch):
    """Второе нажатие «🔐 Применить» (двойное, или со старого сообщения после
    итога): всплывашка «Вопрос уже решён» и панель, второго применения нет —
    иначе бот шлюза снова пропал бы из Telegram на минуту пробы, а
    «⏳ Применяю…» обещало бы то, чего не будет."""
    value = _uplink_value()
    msg, cb = _nav(fake_bot)
    up = _Uplink(svc, monkeypatch, msg.sent)
    _hold_uplink(svc, value)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="uplink"), svc, FakeState())
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="uplink"), svc, FakeState())
    assert len(up.calls) == 1, f"аплинк применён второй раз: {len(up.calls)}"
    assert cb.answers and cb.answers[-1][0] == QUESTION_ANSWERED, cb.answers
    assert _edits(msg)[:1] and _edits(msg)[0].startswith("🛰 "), _edits(msg)   # как у ключей линка: всплывашка и панель
    assert UPLINK_APPLYING not in _edits(msg), "второе нажатие пообещало «пропаду из Telegram»"
    assert len(reported) == 1


async def test_the_result_comes_as_a_new_message_when_the_edit_dies(svc, host, fake_bot, reported, monkeypatch):
    """После смены аплинка прежние соединения с Telegram мертвы: правка
    сообщения итогом падает (таймаут) — итог приходит новым сообщением, а не
    теряется; обработчик не падает, серверу ответ ушёл."""
    value = _uplink_value()
    msg, cb = _nav(fake_bot)
    _Uplink(svc, monkeypatch, msg.sent)
    _hold_uplink(svc, value)
    plain = msg.edit_text

    async def edit_text(text, reply_markup=None, **kw):
        if text != UPLINK_APPLYING:
            raise TimeoutError("соединение с Telegram ушло вместе с прежним аплинком")
        return await plain(text, reply_markup=reply_markup, **kw)
    msg.edit_text = edit_text
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="uplink"), svc, FakeState())
    assert _edits(msg) == [UPLINK_APPLYING], _edits(msg)
    assert "✅ Новый конфиг аплинка применён" in _sent(fake_bot), f"итог потерян: {_sent(fake_bot)}"
    assert len(reported) == 1 and reported[0]["accepted"] is True


async def test_a_dead_warning_edit_does_not_send_a_second_message(svc, host, fake_bot, reported, monkeypatch):
    """Правка «⏳ Применяю…» не вышла — нового сообщения с предупреждением
    нет (оно прожило бы дольше итога и висело бы последним), применение и
    итог идут дальше."""
    value = _uplink_value()
    msg, cb = _nav(fake_bot)
    up = _Uplink(svc, monkeypatch, msg.sent)
    _hold_uplink(svc, value)
    plain = msg.edit_text

    async def edit_text(text, reply_markup=None, **kw):
        if text == UPLINK_APPLYING:
            raise TimeoutError("Telegram не ответил")
        return await plain(text, reply_markup=reply_markup, **kw)
    msg.edit_text = edit_text
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="uplink"), svc, FakeState())
    assert UPLINK_APPLYING not in _sent(fake_bot), "предупреждение пришло отдельным сообщением"
    assert len(up.calls) == 1 and _edits(msg)[0] == "✅ Новый конфиг аплинка применён", _edits(msg)


async def test_a_failing_next_screen_does_not_break_the_answer(svc, host, fake_bot, reported, monkeypatch):
    """Следующий вопрос или панель не показались (Telegram ещё не отвечает
    после смены аплинка): обработчик не падает, итог уже на экране, ответ
    серверу ушёл — иначе aiogram записал бы ошибку, а кнопка «крутилась»."""
    from awgbot.bot.handlers.gateway import ask
    value = _uplink_value()
    msg, cb = _nav(fake_bot)
    _Uplink(svc, monkeypatch, msg.sent)
    _hold_uplink(svc, value)

    async def broken(*a, **kw):
        raise TimeoutError("Telegram не ответил")
    monkeypatch.setattr(ask, "show_cfg_question", broken)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="uplink"), svc, FakeState())
    assert _edits(msg)[-1] == "✅ Новый конфиг аплинка применён", _edits(msg)
    assert len(reported) == 1


async def test_cancel_uplink_keeps_the_old_one_and_remembers_the_refusal(svc, host, fake_bot, reported, monkeypatch):
    """«⬅️ Отмена»: ничего не применяется, памятка с серверным отпечатком
    (тот же конфиг сервер больше не предложит), серверу — отказ; итог «Аплинк
    остался прежним»."""
    value = _uplink_value()
    msg, cb = _nav(fake_bot)
    up = _Uplink(svc, monkeypatch, msg.sent)
    server = _hold_uplink(svc, value)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_no", val="uplink"), svc, FakeState())
    assert up.calls == [], "«Отмена» применила аплинк"
    assert svc.cfg_keep() == {"uplink": server}, svc.cfg_keep()
    assert _edits(msg)[0] == "ℹ️ Аплинк остался прежним", _edits(msg)
    assert UPLINK_APPLYING not in _edits(msg), "при отмене обещано «пропаду из Telegram»"
    assert len(reported) == 1 and reported[0]["accepted"] is False


async def test_uplink_changed_by_hand_drops_the_invitation(svc, host, fake_bot, reported, monkeypatch):
    """Аплинк поправили руками, пока висело приглашение: «Применить» не затирает
    правку — сообщение «Вопрос снят: аплинк поменяли здесь вручную», ни
    применения, ни памятки, ни ответа серверу (новый снимок покажет новое
    стоящее, и сервер решит заново)."""
    value = _uplink_value()
    msg, cb = _nav(fake_bot)
    up = _Uplink(svc, monkeypatch, msg.sent)
    _hold_uplink(svc, value)
    up.mine = "beef" * 4
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="uplink"), svc, FakeState())
    assert _edits(msg)[0] == QUESTION_STALE["uplink"], _edits(msg)
    assert up.calls == [] and reported == [] and svc.cfg_keep() == {} and svc.cfg_questions() == []


# ── очередь ──────────────────────────────────────────────────────────────────

async def test_the_queue_goes_link_then_uplink_then_mail(svc, host, fake_bot, channel, reported, monkeypatch):
    """Ключи линка, аплинк и почта висят разом: на панели — «⏳ Ждут решения:
    ключи линка, конфиг аплинка, почта»; «⏳ Решить» открывает ключи линка;
    ответ на них открывает приглашение аплинка новым живым меню, ответ на
    него — вопрос о почте; очередь пуста — панель без «⏳»."""
    from awgbot.bot.texts import cfg_pending_line
    msg, cb = _nav(fake_bot)
    _Uplink(svc, monkeypatch, msg.sent)
    _asked(svc)
    svc.email_save("own@example.net", "DUMMY-own", "imap.example.net", 993, "smtp.example.net", 587)
    svc.apply_cfg(_apply._doc(mail=_apply.MAIL))
    # аплинк — после документа: документ без раздела uplink снимает его вопрос
    _hold_uplink(svc, _uplink_value())
    assert svc.cfg_questions() == ["link", "uplink", "mail"], svc.cfg_questions()
    assert cfg_pending_line(svc.cfg_questions()) == "⏳ Ждут решения: ключи линка, конфиг аплинка, почта"
    await gh.gw_panel(cb, svc, FakeState())
    assert "⏳ Ждут решения: ключи линка, конфиг аплинка, почта" in msg.sent[-1][1], msg.sent[-1][1]
    await gh.gw_cfg_ask(cb, svc, FakeState())
    assert "Новые ключи линка" in msg.sent[-1][1]
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_no", val="link"), svc, FakeState())
    assert "Новый конфиг аплинка с сервера AWG" in _sent(fake_bot)[-1], _sent(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_no", val="uplink"), svc, FakeState())
    assert "Почта на шлюзе отличается" in _sent(fake_bot)[-1], _sent(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_no", val="mail"), svc, FakeState())
    kind, last, markup = msg.sent[-1]
    assert kind == "answer" and last.startswith("🛰 ") and "Ждут решения" not in last, last
    assert "⏳ Решить" not in _labels(markup), _labels(markup)
    assert svc.cfg_questions() == []


async def test_the_channel_hook_opens_the_link_invitation_as_a_new_menu(svc, host, fake_bot):
    """link_next принят без человека у экрана — крючок канала показывает
    приглашение новым живым меню в чате админа."""
    _asked(svc)
    assert await gh.show_cfg_question(fake_bot, svc, cfg.ADMIN_ID) is True
    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) == 1 and sent[0][1] == cfg.ADMIN_ID
    assert "Новые ключи линка с сервера AWG" in sent[0][2], sent[0][2]


# ── итог перехода — правкой самого приглашения ───────────────────────────────

class _Chat:
    """Чат агента так, как его видят крючки канала: правка по id сообщения
    (как bot.edit_message_text в main — без клавиатуры; удалённое или
    слишком старое сообщение — False) и отдельное уведомление."""

    def __init__(self, fake_bot, monkeypatch):
        self.ids: list[int] = []                    # id сообщений, ушедших новым меню
        self.edited: list[tuple[int, str]] = []
        self.notified: list[str] = []
        self.gone: set[int] = set()
        send = fake_bot.send_message

        async def send_message(chat_id, text, reply_markup=None, **kw):
            sent = await send(chat_id, text, reply_markup=reply_markup, **kw)
            self.ids.append(sent.message_id)
            return sent
        monkeypatch.setattr(fake_bot, "send_message", send_message)

        async def edit(chat_id, message_id, text):
            if message_id in self.gone:
                return False
            self.edited.append((message_id, text))
            return True

        async def notify(text):
            self.notified.append(text)
        monkeypatch.setattr(linkclient, "_edit", edit)
        monkeypatch.setattr(linkclient, "_notify", notify)
        monkeypatch.setattr(linkclient, "_ask", None)


async def test_a_withdrawn_invitation_is_edited_in_place(svc, host, fake_bot, monkeypatch):
    """Приглашение показано крючком канала — запомнено его сообщение; сервер
    отозвал запрос (link_cancel) — правится само приглашение («Запрос отозван
    сервером AWG»), а не приходит отдельное сообщение: иначе под ним
    оставались бы живые «🔑 Применить» и «⬅️ Отмена» к снятому переходу."""
    from awgbot.runtime.linkclient.rekey import WITHDRAWN
    chat = _Chat(fake_bot, monkeypatch)
    _asked(svc)
    assert await gh.show_cfg_question(fake_bot, svc, cfg.ADMIN_ID) is True
    invite = chat.ids[-1]
    assert svc.rekey_state().get("msg") == [cfg.ADMIN_ID, invite], svc.rekey_state()
    await linkclient.LinkClient(svc)._rekey_cancel({"id": "0a1b2c3d4e5f"})
    assert chat.edited == [(invite, WITHDRAWN)], chat.edited
    assert chat.notified == [], "отзыв пришёл отдельным сообщением при живом приглашении"
    assert svc.rekey_state() == {} and svc.cfg_questions() == []


async def test_decide_button_remembers_the_panel_as_the_invitation(svc, host, fake_bot, monkeypatch):
    """«⏳ Решить» рисует приглашение правкой панели — запомнена панель: отзыв
    правит её; приглашение удалено из чата — отзыв приходит уведомлением, а
    не теряется."""
    from awgbot.runtime.linkclient.rekey import WITHDRAWN
    chat = _Chat(fake_bot, monkeypatch)
    _asked(svc)
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_ask(cb, svc, FakeState())
    assert svc.rekey_state().get("msg") == [cfg.ADMIN_ID, msg.message_id], svc.rekey_state()
    chat.gone.add(msg.message_id)
    await linkclient.LinkClient(svc)._rekey_cancel({"id": "0a1b2c3d4e5f"})
    assert chat.edited == [] and chat.notified == [WITHDRAWN], (chat.edited, chat.notified)


async def test_a_newer_invitation_marks_the_old_one_replaced(svc, host, fake_bot, monkeypatch):
    """Сервер прислал новый переход, пока висело прежнее приглашение: прежнее
    правится на «Запрос заменён более новым» — две пары кнопок к разным
    ключам в одном чате путали бы, что именно подтверждаешь."""
    from awgbot.runtime.linkclient.rekey import REPLACED
    chat = _Chat(fake_bot, monkeypatch)
    _asked(svc)
    await gh.show_cfg_question(fake_bot, svc, cfg.ADMIN_ID)
    old = chat.ids[-1]
    client = linkclient.LinkClient(svc)
    body, fp = _link_next("1b2c3d4e5f6a")
    await client._rekey_next(body)
    assert chat.edited == [(old, REPLACED)], chat.edited
    assert svc.rekey_state().get("id") == "1b2c3d4e5f6a" and svc.rekey_state().get("fp") == fp


async def test_a_declined_invitation_is_not_edited_by_a_late_withdrawal(svc, host, fake_bot, monkeypatch, channel):
    """Человек нажал «⬅️ Отмена» (итог уже на экране), потом сервер отозвал
    тот же запрос: итог не затирается «отозван», уведомления нет — метка
    отказа просто снимается."""
    chat = _Chat(fake_bot, monkeypatch)
    _asked(svc)
    await gh.show_cfg_question(fake_bot, svc, cfg.ADMIN_ID)
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_no", val="link"), svc, FakeState())
    await linkclient.LinkClient(svc)._rekey_cancel({"id": "0a1b2c3d4e5f"})
    assert chat.edited == [] and chat.notified == [], (chat.edited, chat.notified)
    assert svc.rekey_state() == {}
