"""Сопряжение серверов кодом на сервисах (domain/services/reserve/invite.py,
pairing.py): приглашение, сессия сопряжения, код и его попытки, срок,
секреты после кода под ключом сопряжения, проверки принимающего (Т-Р4),
строка пары и ключи первого поколения.

Вторая сторона — модель из _reserve_common (тот же util/joinsas, домен
пары), скрипт линка пары — модель его файловых эффектов, файервол — журнал.

Цена ошибки: третий неверный код, после которого приглашение живо, —
перебор кода; ADMIN_ID второй стороны из сообщения до кода — роль в чужом
боте любому держателю строки приглашения; приглашение, выданное без
карантина в файерволе, — чужая машина с линка пары ходит по хосту; пара
без ключей переезда — аварийного кода нет, а админу сказано, что есть.
"""
from __future__ import annotations

import base64

import pytest

from awgbot.core import settings
from awgbot.domain.services.reserve import invite as invitemod
from awgbot.domain.services.types import ServiceError
from awgbot.util import joinsas, pairproto, pairtoken, timeutil

from tests.integration import _reserve_common as rc
from tests.integration._reserve_common import ADDR_B, ADDR_X, ADMIN, PEER_ADMIN, Acceptor, Inviter

pytestmark = pytest.mark.integration
rsv = rc.rsv


# ── приглашение (пригласивший) ───────────────────────────────────────────────

def test_the_invitation_carries_the_staged_keys_and_is_quarantined_at_once(services, rsv):
    res = services.reserve_invite_issue()
    st, tok = res["state"], pairtoken.decode(res["token"])
    stage = rsv.env("--stage")
    assert stage["PAIR_ID"] == st["id"] == tok.pair_id and stage["PAIR_IF"] == "awgpair"
    assert (tok.host, tok.pair_port, tok.peer_addr, tok.cidr) == (rc.HOST_A, 47101, "10.99.98.1", "10.99.98.0/30")
    out = rsv.out["--stage"]
    assert base64.b64encode(tok.peer_priv).decode() == out["PAIR_PEER_PRIV"], "в приглашении не тот закрытый ключ"
    assert base64.b64encode(tok.inviter_pub).decode() == out["PAIR_VPS_PUB"]
    assert tok.obfuscation()["H4"] == out["H4"] and tok.jc == int(out["JC"])
    assert services.reserve_invite()["state"] == "waiting" and services.reserve_invite_live()
    assert rsv.fw == [True], "карантин линка пары не нарисован строго сразу"
    assert rsv.stamped(), "конфиг линка пары без штампа карантина"


def test_a_new_invitation_kills_the_previous_one(services, rsv):
    first = services.reserve_invite_issue()
    services.reserve_invite_set_message(ADMIN, 555)
    second = services.reserve_invite_issue()
    assert second["stale"] == [ADMIN, 555], "сообщение прежнего приглашения не отдано на «Не актуально»"
    assert rsv.modes == ["--stage", "--rollback", "--stage"], rsv.modes
    assert services.reserve_invite()["id"] == second["state"]["id"] != first["state"]["id"]


def test_no_invitation_without_the_firewall_quarantine(services, rsv):
    rsv.fw_fail = True
    with pytest.raises(ServiceError, match="Приглашение не выдано"):
        services.reserve_invite_issue()
    assert services.reserve_invite() == {} and rsv.modes[-1] == "--rollback" and not rsv.conf.exists()


def test_a_failed_stage_leaves_no_invitation_and_no_link(services, rsv):
    """--stage упал на полпути (конфиг записан, линк не встал) — приглашения
    нет и линка под него тоже: откат, как при отмене (у подключения шлюза
    так же — `--join-revert` после отказа стадии)."""
    rsv.fail["--stage"] = "линк пары не поднялся"
    with pytest.raises(ServiceError):
        services.reserve_invite_issue()
    assert services.reserve_invite() == {}
    assert rsv.modes[-1] == "--rollback", f"после отказа стадии отката нет: {rsv.modes}"


def test_no_invitation_without_an_address_or_with_a_pair(services, rsv):
    settings.set_value("app.network.server_host", "")
    with pytest.raises(ServiceError, match="адрес"):
        services.reserve_invite_issue()
    settings.set_value("app.network.server_host", rc.HOST_A)
    rc.pair_row(services)
    with pytest.raises(ServiceError, match="уже подключён"):
        services.reserve_invite_issue()
    assert rsv.modes == [], "скрипт линка пары запущен при отказе"


