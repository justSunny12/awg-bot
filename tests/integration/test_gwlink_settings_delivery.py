"""
Настройки по каналу линка — доставка (концепт «канал линка», этап 2, §3.1,
§4.5, §4.7, §9.2): сервер решает, что слать, по расхождению снимка с
выдаваемым; один набор — одна отправка за сессию; новая сессия — новая
попытка; итог применения возвращается `ack` и ложится в карточку.

Сокеты настоящие (петля), сервер — боевой LinkServer поверх временной БД. В
сквозных сценариях с той стороны работает боевой LinkClient и настоящее
`GatewayServices.apply_link_settings` поверх юнита во временном файле (класс
`_Unit` из юнит-тестов применения) — подменён только systemctl.

Цена ошибки: сервер, который шлёт настройки в каждом такте, перезапускает
обвязку малины (dnsmasq, файервол) без конца; сервер, который не шлёт, молча
оставляет квартиру со старыми подсетями, пока бот уверяет, что «доставлено».

Слот здесь без режима «за шлюзом — без VPN»: фиды локальной сети — отдельный
этап и отдельные тесты, этим сценариям они только мешали бы.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import os
import subprocess
import time

import pytest

from awgbot.core import config
from awgbot.domain import gwsnapshot
from awgbot.domain.gateway import GatewayServices
from awgbot.infra.db import Database
from awgbot.runtime import linkclient, linkserver
from awgbot.util import gwlink, timeutil
from tests.integration.test_gwlink_channel import _Gw, _free_port, _until
from tests.unit.test_gwlink_settings_apply import UNIT_TEXT, _cp, _Unit

pytestmark = pytest.mark.integration

ADMIN = config.ADMIN_ID
PRIV = base64.b64encode(os.urandom(32)).decode()
KEY = gwlink.channel_key(PRIV)
FIELDS = {"ADMIN_IPS": "admin_ips", "HOME_SUBNETS": "home_subnets", "LAN_MODE": "lan_mode",
          "PEER_HOME_NETS": "peer_home_nets", "RESOLVER": "resolver"}


def _installed(services) -> dict:
    """Блок `bundle` снимка, когда на шлюзе стоит ровно выдаваемое."""
    want = services.gwlink_settings_want(services.db.gateway(1))
    return {FIELDS[k]: v for k, v in want.items()}


def _snap(bundle: dict, **kw) -> dict:
    snap = {"bundle": bundle, "link_contract": "1", "plumbing_gen": "new",
            "mark_status": "confirmed", "agent_version": "3.2.0", "awg_generation": 1,
            "egress_ok": True, "boot_id": "b" * 36, "rev": 1}
    snap.update(kw)
    return snap


@pytest.fixture()
async def link(services, make_active_client, monkeypatch):
    """Слушатель канала на петле и слот с локальной подсетью."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "127.0.0.0/30", slot_id=1)
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    monkeypatch.setattr(services, "_link_privkey", lambda g=None: PRIV)
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    port = _free_port()
    monkeypatch.setattr(linkserver, "channel_port", lambda: port)
    monkeypatch.setattr(linkserver, "gw_address", lambda cidr: "127.0.0.1")
    srv = linkserver.LinkServer(services)
    await srv.ensure()
    assert srv._bound, "слушатель канала не поднялся на адресе линка"
    yield srv
    await srv.stop()


async def _connect(srv, bundle: dict) -> _Gw:
    """Шлюз подключился и прислал снимок с тем, что у него стоит."""
    host, port = srv._bound[0]
    gw = _Gw(*await asyncio.open_connection(host, port), key=KEY)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.2.0"})
    await gw.send("snap", _snap(bundle))
    return gw


