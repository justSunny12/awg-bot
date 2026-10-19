"""Подключение шлюза командой из бота сервера AWG — сторона сервера целиком,
без сокетов: выпуск команды, сессия сопряжения и код, неверный код, верный
код и смена ключей после него (auto=join), фиксация, отказы и сроки.

Цена ошибки: команда, по которой подключается чужая машина; токен в чате,
который продолжает к чему-то подходить после отмены, срока или третьего
неверного кода; слот, видимый остальному боту до кода; код, который не
меняется с новой сессией.
"""
from __future__ import annotations

import pytest

from awgbot.domain.services.gwjoin import state as statemod
from awgbot.util import joinsas, jointoken, timeutil, wgconf

from tests.integration import _join_common as jc
from tests.integration._join_common import ADMIN, Gateway, accept, connect, slot_with_device

admin, vps = jc.admin, jc.vps                     # фикстуры сцены

pytestmark = pytest.mark.integration


def _issue(services, **kw) -> tuple[dict, Gateway]:
    res = services.gwjoin_issue(**kw)
    return res, Gateway(res["command"])


# ── выпуск команды ───────────────────────────────────────────────────────────

def test_the_command_puts_the_link_on_token_keys_and_shows_nobody_the_slot(services, vps):
    """«🛰 Подключить шлюз»: новый слот — линк на ключах токена со штампом
    сопряжения (карантин файервола), строки слота нет; в токене — ключ
    шлюзовой стороны, открытый ключ стороны ВПС, обфускация линка, выпуск,
    срок, хост и порты."""
    res, gwside = _issue(services)
    st = res["state"]
    assert vps.modes == ["--join-stage"] and vps.runs[0][1]["JOIN_ID"] == st["id"]
    assert "JOIN_PREV" not in vps.runs[0][1]
    assert services.db.gateways() == [] and st["slot"] == 1 and st["kind"] == "new"
    assert vps.stamped("awglink"), "линк сопряжения без штампа — карантина нет"
    tok = gwside.token
    gconf = wgconf.parse(vps.gw("awglink").read_text(encoding="utf-8"))
    vconf = wgconf.parse(vps.live("awglink").read_text(encoding="utf-8"))
    assert tok.gw_priv_b64 == gconf.get("Interface", "PrivateKey")
    assert tok.vps_pub_b64 == wgconf.pubkey_of(vconf.get("Interface", "PrivateKey"))
    assert (tok.jc, tok.jmin, tok.jmax, tok.s, tok.h) == (4, 40, 70, (10, 20, 0, 0),
                                                          ((100, 200), (300, 400), (500, 600), (700, 800)))
    assert tok.host == "vpn-server.example.org" and tok.link_port == 443 and tok.slot == 1
    assert tok.join_id == st["id"] and tok.version == st["version"]
    assert tok.expires == int(timeutil.parse_iso(st["expires"]).timestamp())
    assert "PresharedKey" not in vps.gw("awglink").read_text(encoding="utf-8"), "PSK — только после кода"
    assert res["command"].endswith(f"--join {jointoken.encode(tok)}")
    assert services.gwjoin_card(1) == {"state": "waiting", "until": st["expires"]}


def test_a_second_new_command_takes_the_next_slot(services, vps):
    """Два новых подключения подряд — разные слоты: строки у первого ещё нет,
    но его номер, интерфейс, порт и /30 заняты."""
    a, _ = _issue(services)
    b, _ = _issue(services)
    assert (a["state"]["slot"], b["state"]["slot"]) == (1, 2)
    assert b["state"]["link_if"] == "awglink2" and b["state"]["port"] == 8443


# ── сессия сопряжения и код ──────────────────────────────────────────────────