def test_no_invitation_while_readiness_is_off(services, rsv):
    settings.set_value("app.reserve.ready", False)
    with pytest.raises(ServiceError, match="Готов быть резервом"):
        services.reserve_invite_issue()
    assert rsv.modes == [] and services.reserve_invite() == {}


def test_cancel_drops_the_link_and_a_second_cancel_says_there_is_none(services, rsv):
    services.reserve_invite_issue()
    services.reserve_invite_cancel()
    assert services.reserve_invite() == {} and rsv.modes[-1] == "--rollback" and not rsv.conf.exists()
    with pytest.raises(ServiceError, match="Приглашения нет"):
        services.reserve_invite_cancel()


def test_the_hour_runs_out_and_the_tick_drops_the_invitation_once(services, rsv, monkeypatch):
    services.reserve_invite_issue()
    assert services.reserve_invite_tick() is None, "свежее приглашение снято тактом"
    later = timeutil.shift(timeutil.now(), invitemod.INVITE_TTL + 1)
    monkeypatch.setattr(timeutil, "now", lambda: later)
    st = services.reserve_invite_tick()
    assert st and st["event"] == "expired" and services.reserve_invite() == {} and rsv.modes[-1] == "--rollback"
    assert services.reserve_invite_tick() is None, "срок отработан второй раз"


# ── сессия сопряжения у пригласившего ───────────────────────────────────────

def test_hello_gives_the_ephemeral_key_and_the_same_code_on_both_sides(services, rsv):
    res, acc, out = rc.invite_and_hello(services, rsv)
    assert out["event"] == "connected"
    assert invitemod._live()["code"] == acc.code, "коды сторон разошлись — сопряжение невозможно"
    body = pairproto.check(pairproto.PAIR_PROTO, "pair_eph", out["body"], pairproto.INVITER)
    assert "admin_id" not in out["body"] and "admin_id" not in body, "ADMIN_ID ушёл до кода"
    st = services.reserve_invite()
    assert (st["state"], st["endpoints"], st["peer_name"]) == ("connected", [ADDR_B], "Beta")


def test_hello_from_a_second_address_is_the_two_event_and_the_tail_names_both(services, rsv):
    from awgbot.bot.texts.reserve import pairing as pt
    res, acc, _ = rc.invite_and_hello(services, rsv)
    out = services.reserve_pair_hello(Acceptor(res["state"]["id"]).hello(addr=ADDR_X))
    assert out["ok"] and out["event"] == "two", out
    tail = pt.invite_tail(services.reserve_invite())
    assert ADDR_B in tail and ADDR_X in tail and "двух адресов" in tail, tail


def test_a_reconnect_from_the_same_address_changes_the_code(services, rsv):
    res, acc, _ = rc.invite_and_hello(services, rsv)
    again = Acceptor(res["state"]["id"])
    out = services.reserve_pair_hello(again.hello())
    assert out["ok"] and out["event"] == "reconnected"
    assert again.eph_in(out["body"]) == invitemod._live()["code"] != acc.code, "код не сменился после переподключения"


@pytest.mark.parametrize("hello,why", [({"pair": "ffffffffffffffff"}, "gone"), ({"eph": "AAAA"}, "bad")],
                         ids=["чужое приглашение", "битый ключ"])
def test_a_foreign_or_broken_hello_is_refused(services, rsv, hello, why):
    res = services.reserve_invite_issue()
    out = services.reserve_pair_hello(Acceptor(res["state"]["id"]).hello(**hello))
    assert (out["ok"], out["why"]) == (False, why)
    assert invitemod._live() == {}, "отвергнутый hello завёл сессию"


def test_sessions_are_capped(services, rsv):
    res = services.reserve_invite_issue()
    for _ in range(invitemod.MAX_SESSIONS):
        assert services.reserve_pair_hello(Acceptor(res["state"]["id"]).hello())["ok"]
    out = services.reserve_pair_hello(Acceptor(res["state"]["id"]).hello())
    assert (out["ok"], out["why"]) == (False, "sessions"), "подключений к приглашению больше потолка"


def test_hello_after_the_hour_is_expired_and_closes(services, rsv, monkeypatch):
    res = services.reserve_invite_issue()
    later = timeutil.shift(timeutil.now(), invitemod.INVITE_TTL + 1)
    monkeypatch.setattr(timeutil, "now", lambda: later)
    out = services.reserve_pair_hello(Acceptor(res["state"]["id"]).hello())
    assert (out["ok"], out["why"]) == (False, "expired") and services.reserve_invite()["state"] == "closing"