async def _settings(gw: _Gw, timeout: float = 2.0) -> dict | None:
    """Следующее сообщение `settings` с сервера или None, если за время его не
    было. Прочие виды сообщений (роль слота, фиды — свои этапы и свои тесты)
    пропускаются: здесь проверяется только доставка настроек."""
    end = asyncio.get_running_loop().time() + timeout
    while True:
        left = end - asyncio.get_running_loop().time()
        if left <= 0:
            return None
        try:
            line = await asyncio.wait_for(gw.reader.readline(), timeout=left)
        except asyncio.TimeoutError:
            return None
        if not line:
            return None
        msg = gwlink.unpack(gw.key, line)
        if msg.get("t") == "settings":
            return msg


def _drifted(services) -> dict:
    return {**_installed(services), "home_subnets": "192.168.1.0/24"}


# ── что решает сервер ────────────────────────────────────────────────────────

async def test_a_gateway_that_already_has_everything_hears_nothing(services, link):
    """Миграция (§9.2): канал включили на системе, где всё уже совпадает.
    Настроек не приходит ни сразу, ни тактом живости — «на всякий случай» не
    шлём: шлюз не перезапускает обвязку без причины."""
    gw = await _connect(link, _installed(services))
    await _until(lambda: services.gwlink_snapshot(1))
    assert await _settings(gw, 0.4) is None, "сервер прислал настройки шлюзу, у которого всё совпадает"
    await link.deliver_all()
    assert await _settings(gw, 0.3) is None, "такт живости прислал настройки без расхождения"
    await gw.close()


async def test_a_drifted_gateway_gets_the_settings_right_after_its_snapshot(services, link):
    """Снимок показал, что на шлюзе стоит не то, — это и есть повод доставить.
    Уходит ровно закрытый список ключей со значениями, из которых собирается
    бандл, и ничего больше: ни ключей линка, ни аплинка, ни токена."""
    gw = await _connect(link, _drifted(services))
    msg = await _settings(gw)
    assert msg is not None, "расхождение есть, а настройки не пришли"
    assert msg["values"] == services.gwlink_settings_want(services.db.gateway(1))
    assert msg["values"]["HOME_SUBNETS"] == "192.168.68.0/24"
    assert set(msg["values"]) == set(gwlink.SETTINGS_KEYS), "в настройках ключ вне списка"
    assert set(msg) == {"t", "seq", "ts", "values"}, f"в сообщении лишние поля: {sorted(msg)}"
    assert PRIV not in json.dumps(msg), "приватный ключ линка уехал в канал"
    assert await _settings(gw, 0.3) is None, "настройки пришли дважды"
    await gw.close()


async def test_the_same_set_is_not_sent_twice_in_one_session(services, link):
    """Шлюз не применил (или ещё применяет) — сервер не долбит его тем же набором
    каждым тактом живости: иначе неудачное применение перезапускало бы обвязку
    малины раз в минуту."""
    gw = await _connect(link, _drifted(services))
    assert await _settings(gw) is not None
    await gw.send("ack", {"ok": False, "changed": ["HOME_SUBNETS"], "error": "exit 1", "hash": ""})
    await gw.send("delta", {"egress_ok": False, "rev": 2})
    await _until(lambda: services.gwlink_snapshot(1)["egress_ok"] is False)
    await link.deliver_all()
    await link.deliver_all()
    assert await _settings(gw, 0.4) is None, "тот же набор ушёл в сессии повторно"
    await gw.close()


async def test_a_new_edit_on_the_server_goes_out_in_the_same_session(services, link):
    """Другое дело — человек поменял настройку на ВПС ещё раз: это новый набор,
    и такт живости обязан его отвезти, не дожидаясь переподключения."""
    gw = await _connect(link, _drifted(services))
    assert (await _settings(gw))["values"]["HOME_SUBNETS"] == "192.168.68.0/24"
    services.gateway_set_home_subnets(1, "192.168.70.0/24")
    await link.deliver_all()
    again = await _settings(gw)
    assert again is not None and again["values"]["HOME_SUBNETS"] == "192.168.70.0/24"
    await gw.close()


