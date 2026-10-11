"""
Токен бота шлюза на стороне ВПС: кому его можно довезти каналом
(gateway_token_slots), годность бота шлюзу (gateway_token_check: не бот
сервера, не бот другого шлюза; новый бот своего слота — можно) и раздел
token документа конфигурации (gwcfg_token, отпечаток, сверка со снимком).

Цена ошибки: раздел token агенту без token1 — агент прежней версии отвергнет
документ целиком (неизвестный раздел), и env с подсетями тоже не доедет;
бот сервера или бот другого шлюза, пропущенный проверкой, — два процесса
на одном боте перехватывают апдейты друг у друга, и один из ботов глохнет;
отказ новому боту своего слота — шлюз с удалённым ботом не поднять; раздел, который едет при совпавшем отпечатке, — бесконечная
доставка и перезапуск агента на каждом снимке.

Токены — в настоящем env-файле во временном каталоге (AWG_BOT_ENV), как
их пишет ввод в «🔧 Сервис»; остальное — фикстуры документа конфигурации.
"""
from __future__ import annotations

import pytest

from awgbot.domain import gwcfg
from awgbot.domain.services.types import ServiceError
from tests.integration import test_gwcfg_delivery as _d

pytestmark = pytest.mark.integration

host, slot = _d.host, _d.slot

OLD = "123456789:AA-dummy-token-for-tests"
NEW = "123456789:AA-new-dummy-token-tests"
OTHER = "987654321:BB-other-bot-token-dummy"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "env"
    monkeypatch.setenv("AWG_BOT_ENV", str(path))
    return path


def _caps(services, slot_id: int = 1, *caps) -> None:
    services.gwlink_session_opened(slot_id, "3.3.0", 2, list(caps or ("cfg1", "confirm1", "token1")))


# ── раздел token ─────────────────────────────────────────────────────────────

def test_no_token_section_for_an_agent_without_token1(services, slot, env):
    """Агент 3.3 без token1 раздела не понимает — не получает его, даже если
    токен на ВПС есть."""
    services.set_gw_bot_token(OLD, 1)
    _caps(services, 1, "cfg1", "confirm1")
    assert services.gwcfg_token(slot) is None
    assert "token" not in gwcfg.build(1, "ab" * 8, services.gwcfg_desired(slot))


def test_no_token_section_without_a_token_on_the_server(services, slot, env):
    """Слот заведён до вопроса о токене — на шлюзе своё, не трогаем."""
    _caps(services)
    assert services.gwcfg_token(slot) is None
    assert services.gwcfg_doc(slot)["prints"]["token"] == ""


def test_the_token_section_carries_the_server_token(services, slot, env):
    """token1 и токен, введённый кнопкой, — раздел с ним и его отпечаток в
    документе; в env ВПС — он же."""
    services.gateway_token_replace(1, OLD)
    assert services.gw_bot_token(1) == OLD
    _caps(services)
    assert services.gwcfg_token(slot) == {"value": OLD}
    doc = services.gwcfg_doc(slot)
    assert doc["prints"]["token"] == gwcfg.token_print(OLD)
    assert OLD not in str(doc["prints"]), "в отпечатках — сам токен"
    body = gwcfg.build(doc["gen"], doc["hash"], doc["sections"])
    assert body["token"] == {"value": OLD}, body.get("token")


def test_a_token_from_the_setup_without_the_mark_does_not_go(services, slot, env):
    """Токен лежит в env с настройки слота, кнопкой его не вводили — раздела
    нет: он мог устареть (меняли на шлюзе руками), и агент отвечал бы вечным
    «Telegram не принял»."""
    services.set_gw_bot_token(OLD, 1)
    _caps(services)
    assert services.gwcfg_token(slot) is None
    assert services.gwcfg_doc(slot)["prints"]["token"] == ""
    assert services.gateway_token_pushed(1, OLD) is False


def test_the_mark_follows_the_token_not_the_slot(services, slot, env):
    """Метка — отпечаток введённого токена: токен в env потом сменили иным
    путём (перевыпуск с настройкой) — метка с ним не совпала, раздела нет."""
    services.gateway_token_replace(1, OLD)
    _caps(services)
    assert services.gwcfg_token(slot) == {"value": OLD}
    services.set_gw_bot_token(NEW, 1)
    assert services.gateway_token_pushed(1, NEW) is False
    assert services.gwcfg_token(slot) is None, "токен без метки поехал шлюзу"