def test_nobody_has_a_gateway_and_the_pairing_is_refused(services, rsv):
    """Шлюза с `rsv1` нет ни у этого сервера, ни у принявшего (его `gw` в
    hello) — сопряжение отказано (Т-Р4): резерву нечем подтвердить падение ни
    в одну сторону, а пара без единого готового направления — ложная
    уверенность у обоих админов."""
    rsv.gw = False
    res = services.reserve_invite_issue()
    out = services.reserve_pair_hello(Acceptor(res["state"]["id"]).hello(gw=False))
    assert (out["ok"], out.get("why")) == (False, "nogw"), "сопряжение без шлюзов у обеих сторон принято"
    assert invitemod._live() == {}, "отвергнутый hello завёл сессию и код"


def test_a_gateway_on_one_side_pairs_with_one_ready_direction(services, rsv):
    """Шлюз только у этого сервера: сопряжение идёт, направление «мои →
    к нему» готово, обратное — нет, причиной строкой про шлюз."""
    rsv.gw = True
    rc.pair_as_inviter(services, rsv, gw=False)
    dirs = services.reserve_directions()
    assert dirs["out"] == (True, ""), dirs
    assert dirs["in"][0] is False and "шлюз" in dirs["in"][1], dirs


# ── код ──────────────────────────────────────────────────────────────────────

def test_the_code_is_asked_only_once_the_reserve_connected(services, rsv):
    services.reserve_invite_issue()
    assert services.reserve_pair_wait() == "wait"
    assert services.reserve_pair_code("K7F2QX")["result"] == "wait"
    assert services.reserve_invite()["attempts"] == 0, "код до подключения резерва съел попытку"


def test_a_malformed_code_costs_no_attempt(services, rsv):
    rc.invite_and_hello(services, rsv)
    assert services.reserve_pair_code("K7F")["result"] == "format"
    assert services.reserve_invite()["attempts"] == 0


def test_wrong_codes_count_down_and_the_third_closes_the_invitation(services, rsv):
    _res, acc, _ = rc.invite_and_hello(services, rsv)
    wrong = "AAAAAA" if acc.code != "AAAAAA" else "BBBBBB"
    assert [services.reserve_pair_code(wrong).get("left") for _ in range(2)] == [2, 1]
    third = services.reserve_pair_code(wrong)
    assert (third["result"], third["left"]) == ("refused", 0)
    st = services.reserve_invite()
    assert (st["state"], st["why"]) == ("closing", "code")
    assert services.reserve_pair_code(acc.code)["result"] == "gone", "верный код после третьего неверного принят"
    assert services.reserve_pair_wait() == "gone"


def test_the_right_code_is_case_and_dash_insensitive_and_pins_the_session(services, rsv):
    res, acc, _ = rc.invite_and_hello(services, rsv)
    shown = joinsas.show(acc.code).lower()
    assert services.reserve_pair_code(shown)["result"] == "ok"
    st = services.reserve_invite()
    assert st["state"] == "accepted"
    # после кода — только та же эфемерная пара: чужой hello не перехватит сессию
    out = services.reserve_pair_hello(Acceptor(res["state"]["id"]).hello(addr=ADDR_X))
    assert (out["ok"], out["why"]) == (False, "gone"), "после кода сессию принял другой эфемерный ключ"
    same = services.reserve_pair_hello({**acc.hello(), "_peer_addr": ADDR_B})
    assert same["ok"], "тот же принявший не смог переподключиться после кода"


def test_a_code_after_the_hour_is_expired(services, rsv, monkeypatch):
    _res, acc, _ = rc.invite_and_hello(services, rsv)
    later = timeutil.shift(timeutil.now(), invitemod.INVITE_TTL + 1)
    monkeypatch.setattr(timeutil, "now", lambda: later)
    assert services.reserve_pair_code(acc.code)["result"] == "expired"
    assert services.reserve_invite()["state"] == "closing"


# ── после кода: секреты под ключом сопряжения ───────────────────────────────