async def test_a_new_session_is_a_new_attempt(services, link):
    """Малина перезагрузилась и переподключилась со старыми значениями. Память
    «уже слали» принадлежит сессии — иначе расхождение осталось бы навсегда."""
    gw = await _connect(link, _drifted(services))
    assert await _settings(gw) is not None
    await gw.close()
    await _until(lambda: not services.gwlink_session(1))
    gw2 = await _connect(link, _drifted(services))
    assert await _settings(gw2) is not None, "после переподключения настройки не пришли"
    await gw2.close()


async def test_changes_made_while_the_channel_is_down_arrive_when_it_comes_back(services, link):
    """§4.5: канал лежит — ничего не копится очередью и никуда не шлётся.
    Поднялся — последнее желаемое уходит одним сообщением. Пока канал молчит
    меньше суток, бот не гонит человека перевыпускать файл."""
    gw = await _connect(link, _installed(services))
    await _until(lambda: services.gwlink_snapshot(1))
    await gw.close()
    await _until(lambda: not services.gwlink_session(1))

    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1),
                          services._gw_bundle_deps(services.db.gateway(1)))
    services.gateway_set_home_subnets(1, "192.168.70.0/24 192.168.71.0/24")
    assert await link.deliver(1) is False, "без сессии доставлять некому"
    await link.deliver_all()
    assert services.gw_bundle_drift_notes() == [], "канал лёг минуту назад, а бот уже гонит перевыпускать"

    # малина вернулась с тем, что стояло до правки
    gw2 = await _connect(link, {**_installed(services), "home_subnets": "192.168.68.0/24"})
    msg = await _settings(gw2)
    assert msg is not None, "канал поднялся, а отложенное не доехало"
    assert msg["values"]["HOME_SUBNETS"] == "192.168.70.0/24 192.168.71.0/24"
    await gw2.close()


async def test_the_ack_lands_on_the_card_and_leaves_with_the_slot(services, link, monkeypatch):
    """Итог применения — то, ради чего человек открывает карточку. Снятие слота
    уносит его вместе с прочими ключами канала: чужой отказ не должен
    проступить у следующего шлюза."""
    gw = await _connect(link, _drifted(services))
    assert await _settings(gw) is not None
    await gw.send("ack", {"ok": False, "error": "LAN-интерфейс не найден\nstack", "hash": ""})
    await _until(lambda: services.gwlink_ack(1))
    ack = services.gwlink_card(services.db.gateway(1), 10)["ack"]
    assert ack["ok"] is False and ack["error"] == "LAN-интерфейс не найден stack", ack
    await gw.send("ack", {"ok": True, "error": "", "hash": "x"})
    await _until(lambda: services.gwlink_ack(1).get("ok") is True)
    await gw.close()

    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    services.gateway_remove(1)
    assert not services.db.get_state("gwlink_ack_1"), "итог применения пережил снятие слота"


# ── что сервер хочет и когда ─────────────────────────────────────────────────

def test_the_wanted_set_is_pure_and_passes_the_gateways_own_check(services, link):
    """Желаемое считается из тех же полей, что бандл, и ничего не пишет в БД.
    И главное — агент обязан его принять: сервер, который хочет то, что шлюз
    отвергнет, получил бы вечный отказ вместо доставки."""
    gw = services.db.gateway(1)
    commits = services.db.commits
    want = services.gwlink_settings_want(gw)
    assert services.db.commits == commits, "расчёт желаемого что-то записал в БД"
    assert services.gwlink_settings_want(gw) == want, "два расчёта подряд дали разное"
    assert tuple(want) == gwlink.SETTINGS_KEYS
    assert gwlink.validate_settings(want) == want, "шлюз отверг бы то, что хочет сервер"
    assert PRIV not in json.dumps(want)