def test_hello_gives_both_sides_one_code_and_a_new_session_a_new_code(services, vps):
    _res, gwside = _issue(services)
    first = connect(services, gwside, 1)
    assert first["event"] == "connected" and first["kind"] == "join_eph"
    assert statemod._LIVE[1]["code"] == gwside.code, "коды сторон разошлись"
    code1 = gwside.code
    again = Gateway(_res["command"])
    second = connect(services, again, 1)
    assert second["event"] == "reconnected"
    assert again.code != code1 and statemod._LIVE[1]["code"] == again.code, "код не сменился с новой сессией"


def test_hello_of_another_command_or_garbage_is_refused(services, vps):
    _res, gwside = _issue(services)
    assert services.gwjoin_hello(1, {**gwside.hello(), "join": "f" * 16}) == {"ok": False, "why": "gone"}
    assert services.gwjoin_hello(1, {**gwside.hello(), "eph": "AAAA"})["why"] == "bad"


def test_more_than_ten_sessions_are_refused(services, vps):
    res, _ = _issue(services)
    for _ in range(statemod.MAX_SESSIONS):
        connect(services, Gateway(res["command"]), 1)
    assert services.gwjoin_hello(1, Gateway(res["command"]).hello())["why"] == "sessions"


@pytest.mark.parametrize("host, shown", [("raspberrypi", "raspberrypi"), ("evil;rm -rf", ""), ("x" * 64, ""),
                                         ("", "")])
def test_the_hostname_from_hello_is_shown_only_when_clean(services, vps, host, shown):
    """hostname из hello — в сообщении «шлюз подключился»; мусорный или длинный — отброшен, без
    поля — без «(hostname: …)»."""
    from awgbot.bot.texts.routing import join as jt
    res, gwside = _issue(services)
    out = connect(services, gwside, 1, host=host)
    st = services.gwjoin(1)
    assert st["hosts"].get("203.0.113.50", "") == shown
    text = jt.join_message(st, res["command"], event=out["event"])
    assert ("(hostname: " in text) is bool(shown), text


def test_two_addresses_on_one_token_are_named(services, vps):
    """К токену подключались с двух адресов — оба в сообщении."""
    from awgbot.bot.texts.routing import join as jt
    res, gwside = _issue(services)
    connect(services, gwside, 1)
    services.peer["addr"] = "198.51.100.23"
    out = connect(services, Gateway(res["command"]), 1, host="nas")
    assert out["event"] == "two"
    text = jt.join_message(services.gwjoin(1), res["command"], event="two")
    assert "203.0.113.50" in text and "198.51.100.23" in text and "(hostname: nas)" in text


def test_the_code_belongs_to_the_pressed_request_only(services, vps):
    """Ввод кода — только к заявке нажатой кнопки; до подключения шлюза
    кода нет; формат не тот — не попытка."""
    res, gwside = _issue(services)
    jid = res["state"]["id"]
    assert services.gwjoin_wait(1, jid) == "wait"
    assert services.gwjoin_code(1, jid, "K7F2QX")["result"] == "wait"
    connect(services, gwside, 1)
    assert services.gwjoin_wait(1, jid) == "ask" and services.gwjoin_wait(1, "0" * 16) == "gone"
    assert services.gwjoin_code(1, "0" * 16, gwside.code)["result"] == "gone"
    assert services.gwjoin_code(1, jid, "K7F")["result"] == "format"
    assert services.gwjoin(1)["attempts"] == 0, "ошибка формата сожгла попытку"


def test_three_wrong_codes_close_the_join_and_the_token_is_dead_at_once(services, vps):
    """Неверный ×2 — попытки; третий — закрыто: запись «closing», вызывающий
    шлёт join_refused и сразу снимает интерфейс под токен: новый
    слот — линк снят, ключи токена удалены, слушателю слушать нечего."""
    res, gwside = _issue(services)
    jid = res["state"]["id"]
    connect(services, gwside, 1)
    wrong = "AAAAAA" if gwside.code != "AAAAAA" else "BBBBBB"
    assert services.gwjoin_code(1, jid, wrong)["result"] == "wrong"
    assert services.gwjoin_code(1, jid, wrong)["result"] == "wrong"
    st = services.gwjoin(1)
    assert st["attempts"] == 2
    out = services.gwjoin_code(1, jid, wrong)
    assert out["result"] == "refused" and services.gwjoin(1)["state"] == "closing"
    assert services.gwjoin_addresses() == [], "слушатель остался бы на адресе закрытого сопряжения"
    services.gwjoin_revert(1, "code")
    assert vps.modes[-1] == "--rekey", "у первого слота линк обвязки — новые случайные ключи"
    assert not vps.stamped("awglink") and services.gwjoin(1) == {} and services.db.gateways() == []
    assert services.gwjoin_hello(1, gwside.hello())["why"] == "gone", "мёртвый токен открывает сессию"


