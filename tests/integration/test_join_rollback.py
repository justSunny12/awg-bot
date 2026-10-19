"""Откаты подключения шлюза командой (FIXES-uupd-3, axis-pairing quar-3/4/7,
ui-7/8): код верный, а смена ключей не встала за час — откат как при отмене,
строка нового слота и устройство, заведённые кодом, прочь; возврат не удался —
запись остаётся «closing», такт повторяет, человеку сказано один раз; linked
без итога агента — снимается по сроку; номер слота после кода не занимается
дважды; ручная смена ключей слота в сопряжении — отказ; итог смены ключей
сопряжения — без «ключи сменены».

Цена ошибки: слот навсегда в карантине со штампом и .join-prev, пир-сирота
«Шлюз» в списке устройств, молчаливое «вернулся», когда линк не вернулся.
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.runtime import linkserver
from awgbot.util import timeutil

from tests.integration import _join_common as jc
from tests.integration._join_common import Gateway, connect, slot_with_device

pytestmark = pytest.mark.integration
admin, vps = jc.admin, jc.vps                     # фикстуры сцены


def _ago(seconds: int) -> str:
    from datetime import timedelta
    return timeutil.to_iso(timeutil.now() - timedelta(seconds=seconds))


def _coded(services, slot: int = 1, **issue) -> Gateway:
    """Команда выдана, шлюз подключился, код верный (смена ключей — в стадии)."""
    res = services.gwjoin_issue(**issue)
    side = Gateway(res["command"])
    connect(services, side, slot)
    from awgbot.util import joinsas
    assert services.gwjoin_code(slot, services.gwjoin(slot)["id"], joinsas.show(side.code))["result"] == "ok"
    return side


@pytest.fixture()
def server(services, monkeypatch):
    """Слушатель канала без сокетов: события хода — в список, ensure — пусто."""
    events: list[tuple[str, dict, dict]] = []

    async def on_join(event, st, extra):
        events.append((event, st, extra))
    monkeypatch.setattr(linkserver, "_on_join", on_join)
    srv = linkserver.LinkServer(services)

    async def _ensure():
        return None
    monkeypatch.setattr(srv, "ensure", _ensure)
    srv.events = events
    return srv


def _names(services) -> set[str]:
    return {d.name for d in services.db.list_devices(services.admin_client().id)}


async def test_accepted_too_long_rolls_a_new_slot_back(services, vps, server):
    """quar-3: новый слот — строка и устройство «Шлюз», заведённые кодом, прочь,
    линк первого слота — на случайные ключи без штампа, событие stuck."""
    _coded(services)
    assert services.db.gateway(1) is not None and "Шлюз" in _names(services)
    st = services.gwjoin(1)
    services._gwjoin_save(1, {**st, "accepted_at": _ago(2 * 3600)})
    await server.join_tick()
    assert [e for e, _s, _x in server.events] == ["stuck"]
    assert services.gwjoin(1) == {} and services.db.gateway(1) is None
    assert "Шлюз" not in _names(services) and "NASPi" in _names(services)
    assert vps.modes[-1] == "--rekey" and not vps.stamped("awglink")


async def test_accepted_too_long_gives_a_replaced_slot_its_old_device_back(services, vps, server):
    """quar-3 у замены: прежние ключи и прежнее устройство на место,
    заведённое кодом «Шлюз» — прочь."""
    slot_with_device(services, vps, 1)
    old_peer = vps.peer_pub("awglink")
    _coded(services, slot_id=1)
    assert services.db.get_device(services.db.gateway(1).device_id).name == "Шлюз"
    st = services.gwjoin(1)
    services._gwjoin_save(1, {**st, "accepted_at": _ago(2 * 3600)})
    await server.join_tick()
    assert [e for e, _s, _x in server.events] == ["stuck"]
    assert services.db.get_device(services.db.gateway(1).device_id).name == "NASPi"
    assert "Шлюз" not in _names(services)
    assert "--join-revert" in vps.modes and vps.peer_pub("awglink") == old_peer
    assert services.gateway_rekey(1) == {} or not services.gateway_rekey_active(1)


async def test_a_failed_revert_keeps_closing_says_it_once_and_the_tick_retries(services, vps, server):
    """quar-4: возврат упал — запись «closing», ответ — причина; такт повторяет
    каждую минуту, а сказано — один раз; удалось — событие по причине (cancel)."""
    slot_with_device(services, vps, 1)
    services.gwjoin_issue(slot_id=1)
    jid = services.gwjoin(1)["id"]
    vps.fail["--join-revert"] = "awg-quick: занято"
    assert services.gwjoin_cancel(1, jid)
    err = await server.join_close(1, "cancel")
    assert "занято" in err
    st = services.gwjoin(1)
    assert st["state"] == "closing" and st["why"] == "cancel" and vps.stamped("awglink")
    await server.join_tick()
    await server.join_tick()
    told = [(e, x) for e, _s, x in server.events]
    assert told == [("revert_failed", {"err": told[0][1]["err"]})] and "занято" in told[0][1]["err"]
    del vps.fail["--join-revert"]
    await server.join_tick()
    assert [e for e, _s, _x in server.events] == ["revert_failed", "cancel"]
    assert services.gwjoin(1) == {} and not vps.stamped("awglink")


def test_the_cancel_button_marks_it_told(services, vps):
    """Отмена кнопкой уже сказала «не вернулся» — такт второй раз не скажет."""
    slot_with_device(services, vps, 1)
    services.gwjoin_issue(slot_id=1)
    jid = services.gwjoin(1)["id"]
    assert services.gwjoin_revert_told(1, jid) is True
    assert services.gwjoin_revert_told(1, jid) is False
    assert services.gwjoin_revert_told(1, "0" * 16) is False


async def test_linked_without_the_agents_word_goes_stale(services, vps, server):
    """quar-7: на постоянных ключах, а «установлен» не пришёл за сутки — запись
    снимается, событие stale; линк не трогается."""
    side = _coded(services)
    accept_rest(services, vps, side)
    st = services.gwjoin(1)
    assert st["state"] == "linked"
    services._gwjoin_save(1, {**st, "linked_at": _ago(25 * 3600)})
    runs = len(vps.runs)
    await server.join_tick()
    assert [e for e, _s, _x in server.events] == ["stale"]
    assert services.gwjoin(1) == {} and services.db.gateway(1) is not None and len(vps.runs) == runs


def accept_rest(services, vps, side):
    """Смена ключей после кода доведена (как в jc.accept, код уже введён)."""
    rk = services.gateway_rekey(1)
    action, _ = services.gateway_rekey_ack_in(1, {"id": rk["id"], "state": "ready"})
    assert action == "go"
    services.gateway_rekey_go(1)
    assert services.gateway_rekey_commit(1)[0] is True
    return services.gateway_rekey_hello(1)


def test_the_join_rekey_says_no_keys_changed(services, vps):
    """ui-7: итог смены ключей сопряжения — сообщением подключения, без
    «🔑 ключи линка сменены»."""
    res = services.gwjoin_issue()
    side = Gateway(res["command"])
    connect(services, side, 1)
    st = services.gwjoin(1)
    from awgbot.util import joinsas
    assert services.gwjoin_code(1, st["id"], joinsas.show(side.code))["result"] == "ok"
    out = accept_rest(services, vps, side)
    assert out["rekeyed"] and out["notes"] == []


def test_a_manual_rekey_of_a_pairing_slot_is_refused(services, vps):
    """ui-8: «🔑 Сменить» у слота в замене — отказ, ключи ведёт сопряжение."""
    from awgbot.domain.services.types import ServiceError
    slot_with_device(services, vps, 1)
    services.gwjoin_issue(slot_id=1)
    with pytest.raises(ServiceError, match="Идёт подключение шлюза командой"):
        services.gateway_rekey_stage(1)


def test_an_accepted_new_slot_is_not_counted_twice(services, vps, monkeypatch):
    """После кода у нового слота есть строка — запись сопряжения номер второй
    раз не занимает: при пределе в два следующий слот — второй."""
    monkeypatch.setattr(config, "ROUTING_GATEWAYS_MAX", 2)
    _coded(services)
    res = services.gwjoin_issue()
    assert res["state"]["slot"] == 2


def test_the_device_made_by_the_code_is_a_plain_client_peer(services, vps):
    """Вне оси (accept.py add_device): «Шлюз» — обычное устройство админа, как
    у прежнего «➕ Новое устройство», без РФ-доступа."""
    _coded(services)
    dev = services.db.get_device(services.db.gateway(1).device_id)
    assert dev.name == "Шлюз" and not dev.routing_on
    assert services.gwjoin(1)["made"] == dev.id



async def test_cancel_drops_the_open_code_input_of_this_join(services, vps):
    """ui-5: ввод кода к этой заявке был открыт — отмена сбрасывает его (иначе
    следующий текст ушёл бы кодом в закрытую заявку); к другой — не трогает."""
    import types
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from awgbot.bot.callbacks import GwJoinCB
    from awgbot.bot.handlers.settings import gwjoin as h
    from awgbot.bot.states import GatewayJoinCode
    services.gwjoin_issue()
    jid = services.gwjoin(1)["id"]
    services.gwjoin_set_message(1, 1, 7)

    class _Bot:
        async def edit_message_text(self, *a, **kw):
            return None

        async def delete_message(self, *a, **kw):
            return True

        async def delete_messages(self, *a, **kw):
            return True
    answers = []

    async def answer(*a, **kw):
        answers.append(a)
    cb = types.SimpleNamespace(bot=_Bot(), answer=answer,
                               message=types.SimpleNamespace(chat=types.SimpleNamespace(id=1), message_id=7))
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(GatewayJoinCode.value)
    await state.update_data(gwjoin_slot=1, gwjoin_id=jid)
    await h._cancel(cb, services, GwJoinCB(action="cancel", slot=1, jid=jid), state)
    assert await state.get_state() is None and services.gwjoin(1) == {}
    services.gwjoin_issue()
    other = services.gwjoin(1)["id"]
    services.gwjoin_set_message(1, 1, 7)
    await state.set_state(GatewayJoinCode.value)
    await state.update_data(gwjoin_slot=2, gwjoin_id="f" * 16)
    await h._cancel(cb, services, GwJoinCB(action="cancel", slot=1, jid=other), state)
    assert await state.get_state() == GatewayJoinCode.value.state


async def test_a_double_press_issues_one_command(services, vps, monkeypatch):
    """ui-4: второе «Подключить шлюз» под тем же сообщением, пришедшее раньше,
    чем экран сменился, — всплывашка, вторая команда (и второй слот) не
    выпускается; под другим сообщением — своё нажатие."""
    import types
    from awgbot.bot.callbacks import GwMarkCB
    from awgbot.bot.handlers.settings import gwmark
    issued, toasts = [], []

    async def go(message, services, device_id=None, slot=0):
        issued.append(slot)

    async def done(cb, services):
        return None
    monkeypatch.setattr(gwmark, "_gateway_setup_go", go)
    monkeypatch.setattr(gwmark, "_screen_done", done)
    monkeypatch.setattr(services, "gw_bot_token", lambda slot: "DUMMY")

    def cb(mid):
        async def answer(text=None, **kw):
            toasts.append(text)
        return types.SimpleNamespace(answer=answer, message=types.SimpleNamespace(
            chat=types.SimpleNamespace(id=1), message_id=mid))

    class _State:
        async def clear(self):
            return None
    for mid in (5, 5, 6):
        await gwmark.gateway_new_yes(cb(mid), GwMarkCB(action="new_yes", slot=0), services, _State())
    assert len(issued) == 2 and toasts.count(gwmark.JOIN_PREPARING) == 1


@pytest.mark.parametrize("installed, releases, kit", [
    ("3.5.0", ["v3.4.0"], True),            # своя сборка будущего выпуска: на GitHub её нет
    ("3.4.0", ["v3.4.0"], False),
    ("3.4.0.2", ["v3.4.0"], False),         # хотфикс-сборка: шлюзу — v3.4.0 из выпуска
    ("3.5.0", None, False),                 # выпуски не проверялись — как обычно
])
def test_a_server_not_from_a_release_sends_the_gateway_to_the_kit(services, vps, monkeypatch, installed, releases,
                                                                  kit):
    """flow-12: версия в токене — та, что есть на GitHub, или Т-П1 ведёт к киту
    сервера (его код и есть эта версия)."""
    from awgbot.bot.texts.routing.join import join_message
    from awgbot.infra import updates
    monkeypatch.setattr(config, "INSTALLED_VERSION", installed)
    meta = {"schema": 1, "awg_gen": 1, "roles": {"main": {"requires": "3.4.0"}, "gw": {"requires": "3.4.0"}}}
    rels = None if releases is None else [
        updates.Release(tag=t, version=updates.parse_version(t), body="", asset_url=f"https://api/assets/{t}",
                        sha256="a" * 64, meta=updates.meta.check({**meta, "version": t.lstrip("v")}, t))
        for t in releases]
    monkeypatch.setattr(services, "fleet_releases", lambda: rels)
    st = services.gwjoin_issue()["state"]
    assert bool(st.get("kit")) is kit
    text = join_message(st, "curl … --join AWGJ1.DUMMY")
    assert ("sudo awg-bot gw-kit --stdout" in text) is kit and ("нужен доступ к GitHub" in text) is not kit