def test_nothing_is_due_without_a_snapshot_or_its_configuration_block(services, link):
    """Судить не по чему — не шлём: вслепую значит перезапускать обвязку без
    нужды на каждом подключении старого агента."""
    gw = services.db.gateway(1)
    assert services.gwlink_settings_due(gw) is None
    services.gwlink_snapshot_in(1, {"agent_version": "3.1.0", "rev": 1}, 1, True)
    assert services.gwlink_settings_due(gw) is None
    services.gwlink_snapshot_in(1, _snap(_installed(services)), 1, True)
    assert services.gwlink_settings_due(gw) is None
    services.gwlink_snapshot_in(1, _snap(_drifted(services)), 1, True)
    assert services.gwlink_settings_due(gw) == services.gwlink_settings_want(gw)


# ── время отправки снимка и часы малины ─────────────────────────────────────

async def test_clock_skew_is_measured_from_the_send_time_on_the_wire(services, link):
    """Время снимка приезжает в конверте unix-числом. Храни его как есть — сверка
    часов никогда бы не срабатывала, и канал однажды замолчал бы по окну
    времени без единой подсказки на экране. Здесь часы малины на 200 с впереди."""
    host, port = link._bound[0]
    gw = _Gw(*await asyncio.open_connection(host, port), key=KEY)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.2.0"})
    gw.seq += 1
    await gw.raw(gwlink.pack(KEY, "snap", _snap(_installed(services)), seq=gw.seq,
                             now=time.time() + 200))
    await _until(lambda: services.gwlink_snapshot(1))
    skew = services.gwlink_card(services.db.gateway(1), 10)["clock_skew"]
    assert skew is not None and 190 <= skew <= 210, f"сверка часов: {skew}"
    stored = services.db.get_state("gwlink_snap_ts_1")
    assert timeutil.parse_iso(stored), f"время снимка хранится не как время: {stored!r}"
    await gw.close()


def test_the_send_time_is_stored_as_a_moment_whatever_arrives(services):
    now = int(time.time())
    iso = services._gwlink_sent_at(now)
    assert abs((timeutil.parse_iso(iso) - timeutil.now()).total_seconds()) < 5
    assert services._gwlink_sent_at(str(now)) == iso
    assert services._gwlink_sent_at("2026-09-22T20:00:00+03:00") == "2026-09-22T20:00:00+03:00"
    assert services._gwlink_sent_at("9" * 30) == "", "число за пределами календаря уронило приём"
    assert services._gwlink_sent_at(None) == ""
    assert len(services._gwlink_sent_at("x" * 500)) <= 40


# ── оба конца сразу: настоящий клиент и настоящее применение ────────────────

class _Pi(GatewayServices):
    """Агент шлюза: всё настоящее, кроме того, что снимает тик монитора."""

    def gw_snapshot(self) -> dict:
        return gwsnapshot.collect(mark_status="confirmed", egress_ok=True, guard_info=None,
                                  peer_nets=None)

    def gateway_claim_if_needed(self):
        return None


