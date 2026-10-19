"""Подключение шлюза командой после перепроверки выпуска: секреты после
кода, копия во время сопряжения, I1 и возврат слота соседям.

- link_next сессии сопряжения: конфиг новых ключей только под ключом
  сопряжения Kp (поле box), без conf; сессии (и Kp) нет — не уходит вовсе;
- копия (кнопка, месячная, по событию) ждёт и сопряжения: линк на ключах
  токена со штампом, .join-prev в копию не попадает — после восстановления
  карантин навсегда; всплывашка говорит о подключении, а не о смене ключей;
- смена ключей ради I1, снятая сопряжением слота, — не неудача I1 (иначе
  ложная строка «не встало трижды»);
- отмена, срок и третий неверный код замены возвращают слоту политику
  маршрутизации и подсети соседям, канал будится.

Цена ошибки: держатель токена, перетянувший endpoint пира, читает постоянный
ключ слота; восстановленный бэкап с вечным карантином линка; ложная тревога
в карточке слота; сосед без маршрута в сеть слота после отмены замены.
"""
from __future__ import annotations

import pytest

from awgbot.domain.notices import backup as nb
from awgbot.domain.services.gwjoin import state as statemod
from awgbot.infra import routing
from awgbot.util import joinsas

from tests.integration import _join_common as jc
from tests.integration._join_common import Gateway, connect, slot_with_device

pytestmark = pytest.mark.integration
admin, vps = jc.admin, jc.vps                     # фикстуры сцены


def _accepted_new(services):
    """Новый слот 1: команда, hello шлюза, верный код — смена ключей заведена."""
    res = services.gwjoin_issue()
    side = Gateway(res["command"])
    connect(services, side, 1)
    st = services.gwjoin(1)
    assert services.gwjoin_code(1, st["id"], joinsas.show(side.code))["result"] == "ok"
    return side


# ── sec-1: новые ключи — под ключом сопряжения ──────────────────────────────

def test_the_new_keys_after_the_code_travel_only_under_the_pairing_key(services, vps):
    side = _accepted_new(services)
    plain = services.gateway_rekey_next_body(1)
    body = services.gwjoin_next_body(1)
    assert body is not None and body["id"] == plain["id"] and body.get("auto") == "join"
    assert "conf" not in body, "конфиг новых ключей ушёл открытым (ключ канала выводится из токена)"
    with pytest.raises(ValueError):
        joinsas.unseal(bytes(32), body["box"])
    assert joinsas.unseal(side.kp, body["box"]) == {"conf": plain["conf"]}
    assert "PrivateKey" in plain["conf"] and "PresharedKey" in plain["conf"]


def test_without_a_live_pairing_session_no_keys_are_sent(services, vps):
    """Сессия сопряжения ушла (Kp есть только у неё) — link_next не уходит
    ни под каким ключом; смена ключей при этом не снята."""
    _accepted_new(services)
    statemod._LIVE.clear()
    assert services.gwjoin_next_body(1) is None
    assert services.gateway_rekey(1).get("state") == "staged"


# ── cross-2: копия ждёт сопряжения ──────────────────────────────────────────

def test_a_copy_waits_for_a_new_slot_being_joined(services, vps, monkeypatch):
    """Новый слот: строки в gateways ещё нет, линк со штампом уже стоит —
    копия ждёт; после отмены и возврата — нет."""
    res = services.gwjoin_issue()
    assert services.db.gateway(1) is None, "сценарий собран не так: строка слота до кода"
    busy = services.backup_rekey_busy()
    assert busy is not None and busy.slot == 1 and busy.joining, "копия снимется посреди сопряжения нового слота"
    toast = nb.rekey_wait_toast(busy.name, joining=busy.joining)
    assert toast.startswith("Идёт подключение шлюза «") and "смена ключей" not in toast, toast
    monkeypatch.setattr("awgbot.domain.services.backups.EVENT_SETTLE_S", 0)
    services.backup_set_passphrase("correct horse battery staple")
    services.backup_event_mark("join")
    assert services.backup_event_take() is None, "копия по событию не ждёт сопряжения"
    services.gwjoin_cancel(1, res["state"]["id"])
    assert services.backup_rekey_busy() is not None, "возврат (closing) ещё идёт — копия рано"
    services.gwjoin_revert(1, "cancel")
    assert services.backup_rekey_busy() is None
    assert services.backup_event_take() is not None, "после сопряжения копия по событию так и не снялась"


def test_a_copy_waits_for_a_device_replacement_and_names_the_slot(services, vps):
    slot_with_device(services, vps, 1)
    res = services.gwjoin_issue(slot_id=1)
    busy = services.backup_rekey_busy()
    assert busy is not None and busy.slot == 1 and busy.joining
    assert nb.rekey_wait_toast(busy.name, joining=busy.joining) == \
        "Идёт подключение шлюза «NASPi» — бэкап сниму сразу после него"
    services.gwjoin_cancel(1, res["state"]["id"])
    services.gwjoin_revert(1, "cancel")
    assert services.backup_rekey_busy() is None