def test_pair_ok_carries_the_new_link_keys_and_the_admin_id_only_under_the_pairing_key(services, rsv):
    _res, acc, _ = rc.invite_and_hello(services, rsv)
    services.reserve_pair_code(acc.code)
    ok = services.reserve_pair_ok_body()
    assert set(ok) == {"sealed"}, f"в pair_ok что-то открытым текстом: {set(ok) - {'sealed'}}"
    data = joinsas.unseal(acc.kp, ok["sealed"])
    stage = rsv.out["--rekey-stage"]
    assert (data["psk"], data["priv"], data["peer_pub"]) == (stage["PAIR_PSK"], stage["PAIR_PEER_PRIV"],
                                                             stage["PAIR_VPS_PUB"])
    assert data["admin_id"] == ADMIN and data["obf"]["H4"] == stage["H4"]
    with pytest.raises(ValueError):
        joinsas.unseal(bytes(32), ok["sealed"])
    assert rsv.modes[-1] == "--rekey-stage" and rsv.p(".next").exists() and rsv.stamped(), \
        "живой линк тронут до ответа принявшего"


def test_no_pair_ok_before_the_code(services, rsv):
    rc.invite_and_hello(services, rsv)
    assert services.reserve_pair_ok_body() == {}
    assert "--rekey-stage" not in rsv.modes


def test_pair_admin_commits_the_keys_and_writes_the_pair(services, rsv):
    acc = rc.pair_as_inviter(services, rsv)
    assert rsv.modes[-3:] == ["--rekey-stage", "--rekey-commit", "--rekey-drop"], rsv.modes
    assert not rsv.stamped(), "после кода штамп карантина остался"
    peer = services.reserve_peer()
    assert (peer["name"], peer["host"], peer["listener"], peer["peer_admin_id"], peer["bot_username"]) == \
        ("Beta", ADDR_B, 1, PEER_ADMIN, "beta_vpn_bot")
    assert services.reserve_invite() == {} and invitemod._live() == {}, "приглашение пережило сопряжение"
    keys = services.reserve_keys()
    assert keys["m_gen"] == 1 and keys["k_gen"] == 1 and services.reserve_code(), "нет ключей первого поколения"
    assert (services.reserve_out(), services.reserve_in()) == ("home", "idle")
    assert services.rsv_get("rsv_peer_admin")["admin_id"] == PEER_ADMIN
    assert services.reserve_channel_secret() == acc.secrets["psk"], "канал пары не на PSK после кода"
    assert services.reserve_link_role() == "inviter"


def test_a_pair_admin_sealed_by_someone_else_writes_nothing(services, rsv):
    _res, acc, _ = rc.invite_and_hello(services, rsv)
    services.reserve_pair_code(acc.code)
    services.reserve_pair_ok_body()
    forged = joinsas.seal(bytes(32), {"admin_id": 31337, "self_host": ADDR_X})
    with pytest.raises(ValueError):
        services.reserve_pair_admin_in({"sealed": forged})
    assert not services.reserve_paired() and "--rekey-commit" not in rsv.modes


# ── принявший: проверки строки (Т-Р4) ───────────────────────────────────────

@pytest.mark.parametrize("text,frag", [("привет", "не приглашение"), ("AWGP1." + "A" * 60, "повреждено")],
                         ids=["не приглашение", "повреждено"])
def test_garbage_is_refused_with_its_reason(services, rsv, text, frag):
    with pytest.raises(ServiceError, match=frag):
        services.reserve_accept_parse(text)


def test_a_corrupted_character_is_damage(services, rsv):
    tok = Inviter().token
    bad = tok[:-5] + ("B" if tok[-5] != "B" else "C") + tok[-4:]
    with pytest.raises(ServiceError, match="повреждено"):
        services.reserve_accept_parse(bad)


def test_an_expired_invitation_names_the_time(services, rsv):
    with pytest.raises(ServiceError, match="Срок приглашения вышел"):
        services.reserve_accept_parse(Inviter(expires_in=-60).token)


def test_a_server_with_a_pair_refuses_another(services, rsv):
    rc.pair_row(services)
    with pytest.raises(ServiceError, match="Резерв уже есть — «Beta»"):
        services.reserve_accept_parse(Inviter().token)


def test_its_own_invitation_is_refused(services, rsv):
    res = services.reserve_invite_issue()
    with pytest.raises(ServiceError, match="этого же сервера"):
        services.reserve_accept_parse(res["token"])


def test_readiness_off_refuses(services, rsv):
    settings.set_value("app.reserve.ready", False)
    with pytest.raises(ServiceError, match="Готов быть резервом"):
        services.reserve_accept_parse(Inviter().token)