@pytest.fixture()
async def pi(services, link, tmp_path, monkeypatch):
    """Малина на том конце: юнит с теми значениями, что сейчас выдаёт ВПС,
    боевой клиент канала, чат агента — списком."""
    want = services.gwlink_settings_want(services.db.gateway(1))
    text = (UNIT_TEXT.replace("Environment=LAN_MODE=1", f"Environment=LAN_MODE={want['LAN_MODE']}")
            .replace("Environment=RESOLVER=10.9.1.1", f"Environment=RESOLVER={want['RESOLVER']}")
            .replace('"ADMIN_IPS=10.8.1.2"', f'"ADMIN_IPS={want["ADMIN_IPS"]}"')
            .replace('"HOME_SUBNETS=192.168.68.0/24"', f'"HOME_SUBNETS={want["HOME_SUBNETS"]}"'))
    unit = _Unit(tmp_path, monkeypatch, text=text)
    conf = tmp_path / "awglink.conf"
    conf.write_text(f"[Interface]\nPrivateKey = {PRIV}\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(conf))
    host, port = link._bound[0]
    monkeypatch.setattr(linkclient, "server_address", lambda: host)
    monkeypatch.setattr(linkclient, "server_port", lambda: port)
    chat: list[str] = []

    async def _notify(text: str) -> None:
        chat.append(text)
    monkeypatch.setattr(linkclient, "_notify", _notify)
    pidb = Database(str(tmp_path / "pi.db"))
    pidb.init_schema()
    client = linkclient.LinkClient(_Pi(pidb))
    client.start()
    await _until(lambda: services.gwlink_snapshot(1) and client._writer is not None)
    assert services.gwlink_config_drift(services.db.gateway(1)) == [], (
        "стартовый юнит малины разошёлся с выдаваемым — сценарий начат не с того")
    try:
        yield {"unit": unit, "client": client, "chat": chat}
    finally:
        await client.stop()
        pidb.close()


async def test_a_subnet_edit_on_the_server_is_delivered_applied_and_confirmed(services, link, pi):
    """Вся польза этапа одним сценарием: человек поменял подсети на ВПС — через
    секунды они в юните малины, обвязка перезапущена один раз, агент сказал об
    этом в своём чате, сервер получил «ок» и новый снимок, расхождения нет, и
    напоминания перевыпустить файл нет. Сессия канала при этом та же самая —
    применение не рвёт канал, который его принёс."""
    unit, client, chat = pi["unit"], pi["client"], pi["chat"]
    assert unit.restarts == 0, "первое подключение на неизменённой системе перезапустило обвязку"
    since = services.gwlink_session(1)["since"]
    writer = client._writer
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1),
                          services._gw_bundle_deps(services.db.gateway(1)))

    services.gateway_set_home_subnets(1, "192.168.70.0/24")
    await link.deliver_all()
    await _until(lambda: services.gwlink_snapshot(1)["bundle"]["home_subnets"] == "192.168.70.0/24")

    assert unit.env()["HOME_SUBNETS"] == "192.168.70.0/24", "юнит малины не переписан"
    assert unit.restarts == 1, f"обвязку перезапустили {unit.restarts} раз"
    await _until(lambda: services.gwlink_ack(1))
    assert services.gwlink_ack(1)["ok"] is True
    assert services.gwlink_config_drift(services.db.gateway(1)) == []
    assert services.gw_bundle_drift_notes() == [], "доставлено каналом, а бот всё равно гонит перевыпускать"
    assert chat == ["⚙️ Сервер прислал новые настройки шлюза — применены: локальные подсети."]
    assert client._writer is writer and services.gwlink_session(1)["since"] == since, (
        "применение настроек порвало сессию канала")
    assert link.online(1) is True

    # такт живости после доставки — ни повторной отправки, ни второго рестарта
    await link.deliver_all()
    await asyncio.sleep(0.3)
    assert unit.restarts == 1 and len(chat) == 1


async def test_a_refused_delivery_rolls_back_and_says_so_on_both_ends(services, link, pi):
    """Скрипт обвязки новых значений не принял. Малина возвращает прежний юнит,
    человек в чате агента узнаёт, что не применилось и почему; карточка на ВПС
    показывает отказ; тот же набор в этой сессии больше не шлётся — обвязка не
    перезапускается по кругу."""
    unit, client, chat = pi["unit"], pi["client"], pi["chat"]
    unit.restart = [_cp(1, err=b"LAN-interface for 192.168.70.0/24 not found"), _cp(0)]
    services.gateway_set_home_subnets(1, "192.168.70.0/24")
    await link.deliver_all()
    await _until(lambda: services.gwlink_ack(1))

    ack = services.gwlink_ack(1)
    assert ack["ok"] is False and "not found" in ack["error"]
    assert unit.env()["HOME_SUBNETS"] == "192.168.68.0/24", "после отказа юнит остался с новыми значениями"
    assert unit.restarts == 2
    await _until(lambda: chat)
    assert len(chat) == 1 and "не применились" in chat[0] and "локальные подсети" in chat[0]
    await link.deliver_all()
    await asyncio.sleep(0.3)
    assert unit.restarts == 2, "отвергнутый набор слали снова в той же сессии"
    assert client._writer is not None and link.online(1)