async def test_the_backup_button_says_the_gateway_is_being_joined(services, vps, fake_bot):
    from awgbot.bot.handlers.settingscore import actions
    from awgbot.core import config
    from tests.conftest import FakeCallback, FakeMessage
    slot_with_device(services, vps, 1)
    services.gwjoin_issue(slot_id=1)
    services.backup_set_passphrase("correct horse battery staple")
    made = []
    services.make_backup = lambda: made.append(1) or []
    msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=config.ADMIN_ID, bot=fake_bot)
    await actions.backup_now(cb, services, None)
    assert cb.answers == [("Идёт подключение шлюза «NASPi» — бэкап сниму сразу после него", True)], cb.answers
    assert made == [], "копия снята посреди сопряжения"


# ── cross-5: смена ради I1, снятая сопряжением, — не неудача ────────────────

def test_a_rekey_for_i1_taken_away_by_the_pairing_is_not_a_failure(services, vps, monkeypatch):
    from awgbot.util import gwlink
    from tests.unit import test_link_i1 as i1t
    slot_with_device(services, vps, 1)
    i1t._client(monkeypatch, i1t.AWG1)
    services.gwlink_session_opened(1, "3.4.0", gwlink.PROTO, caps=("cfg1", "confirm1", "upd1", "rekey2"))
    monkeypatch.setattr(services, "gateway_link_i1", lambda gw: (True, "<b 0x01>"))
    services._i1_plan_save(1, {"want": i1t.I1, "rid": "a1b2c3d4e5f6", "tries": 0,
                               "next_at": "2020-01-01T00:00:00+00:00"})
    services.gwjoin_issue(slot_id=1)                      # замена устройства сняла смену плана
    for _ in range(3):
        started, _notes = services.gateway_i1_tick()
        assert started == [], "смена ради I1 начата посреди сопряжения"
    plan = services.gateway_i1_plan(1)
    assert int(plan.get("tries") or 0) == 0 and not plan.get("rid"), plan
    assert services.gateway_i1_line(1) is None, "ложная строка «не встало» в карточке"


# ── honest-3: возврат после отмены замены — политика и соседи ───────────────

@pytest.mark.parametrize("how", ["cancel", "expired", "code"])
def test_giving_the_link_back_restores_the_slot_policy_and_the_neighbours(services, vps, monkeypatch, how):
    """Отмена, срок, третий неверный код замены: политика маршрутизации
    слотов поставлена заново с этим слотом, соседу его подсети снова видны,
    канал разбужен (документы соседям — с его подсетями)."""
    services.add_device(services.admin_client().id, "Pi4")
    slot_with_device(services, vps, 1)
    slot_with_device(services, vps, 2, "Pi4")
    for n, net in ((1, "198.51.100.0/24"), (2, "203.0.113.0/24")):
        services.db.gateway_update(n, lan_subnets=[net], lan_mode=1)
    services.db.set_state(services._RT_ACTIVE_KEY, "2")
    before = services.gateway_peer_nets(2)
    assert before, "сценарий собран не так: у соседа нет подсетей слота 1"
    policies: list = []
    part = next(m for m in routing._PARTS if "ensure_policy" in vars(m))
    monkeypatch.setattr(part, "ensure_policy", lambda active_iface="", slots=(): policies.append(
        (active_iface, sorted(slots))))
    touched: list = []
    monkeypatch.setattr(services, "channel_touch", lambda: touched.append(1), raising=False)
    res = services.gwjoin_issue(slot_id=1)
    side = Gateway(res["command"])
    assert services.gateway_peer_nets(2) == [], "сценарий собран не так: слот в замене виден соседу"
    jid = res["state"]["id"]
    if how == "cancel":
        services.gwjoin_cancel(1, jid)
    elif how == "expired":
        st = services.gwjoin(1)
        st["expires"] = st["issued"]
        services._gwjoin_save(1, st)
        services.gwjoin_tick()
    else:
        connect(services, side, 1)
        wrong = "AAAAAA" if side.code != "AAAAAA" else "BBBBBB"
        for _ in range(3):
            services.gwjoin_code(1, jid, wrong)
    policies.clear(); touched.clear()
    assert services.gwjoin_revert(1, how) == ""
    assert policies and policies[-1] == ("awglink2", [(1, "awglink"), (2, "awglink2")]), \
        f"политика слота не вернулась после возврата: {policies}"
    assert touched, "канал не разбужен — соседи не узнают, что слот вернулся"
    assert services.gateway_peer_nets(2) == before, "подсети слота не вернулись соседу"