def test_another_awg_generation_refuses(services, rsv, monkeypatch):
    monkeypatch.setattr(services, "_awg_generation", lambda: 1)
    with pytest.raises(ServiceError, match="поколения 2"):
        services.reserve_accept_parse(Inviter(awg_gen=2).token)
    assert services.reserve_accept_parse(Inviter(awg_gen=1).token)["tok"], "одно поколение — отказано"


def test_no_own_gateway_is_a_warning_not_a_refusal(services, rsv):
    rsv.gw = False
    info = services.reserve_accept_parse("Пересылаю: " + Inviter().token + " — вот")
    assert info["warn"] and "шлюзов нет" in info["warn"][0], info["warn"]
    rsv.gw = True
    assert services.reserve_accept_parse(Inviter().token)["warn"] == []


def test_a_newer_inviter_is_a_question_not_a_refusal(services, rsv):
    assert services.reserve_accept_parse(Inviter(version="4.1.0").token)["ask_version"] is True
    assert services.reserve_accept_parse(Inviter(version="4.0.0.2").token)["ask_version"] is False, \
        "хотфикс — та же линия, вопроса нет"


# ── принявший: подключение и код ────────────────────────────────────────────

def test_connect_brings_the_link_up_on_the_invitation_keys(services, rsv):
    inv = Inviter()
    st = services.reserve_accept_go(inv.token, False)
    env = rsv.env("--join")
    assert (env["PAIR_ID"], env["PAIR_PRIV"], env["PAIR_PEER_PUB"], env["PAIR_ENDPOINT"]) == \
        (inv.pair_id, inv.peer_priv, inv.inviter_pub, f"{rc.HOST_B}:47101")
    assert all(env.get(k) == v for k, v in rc.OBF.items()), {k: env.get(k) for k in rc.OBF}
    assert (st["role"], st["state"]) == ("acceptor", "waiting") and services.reserve_link_role() == "acceptor"
    assert rsv.fw == [True], "карантин у принявшего не нарисован строго"
    assert settings.get_bool("app.reserve.rf_via_mine", True) is False, "выбор РФ-доступа из диалога не сохранён"
    assert services.reserve_channel_secret() == inv.peer_priv, "секрет канала до кода — не ключ из приглашения"
    assert services.reserve_connect_address() == ("10.99.98.2", 8788)


def test_connect_without_the_firewall_rolls_back(services, rsv):
    rsv.fw_fail = True
    with pytest.raises(ServiceError, match="Подключение не начато"):
        services.reserve_accept_go(Inviter().token, True)
    assert services.reserve_invite() == {} and rsv.modes[-1] == "--rollback"


def test_the_acceptor_hello_has_no_admin_id_and_passes_the_dictionary(services, rsv):
    services.reserve_accept_go(Inviter().token, True)
    body = services.reserve_accept_hello_body()
    assert "admin_id" not in body, "ADMIN_ID ушёл в hello — до кода"
    pairproto.check(pairproto.PAIR_PROTO, "hello", body, pairproto.ACCEPTOR)


def test_pair_eph_gives_the_acceptor_the_inviters_code(services, rsv):
    inv = Inviter()
    services.reserve_accept_go(inv.token, True)
    res = services.reserve_accept_eph_in(inv.eph_reply(services.reserve_accept_hello_body()))
    assert res["code"] == inv.code, "коды сторон разошлись"
    assert services.reserve_invite()["state"] == "connected"
    with pytest.raises(ServiceError, match="повреждён"):
        services.reserve_accept_eph_in({"eph": "AAAA"})


def _acceptor_at_code(services, inv: Inviter) -> None:
    services.reserve_accept_go(inv.token, True)
    services.reserve_accept_eph_in(inv.eph_reply(services.reserve_accept_hello_body()))


def test_pair_ok_puts_the_new_keys_writes_the_pair_and_answers_with_the_admin_id(services, rsv):
    inv = Inviter()
    _acceptor_at_code(services, inv)
    ok = inv.ok_body()
    reply = services.reserve_accept_ok_in({"sealed": ok["sealed"]})
    # до ухода pair_admin пары нет и линк на ключах приглашения: иначе ответ до пригласившего не дойдёт
    assert not services.reserve_paired() and "--set-peer" not in rsv.modes, \
        "линк переведён на новые ключи до отправки pair_admin"
    services.reserve_accept_commit()
    env = rsv.env("--set-peer")
    data = ok["_data"]
    assert (env["PAIR_PRIV"], env["PAIR_PEER_PUB"], env["PAIR_PSK"]) == (data["priv"], data["peer_pub"], data["psk"])
    assert env["H4"] == rc.OBF["H4"] and env["JC"] == rc.OBF["JC"]
    back = joinsas.unseal(inv.kp, reply["sealed"])
    assert back["admin_id"] == ADMIN and set(reply) == {"sealed"}, "pair_admin несёт что-то открытым текстом"
    peer = services.reserve_peer()
    assert (peer["listener"], peer["peer_admin_id"], peer["host"]) == (0, PEER_ADMIN, ADDR_B)
    assert services.reserve_link_role() == "acceptor" and services.reserve_keys()["m_gen"] == 1
    assert services.reserve_channel_secret() == data["psk"]