def test_the_wrong_code_reports_the_attempts_left(services, vps):
    res, gwside = _issue(services)
    connect(services, gwside, 1)
    wrong = "AAAAAA" if gwside.code != "AAAAAA" else "BBBBBB"
    assert services.gwjoin_code(1, res["state"]["id"], wrong)["left"] == 2
    assert services.gwjoin_code(1, res["state"]["id"], wrong)["left"] == 1


def test_a_new_slot_that_is_not_the_first_is_rolled_back(services, vps):
    """Новый второй слот при отмене — линк снят целиком (--rollback), файлы
    с ключами токена удалены."""
    slot_with_device(services, vps, 1)
    res, gwside = _issue(services)
    assert res["state"]["slot"] == 2
    st = services.gwjoin_cancel(2, res["state"]["id"])
    assert st["state"] == "closing"
    services.gwjoin_revert(2, "cancel")
    assert vps.modes[-1] == "--rollback" and not vps.live("awglink2").exists() and not vps.gw("awglink2").exists()


# ── верный код ───────────────────────────────────────────────────────────────

def test_the_right_code_makes_the_slot_and_rekeys_without_asking(services, vps):
    """Верный код: строка слота и устройство «Шлюз», join_ok — секреты под
    ключом сопряжения, смена ключей auto=join (без вопроса в чате бота
    шлюза), после фиксации штампа нет — карантин снят; агент «установлен» —
    запись снята, итог «шлюз подключён»."""
    res, gwside = _issue(services)
    connect(services, gwside, 1)
    st = services.gwjoin(1)
    out = services.gwjoin_code(1, st["id"], joinsas.show(gwside.code).lower())
    assert out["result"] == "ok"
    gw = services.db.gateway(1)
    assert gw is not None and services.db.get_device(gw.device_id).name == "Шлюз"
    body = services.gwjoin_ok_body(1)
    secrets_ = joinsas.unseal(gwside.kp, body["box"])
    assert secrets_["admin_id"] == ADMIN and secrets_["slot"] == 1
    rk = services.gateway_rekey(1)
    assert rk["auto"] == "join" and rk["state"] == "staged"
    nxt = services.gateway_rekey_next_body(1)
    assert nxt.get("auto") == "join", nxt
    action, _ = services.gateway_rekey_ack_in(1, {"id": rk["id"], "state": "ready"})
    assert action == "go"
    services.gateway_rekey_go(1)
    assert services.gateway_rekey_commit(1)[0] is True
    assert services.gateway_rekey_hello(1)["rekeyed"]
    assert services.gwjoin(1)["state"] == "linked" and not vps.stamped("awglink")
    assert "PresharedKey" in vps.gw("awglink").read_text(encoding="utf-8"), "PSK после кода не встал"
    assert services.gwjoin_active() is False
    done = services.gwjoin_installed(1)
    assert done["id"] == st["id"] and services.gwjoin(1) == {}


def test_after_the_right_code_cancel_is_impossible(services, vps):
    res, gwside = _issue(services)
    connect(services, gwside, 1)
    accept(services, vps, gwside, 1)
    assert services.gwjoin_cancel(1, res["state"]["id"]) == {}