def test_the_mark_is_forgotten_with_the_slot(services, slot, env):
    """Слот сняли — метка уходит с ним: новый слот с тем же номером не
    повезёт токен, введённый для прежнего."""
    services.gateway_token_replace(1, OLD)
    services._slot_forget(1)
    assert services.db.get_state(services._gw_slot_key(services._TOKEN_PUSH_KEY, 1)) in ("", None)
    services.set_gw_bot_token(OLD, 1)
    assert services.gateway_token_pushed(1, OLD) is False


def test_a_token_that_would_not_pass_the_agent_check_does_not_go(services, slot, env):
    """В env ВПС руками вписали мусор — раздела нет (агент отверг бы весь
    документ), а не исключение на расчёте."""
    env.write_text("GW_BOT_TOKEN=not-a-token\n", encoding="utf-8")
    services.db.set_state(services._gw_slot_key(services._TOKEN_PUSH_KEY, 1), gwcfg.token_print("not-a-token"))
    _caps(services)
    assert services.gwcfg_token(slot) is None
    assert services.gwcfg_doc(slot)["prints"]["token"] == ""


def test_a_matching_token_print_brings_nothing_a_new_token_brings_the_document(services, slot, env):
    """Снимок с отпечатком того же токена — документ не едет; токен на ВПС
    сменили — расхождение только в token, документ с новым токеном."""
    services.gateway_token_replace(1, OLD)
    _caps(services)
    _d._snap(services, _d._have(services))
    assert services.gwcfg_due(slot) is None, "токен совпадает, а документ едет"
    services.gateway_token_replace(1, NEW)
    doc = services.gwcfg_doc(slot)
    assert services.gwcfg_diff(slot, doc, _d._have(services, token=gwcfg.token_print(OLD))) == ["token"]
    _d._snap(services, _d._have(services, token=gwcfg.token_print(OLD)))
    body = services.gwcfg_due(slot)
    assert body is not None and body["token"] == {"value": NEW}, body


# ── кому довезти ─────────────────────────────────────────────────────────────

def test_token_slots_are_only_agents_with_token1(services, slot, env):
    assert services.gateway_token_slots() == []
    _caps(services, 1, "cfg1", "confirm1")
    assert services.gateway_token_slots() == [], "агенту без token1 токен не довезти"
    _caps(services)
    services.gateway_set_label(1, "север")
    assert services.gateway_token_slots() == [(1, "NASPi (север)")]


# ── годность бота шлюзу ──────────────────────────────────────────────────────

SERVER = "111111111:AA-server-dummy-token"


@pytest.fixture()
def server_bot(monkeypatch):
    from awgbot.core import config
    monkeypatch.setattr(config, "BOT_TOKEN", SERVER)


def _second(services) -> None:
    """Второй слот (Pi2) — со своим ботом."""
    admin = services.db.get_client_by_tg(_d.ADMIN)
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)


def test_the_same_bot_is_not_a_new_bot(services, slot, env, server_bot):
    """Прежний токен слота на ВПС, новый токен того же бота — годится, без
    строки про «Старт»."""
    services.set_gw_bot_token(OLD, 1)
    got = services.gateway_token_check(1, NEW, 123456789, "naspi_gw_bot")
    assert got == {"new_bot": False, "username": "naspi_gw_bot"}, got


def test_a_new_bot_for_the_own_slot_is_taken(services, slot, env, server_bot):
    """Прежнего бота удалили — токен нового принимается (вся настройка шлюза
    остаётся), new_bot=True и его имя: человеку нажать у него «Старт»."""
    services.set_gw_bot_token(OLD, 1)
    got = services.gateway_token_check(1, OTHER, 987654321, "other_gw_bot")
    assert got == {"new_bot": True, "username": "other_gw_bot"}, got


def test_without_the_old_token_the_name_tells_a_new_bot(services, slot, env, server_bot):
    """Токена слота на ВПС нет — новый ли бот, решает имя из снимка канала
    (без учёта регистра)."""
    services.gwlink_snapshot_in(1, {"agent_bot": {"username": "NASPi_gw_bot", "name": "NASPi"}, "rev": 1}, 1, True)
    assert services.gateway_token_check(1, NEW, 123456789, "naspi_gw_bot")["new_bot"] is False
    assert services.gateway_token_check(1, OTHER, 987654321, "other_gw_bot")["new_bot"] is True


def test_nothing_to_compare_with_is_not_a_refusal(services, slot, env, server_bot):
    """Ни прежнего токена, ни имени бота — токен живой, бот ничей: годится;
    «новый бот» не утверждаем — не с чем сравнить."""
    got = services.gateway_token_check(1, NEW, 123456789, "naspi_gw_bot")
    assert got == {"new_bot": False, "username": "naspi_gw_bot"}, got