def test_pair_ok_without_a_field_or_under_another_key_writes_nothing(services, rsv):
    inv = Inviter()
    _acceptor_at_code(services, inv)
    data = inv.ok_body()["_data"]
    data.pop("admin_id")
    with pytest.raises(ServiceError, match="не того вида"):
        services.reserve_accept_ok_in({"sealed": joinsas.seal(inv.kp, data)})
    with pytest.raises(ValueError):
        services.reserve_accept_ok_in({"sealed": joinsas.seal(bytes(32), inv.ok_body()["_data"])})
    assert not services.reserve_paired() and "--set-peer" not in rsv.modes


# ── адреса канала ────────────────────────────────────────────────────────────

def test_the_channel_listens_on_the_inviters_address_in_the_30(services, rsv, monkeypatch):
    assert services.reserve_link_role() == "" and services.reserve_listen_address() is None
    services.reserve_invite_issue()
    assert services.reserve_link_role() == "inviter"
    assert services.reserve_listen_address() == ("10.99.98.2", 8788)
    settings.set_value("app.reserve.pair_channel_port", 9100)
    assert services.reserve_listen_address() == ("10.99.98.2", 9100)
    assert services.reserve_channel_secret() == services.reserve_invite()["peer_priv"]


def test_the_admin_id_is_never_in_the_session_bodies_before_the_code():
    """Сторож словаря: в видах до кода (`hello`, `pair_eph`, `pair_wrong`,
    `pair_refused`) поля admin_id нет — только `pair_ok`/`pair_admin` несут
    его, и только внутри `sealed`."""
    for kind in ("hello", "pair_eph", "pair_wrong", "pair_refused"):
        assert "admin_id" not in pairproto.PAIR_KINDS[kind][1], kind
    for kind in ("pair_ok", "pair_admin"):
        assert set(pairproto.PAIR_KINDS[kind][1]) == {"sealed"}, kind


def test_hello_bodies_carry_the_gateway_flag_both_ways(services, rsv):
    """Флаг шлюза (`gw`) едет в hello и pair_eph: по нему вторая сторона
    решает про «шлюзов нет ни у кого» и считает направление."""
    rsv.gw = False
    _res, _acc, out = rc.invite_and_hello(services, rsv)
    assert out["body"]["gw"] is False
    assert services.reserve_invite()["peer_gw"] is True


def test_pair_admin_that_did_not_go_rolls_the_invitation_back_and_writes_nothing(services, rsv):
    inv = Inviter()
    _acceptor_at_code(services, inv)
    services.reserve_accept_ok_in({"sealed": inv.ok_body()["sealed"]})
    services.reserve_accept_abort("link")
    assert not services.reserve_paired() and "--set-peer" not in rsv.modes
    assert not services.reserve_invite().get("id"), "приглашение живо после несостоявшегося ответа"
    with pytest.raises(ServiceError, match="Сопряжения нет"):
        services.reserve_accept_commit()
    assert "--set-peer" not in rsv.modes


def test_a_commit_without_a_checked_pair_ok_is_refused(services, rsv):
    inv = Inviter()
    _acceptor_at_code(services, inv)
    with pytest.raises(ServiceError, match="Сопряжения нет"):
        services.reserve_accept_commit()
    assert not services.reserve_paired() and "--set-peer" not in rsv.modes


def test_a_fresh_pair_is_not_confirmed_until_the_first_session(services, rsv):
    inv = Inviter()
    _acceptor_at_code(services, inv)
    services.reserve_accept_ok_in({"sealed": inv.ok_body()["sealed"]})
    services.reserve_accept_commit()
    ps = services.reserve_peer_state()
    assert ps.get("confirmed") is False and ps.get("linked_at"), ps