def test_the_join_ok_is_repeated_only_to_the_gateway_whose_code_was_entered(services, vps):
    """Сессия оборвалась после верного кода — новая сессия ТОГО ЖЕ шлюза (тот
    же эфемерный ключ, агент держит пару до join_ok) получает «accepted» и
    тот же путь: join_ok и смену ключей. Чужой hello с тем же токеном и своим
    эфемерным ключом — отказ (quar-1): ни секретов, ни постоянных ключей."""
    res, gwside = _issue(services)
    connect(services, gwside, 1)
    services.gwjoin_code(1, res["state"]["id"], gwside.code)
    assert services.gwjoin_hello(1, Gateway(res["command"]).hello()) == {"ok": False, "why": "gone"}
    again = Gateway(res["command"])
    again.priv, again.pub = gwside.priv, gwside.pub              # тот же шлюз — та же пара
    out = connect(services, again, 1)
    assert out["accepted"] and out["body"]["accepted"] is True
    assert joinsas.unseal(again.kp, services.gwjoin_ok_body(1)["box"])["admin_id"] == ADMIN


# ── сроки, отмена, новая команда ─────────────────────────────────────────────

def _expire(services, slot):
    st = services.gwjoin(slot)
    st["expires"] = timeutil.to_iso(timeutil.shift(timeutil.now(), -1))
    services._gwjoin_save(slot, st)


def test_the_hour_ends_the_join_at_once(services, vps):
    """Час вышел — такт отдаёт запись на закрытие; hello по токену
    после срока — отказ и тоже на закрытие (сначала отказ шлюзу)."""
    _res, gwside = _issue(services)
    _expire(services, 1)
    closed = services.gwjoin_tick()
    assert [c["slot"] for c in closed] == [1] and services.gwjoin(1)["state"] == "closing"
    services.gwjoin_revert(1, "expired")
    assert services.gwjoin(1) == {} and not vps.stamped("awglink")
    res2, gw2 = _issue(services)
    _expire(services, 1)
    out = services.gwjoin_hello(1, gw2.hello())
    assert out["why"] == "expired" and services.gwjoin(1)["state"] == "closing"
    assert vps.stamped("awglink"), "возврат раньше отказа шлюзу — отказ не дошёл бы"


def test_an_expired_code_input_closes_too(services, vps):
    res, gwside = _issue(services)
    connect(services, gwside, 1)
    _expire(services, 1)
    out = services.gwjoin_code(1, res["state"]["id"], gwside.code)
    assert out["result"] == "expired" and services.gwjoin(1)["state"] == "closing"


def test_a_new_command_for_the_same_slot_kills_the_old_one(services, vps):
    """Новая команда того же слота: прежняя мертва (её сессия и код —
    тоже), её сообщение — «Не актуально»."""
    slot_with_device(services, vps, 1)
    first, gw1 = _issue(services, slot_id=1)
    connect(services, gw1, 1)
    services.gwjoin_set_message(1, ADMIN, 41)
    second, _gw2 = _issue(services, slot_id=1)
    assert second["stale"] == [ADMIN, 41]
    assert services.gwjoin_hello(1, gw1.hello())["why"] == "gone"
    assert 1 not in statemod._LIVE, "код прежней команды пережил новую"
    assert services.gwjoin(1)["id"] == second["state"]["id"] != first["state"]["id"]


def test_the_firewall_and_listener_follow_the_join(services, vps):
    """Слушатель канала — на адресе ВПС линка сопряжения, пока оно живо;
    штамп — в конфиге стороны ВПС (по нему файервол держит карантин)."""
    from awgbot.infra import nftguard
    res, gwside = _issue(services)
    assert services.gwjoin_addresses() == [(1, "10.99.99.1")]
    links = ["awglink"]
    assert nftguard.quarantine_ifaces(links) == ["awglink"]
    connect(services, gwside, 1)
    accept(services, vps, gwside, 1)
    assert nftguard.quarantine_ifaces(links) == [], "после фиксации карантин остался"
    assert services.gwjoin_addresses() == [], "после фиксации — обычная сессия слота"