def test_getme_must_name_the_bot_of_the_token(services, slot, env, server_bot):
    """getMe назвал не тот id, что в токене, — отказ до любых сверок."""
    services.set_gw_bot_token(OLD, 1)
    with pytest.raises(ServiceError, match="Telegram назвал другого бота, чем в токене"):
        services.gateway_token_check(1, NEW, 555555555, "x_bot")


def test_the_server_bot_is_refused(services, slot, env, server_bot):
    """Токен бота сервера AWG — отказ: два процесса на одном боте."""
    with pytest.raises(ServiceError, match="это токен бота сервера AWG — шлюзу нужен свой бот"):
        services.gateway_token_check(1, SERVER, 111111111, "awg_srv_bot")


def test_a_bot_of_another_gateway_is_refused_by_its_token(services, slot, env, server_bot):
    """id совпал с токеном другого слота в env ВПС — отказ с именем того шлюза."""
    _second(services)
    services.set_gw_bot_token(OTHER, 2)
    services.gateway_set_label(2, "север")
    with pytest.raises(ServiceError, match=r"этот бот уже у шлюза Pi2 \(север\) — нужен свой бот для каждого шлюза"):
        services.gateway_token_check(1, "987654321:BB-another-dummy-token", 987654321, "other_gw_bot")


def test_a_bot_of_another_gateway_is_refused_by_its_name(services, slot, env, server_bot):
    """Токена другого слота на ВПС нет, но его бот известен по снимку —
    совпадение имени (без учёта регистра) — тоже отказ."""
    _second(services)
    services.gwlink_snapshot_in(2, {"agent_bot": {"username": "Other_GW_bot", "name": "Pi2"}, "rev": 1}, 1, True)
    with pytest.raises(ServiceError, match="этот бот уже у шлюза Pi2"):
        services.gateway_token_check(1, OTHER, 987654321, "other_gw_bot")


def test_the_own_slot_is_not_another_gateway(services, slot, env, server_bot):
    """Свой слот в сверке «занят другим шлюзом» не участвует: тот же бот и
    тот же токен — годятся."""
    _second(services)
    services.set_gw_bot_token(OLD, 1)
    services.set_gw_bot_identity(1, "naspi_gw_bot", "NASPi")
    assert services.gateway_token_check(1, OLD, 123456789, "naspi_gw_bot")["new_bot"] is False


# ── отказ шлюза: уведомление ─────────────────────────────────────────────────

def _ack(services, failed=None, retry=False, digest="ab" * 8) -> dict:
    body = {"gen": 3, "hash": digest, "applied": ["env"], "wait": {}, "declined": {}, "failed": failed or {}}
    if retry:
        body["retry"] = True
    return body


def test_a_refused_token_notifies_once_per_document(services, slot, env):
    """Шлюз не принял токен (другой бот, Telegram не принял) — админу одно
    уведомление с именем шлюза и причиной (экранированной); тот же ответ
    повторно — тишина; новый документ с тем же отказом — снова одно."""
    _, notes = services.gwcfg_ack_take(1, _ack(services, {"token": "Telegram не принял <новый> токен"}))
    texts = [n.text for n in notes]
    assert len(texts) == 1 and texts[0].startswith("⚠️ ") and "новый токен бота не применён" in texts[0], texts
    assert "NASPi" in texts[0] and "&lt;новый&gt;" in texts[0], texts[0]
    _, again = services.gwcfg_ack_take(1, _ack(services, {"token": "Telegram не принял <новый> токен"}))
    assert again == [], "тот же отказ — второе уведомление"
    _, other = services.gwcfg_ack_take(1, _ack(services, {"token": "токен другого бота — не применяю"},
                                                 digest="cd" * 8))
    assert len(other) == 1 and "другого бота" in other[0].text, [n.text for n in other]


def test_a_temporary_token_refusal_is_silent(services, slot, env):
    """retry (Telegram шлюзу не ответил) — сервер повторит сам, админу нечего
    сообщать; и метку «сказали» не ставит: настоящий отказ потом прозвучит."""
    _, notes = services.gwcfg_ack_take(1, _ack(services, {"token": "Telegram не ответил"}, retry=True))
    assert notes == []
    _, notes = services.gwcfg_ack_take(1, _ack(services, {"token": "Telegram не принял новый токен"}))
    assert len(notes) == 1, "после retry настоящий отказ того же документа промолчал"


def test_no_token_note_when_the_token_went_in(services, slot, env):
    _, notes = services.gwcfg_ack_take(1, {**_ack(services), "applied": ["env", "token"]})
    assert notes == []