async def test_a_delivery_that_changes_nothing_is_answered_quietly(services, link, pi):
    """Сервер прислал ровно то, что уже стоит (второй ВПС-процесс, гонка с
    бандлом). Ответ «ок, ничего не изменилось» — и тишина: ни рестарта, ни
    сообщения человеку, ни лишней дельты по каналу."""
    unit, client, chat = pi["unit"], pi["client"], pi["chat"]
    rev = client._rev
    want = services.gwlink_settings_want(services.db.gateway(1))
    assert await link.send(1, "settings", {"values": want})
    await _until(lambda: services.gwlink_ack(1))
    assert services.gwlink_ack(1)["ok"] is True
    await asyncio.sleep(0.2)
    assert unit.restarts == 0 and chat == [], chat
    assert client._rev == rev, "после пустого применения по каналу ушла дельта"


async def test_a_compromised_server_cannot_smuggle_a_line_into_the_unit(services, link, pi):
    """ВПС может быть взломан. Перевод строки со своей ExecStart в значении —
    отказ всего сообщения: юнит нетронут, обвязка не перезапущена, сессия жива,
    человеку в чате сказано, что сервер прислал то, что не применилось."""
    unit, client, chat = pi["unit"], pi["client"], pi["chat"]
    before = unit.text()
    want = services.gwlink_settings_want(services.db.gateway(1))
    evil = {**want, "HOME_SUBNETS": "192.168.68.0/24\"\nExecStartPre=/bin/sh -c 'id'"}
    assert await link.send(1, "settings", {"values": evil})
    await _until(lambda: services.gwlink_ack(1))
    assert services.gwlink_ack(1)["ok"] is False
    assert unit.text() == before and unit.restarts == 0
    await _until(lambda: chat)
    assert chat[0].startswith("⚠️")
    assert client._writer is not None and link.online(1), "мусор с сервера порвал сессию"


async def test_a_hanging_restart_does_not_break_the_channel(services, link, pi):
    """Скрипт обвязки завис дольше таймаута. Канал обязан пережить это: шлюз
    откатывает юнит и отвечает отказом. Если вместо этого исключение уронит
    сессию, новая сессия пришлёт тот же набор, малина увидит его в юните (запись
    прошла, отката не было) и ответит «ок» — отказ превратится в ложный успех."""
    unit, client = pi["unit"], pi["client"]
    writer = client._writer
    unit.restart = [subprocess.TimeoutExpired(["systemctl", "restart"], 90), _cp(0)]
    services.gateway_set_home_subnets(1, "192.168.70.0/24")
    await link.deliver_all()
    await _until(lambda: services.gwlink_ack(1) or client._writer is not writer)
    assert client._writer is writer, "зависший рестарт обвязки порвал сессию канала"
    assert services.gwlink_ack(1).get("ok") is False
    assert unit.env()["HOME_SUBNETS"] == "192.168.68.0/24"


def test_the_reminder_returns_after_a_day_of_silence(services, link):
    """Канал молчит больше суток — ждать его больше нечего: одно тихое
    напоминание перевыпустить файл (§4.5)."""
    gw = services.db.gateway(1)
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1),
                          services._gw_bundle_deps(gw))
    services.gwlink_snapshot_in(1, _snap(_installed(services)), 1, True)
    services.gateway_set_home_subnets(1, "192.168.70.0/24")
    services.db.set_state("gwlink_seen_1", timeutil.to_iso(timeutil.now() - datetime.timedelta(hours=25)))
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "Перевыпусти" in notes[0].text
    assert services.gw_bundle_drift_notes() == []
