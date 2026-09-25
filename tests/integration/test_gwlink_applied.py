"""
Итог применения файла конфигурации по каналу линка: шлюз применил файл из
своего чата (или не смог) — вид `applied` {ok, error} уходит на ВПС, сервер
кладёт его в state слота и зовёт крючок `linkserver.set_on_applied` (в бою
при успехе он убирает файл из чата админа и ставит карточку слота).

Сокеты настоящие (петля), сервер — боевой LinkServer поверх временной БД
(сцена `link` из test_gwlink_channel); на том конце то подписывающий руками
`_Gw`, то боевой LinkClient поверх настоящего `GatewayServices`.

Цена ошибки: итог не дошёл — файл с ключом линка висит в чате ВПС без
ответа; итог дошёл дважды — повторная карточка поверх того, что человек
делает; упавший крючок рвёт сессию — канал в петле
переподключений из-за строки в чате.
"""
from __future__ import annotations

import asyncio

import pytest

from awgbot.core import config
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from awgbot.runtime import linkclient, linkserver
from tests.integration import test_gwlink_channel as _chan
from tests.integration.test_gwlink_channel import SNAP, _connect, _until

pytestmark = pytest.mark.integration

# один слот на петле, ключ линка слота подставлен — та же сцена, что у канала
link = _chan.link


@pytest.fixture()
def hook(monkeypatch):
    """Крючок сервера, записывающий вызовы. Модульный: без отката остался бы
    на следующие тесты."""
    monkeypatch.setattr(linkserver, "_on_applied", None)
    calls = _Calls()

    async def on_applied(slot_id, ok, error, fp=""):
        calls.append((slot_id, ok, error))
        calls.fps.append(fp)
    linkserver.set_on_applied(on_applied)
    return calls


class _Calls(list):
    """Вызовы крючка (слот, успех, причина); отпечатки — рядом, в `fps`."""

    def __init__(self):
        super().__init__()
        self.fps: list[str] = []


def _applied_state(services, slot: int = 1) -> str:
    return services.db.get_state(services._gwlink_key(services._GWLINK_APPLIED_KEY, slot)) or ""


async def _hello(srv) -> _chan._Gw:
    gw = await _connect(srv)
    await gw.send("hello", {"proto": _chan.gwlink.PROTO, "agent": "3.1.0"})
    assert await gw.role(), "сервер не ответил ролью на hello"
    return gw


async def _next(gw, kind: str, timeout: float = 2.0) -> dict | None:
    """Следующее сообщение сервера вида `kind` или None; прочие пропускаются."""
    end = asyncio.get_running_loop().time() + timeout
    while (left := end - asyncio.get_running_loop().time()) > 0:
        msg = await gw.next_msg(left)
        if msg is None:
            return None
        if msg.get("t") == kind:
            return msg
    return None


FP, AT = "0123456789abcdef", "2026-09-25T12:00:00+03:00"


# ── сервер: приём `applied` ──────────────────────────────────────────────────

async def test_every_report_is_acknowledged_and_a_repeat_is_shown_once(link, services, hook):
    """На каждый `applied` сервер отвечает `applied_ack` с теми же отпечатком
    и временем — по нему агент чистит очередь. Тот же итог повторно (агент
    не дождался ответа в умершей сессии) подтверждается снова, но админу
    второе «применено» не приходит; итог с другим временем — новый."""
    gw = await _hello(link)
    await gw.send("applied", {"ok": True, "error": "", "fp": FP, "at": AT})
    ack = await _next(gw, "applied_ack")
    assert ack is not None, "сервер не подтвердил итог — агент будет слать его вечно"
    assert (ack["fp"], ack["at"]) == (FP, AT), ack
    assert hook == [(1, True, "")] and hook.fps == [FP], (hook, hook.fps)
    await gw.send("applied", {"ok": True, "error": "", "fp": FP, "at": AT})
    ack = await _next(gw, "applied_ack")
    assert ack is not None and (ack["fp"], ack["at"]) == (FP, AT), f"повтор не подтверждён: {ack}"
    await asyncio.sleep(0.1)
    assert hook == [(1, True, "")], f"повтор того же итога показан админу второй раз: {hook}"
    later = "2026-09-25T12:05:00+03:00"
    await gw.send("applied", {"ok": False, "error": "нет", "fp": FP, "at": later})
    assert (await _next(gw, "applied_ack"))["at"] == later
    assert await _until(lambda: len(hook) == 2), "новое применение того же файла принято за повтор"
    assert hook[-1] == (1, False, "нет")
    await gw.close()


async def test_a_report_from_an_old_agent_without_fingerprint_is_shown(link, services, hook):
    """Агент до отпечатков шлёт {ok, error}: итог всё равно показан (крючок
    с пустым отпечатком — файл убирается по старому правилу) и подтверждён."""
    gw = await _hello(link)
    await gw.send("applied", {"ok": True, "error": ""})
    ack = await _next(gw, "applied_ack")
    assert ack is not None and (ack["fp"], ack["at"]) == ("", ""), ack
    assert await _until(lambda: hook), "итог старого агента не показан"
    assert hook == [(1, True, "")] and hook.fps == [""], (hook, hook.fps)
    await gw.close()



async def test_a_success_report_is_stored_and_shown_once(link, services, hook):
    """Главный путь: шлюз сказал «применено» — крючок получает слот и успех
    ровно один раз, в state слота — отметка «ok» со временем."""
    gw = await _hello(link)
    await gw.send("applied", {"ok": True, "error": ""})
    assert await _until(lambda: hook), "крючок итога не вызван"
    await asyncio.sleep(0.1)
    assert hook == [(1, True, "")], f"итог показан не так или не один раз: {hook}"
    assert _applied_state(services).startswith("ok "), _applied_state(services)
    await gw.close()


async def test_a_failure_report_carries_its_reason_in_one_short_line(link, services, hook):
    """Отказ приходит с причиной: крючок получает её одной строкой и не
    длиннее 300 знаков — хвост вывода скрипта с чужой машины не должен
    раздувать сообщение в чате админа."""
    gw = await _hello(link)
    await gw.send("applied", {"ok": False, "error": "скрипт\n  упал:\tнет места " + "x" * 400})
    assert await _until(lambda: hook), "крючок итога при отказе не вызван"
    (slot, ok, error), = hook
    assert (slot, ok) == (1, False), hook
    assert error.startswith("скрипт упал: нет места x") and "\n" not in error and "\t" not in error, error
    assert len(error) <= 300, f"причина длиной {len(error)} прошла в чат целиком"
    st = _applied_state(services)
    assert st.startswith("fail ") and "скрипт упал: нет места" in st, st
    await gw.close()


async def test_a_failing_hook_does_not_break_the_session(link, services, monkeypatch):
    """Крючок упал (Telegram не ответил) — сессия канала живёт дальше, итог
    в state записан, следующий снимок принимается как обычно."""
    monkeypatch.setattr(linkserver, "_on_applied", None)
    seen = []

    async def broken(slot_id, ok, error, fp=""):
        seen.append(ok)
        raise RuntimeError("Telegram не ответил")
    linkserver.set_on_applied(broken)
    gw = await _hello(link)
    await gw.send("applied", {"ok": True, "error": ""})
    assert await _until(lambda: seen), "крючок не вызван"
    await gw.send("snap", {**SNAP, "rev": 1})
    assert await _until(lambda: services.gwlink_snapshot(1)), "после упавшего крючка снимок не принят"
    assert link.online(1), "упавший крючок порвал сессию"
    assert _applied_state(services).startswith("ok ")
    await gw.close()


async def test_without_a_hook_the_report_is_only_stored(link, services, monkeypatch):
    """Крючок не поставлен (слушатель поднят раньше, чем main его повесил) —
    итог просто ложится в state, сессия не рвётся."""
    monkeypatch.setattr(linkserver, "_on_applied", None)
    gw = await _hello(link)
    await gw.send("applied", {"ok": False, "error": "нет"})
    assert await _until(lambda: _applied_state(services)), "итог без крючка не записан"
    assert _applied_state(services).startswith("fail ") and _applied_state(services).endswith(" нет")
    await gw.send("snap", {**SNAP, "rev": 1})
    assert await _until(lambda: services.gwlink_snapshot(1)) and link.online(1)
    await gw.close()


def test_forgetting_a_slot_clears_its_result_and_file_record(link, services):
    """Слот сняли — его итог и память о том, где в чате лежал файл, уходят
    вместе с ним: иначе новый слот с тем же номером удалил бы чужие сообщения."""
    services.gwlink_applied_in(1, {"ok": True})
    services.gw_bundle_msg_set(1, config.ADMIN_ID, 501, 500)
    assert _applied_state(services) and services.gw_bundle_msg_get(1)
    services.gwlink_forget(1)
    assert _applied_state(services) == "", "итог пережил снятие слота"
    assert services.gw_bundle_msg_get(1) == {}, "запись о файле пережила снятие слота"


# ── оба конца: боевой клиент агента ─────────────────────────────────────────

class _Pi(GatewayServices):
    """Агент шлюза: снимок ровно тот, что ВПС выдаёт слоту, — сервер не шлёт
    настроек, и в канале только то, что относится к делу."""

    def gw_snapshot(self) -> dict:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in SNAP.items()}

    def gateway_claim_if_needed(self):
        return None

    def services_local(self):
        return []


@pytest.fixture()
async def agent(link, tmp_path, monkeypatch):
    """Настоящий агент с клиентом канала; `kinds` — что сервер получил от
    него по порядку, через все сессии."""
    conf = tmp_path / "awglink.conf"
    conf.write_text(f"# awg-bot: контракт линка 1\n[Interface]\nPrivateKey = {_chan.PRIV}\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(conf))
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.1.0")
    monkeypatch.setattr(linkclient, "server_address", lambda: link._bound[0][0])
    monkeypatch.setattr(linkclient, "server_port", lambda: link._bound[0][1])
    monkeypatch.setattr(linkclient, "_notify", None)
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "1" if k == "LINK_CHANNEL" else "")
    kinds: list[str] = []
    real = link._handle

    async def spy(gw, sess, msg):
        kinds.append(str(msg.get("t")))
        return await real(gw, sess, msg)
    monkeypatch.setattr(link, "_handle", spy)
    db = Database(str(tmp_path / "pi.db")); db.init_schema()
    pi = _Pi(db)
    client = linkclient.LinkClient(pi)
    monkeypatch.setattr(linkclient, "_client", client)
    try:
        yield pi, client, kinds
    finally:
        await client.stop()
        db.close()


async def _up(client, kinds, services) -> None:
    """Поднять сессию и дождаться, пока сервер примет её снимок."""
    n = kinds.count("snap")
    client.start()
    assert await _until(lambda: kinds.count("snap") > n and client._writer is not None, timeout=5), \
        f"сессия не поднялась: {kinds}"


async def test_a_result_made_with_the_channel_up_goes_at_once_and_only_once(link, services, hook, agent):
    """Канал жив в момент применения: итог уходит сразу, без ожидания тика,
    и из очереди агента пропадает. Переподключение после этого (бандл
    перезапустил линк) не шлёт его второй раз — у админа одно сообщение."""
    pi, client, kinds = agent
    await _up(client, kinds, services)
    await linkclient.report_applied(pi, True, "")
    assert await _until(lambda: hook), f"итог не дошёл до крючка; сервер получил {kinds}"
    assert hook == [(1, True, "")], hook
    assert await _until(lambda: pi.applied_pending_get() == {}), \
        "итог остался в очереди агента после подтверждения сервера"
    await client.stop()
    await _up(client, kinds, services)
    await asyncio.sleep(0.3)
    assert hook == [(1, True, "")], f"итог ушёл повторно при переподключении: {hook}"
    assert kinds.count("applied") == 1, kinds


async def test_a_result_made_with_the_channel_down_goes_right_after_the_snapshot(link, services, hook, agent):
    """Канала не было в момент применения — итог ждёт и уходит первым делом
    при подключении, сразу за полным снимком (раньше любой дельты). Следующее
    подключение без нового применения ничего не шлёт."""
    pi, client, kinds = agent
    await linkclient.report_applied(pi, False, "скрипт упал")
    assert pi.applied_pending_get().get("error") == "скрипт упал", "итог без канала не сохранён"
    assert hook == [] and kinds == [], "без канала что-то ушло на сервер"
    await _up(client, kinds, services)
    assert await _until(lambda: hook, timeout=5), f"отложенный итог не дошёл; сервер получил {kinds}"
    assert hook == [(1, False, "скрипт упал")], hook
    session = kinds[kinds.index("hello"):]
    assert session.index("applied") > session.index("snap"), f"итог ушёл раньше снимка: {session}"
    assert "delta" not in session[:session.index("applied")], f"итог ждал дельты: {session}"
    assert await _until(lambda: pi.applied_pending_get() == {}), \
        "итог остался в очереди после подтверждения сервера"
    await client.stop()
    await _up(client, kinds, services)
    await asyncio.sleep(0.3)
    assert kinds.count("applied") == 1, f"итог ушёл и во второй сессии: {kinds}"
    assert hook == [(1, False, "скрипт упал")], hook


async def test_a_session_without_a_pending_result_says_nothing_about_it(link, services, hook, agent):
    """Ничего не применяли — при подключении вида `applied` нет вовсе: иначе
    каждый перезапуск линка приносил бы админу «конфигурация применена»."""
    _pi, client, kinds = agent
    await _up(client, kinds, services)
    await asyncio.sleep(0.3)
    assert "applied" not in kinds and hook == [], kinds


async def test_a_result_whose_ack_was_lost_is_resent_and_shown_once(link, services, hook, agent):
    """Бандл перезапустил линк: итог сервер получил, а подтверждение до
    агента не дошло — сессия умерла раньше. Очередь агента цела, итог уходит
    при следующем подключении, сервер узнаёт повтор: админу одно сообщение,
    а очередь агента после второго подтверждения пуста."""
    pi, client, kinds = agent
    real_send = link.send
    drop = {"on": True}

    async def send(slot_id, kind, body=None, **kw):
        if kind == "applied_ack" and drop["on"]:
            return True                              # строка «ушла» в умирающую сессию
        return await real_send(slot_id, kind, body, **kw)
    link.send = send
    await _up(client, kinds, services)
    await linkclient.report_applied(pi, True, "", FP)
    assert await _until(lambda: hook), f"итог не дошёл; сервер получил {kinds}"
    await asyncio.sleep(0.2)
    assert pi.applied_pending_get().get("fp") == FP, "очередь вычищена без подтверждения сервера"
    await client.stop()
    drop["on"] = False
    await _up(client, kinds, services)
    assert await _until(lambda: kinds.count("applied") == 2, timeout=5), \
        f"неподтверждённый итог не ушёл повторно: {kinds}"
    assert await _until(lambda: pi.applied_pending_get() == {}), "после подтверждения итог остался в очереди"
    assert hook == [(1, True, "")] and hook.fps == [FP], f"повтор показан админу второй раз: {hook}"


async def test_an_ack_for_another_result_leaves_the_queue_alone(link, services, agent):
    """Подтверждение пришло на прежний итог, а в очереди уже новый (второе
    применение, пока первое ехало) — новый остаётся и уйдёт."""
    pi, client, _kinds = agent
    pi.applied_pending_set(False, "нет", FP)
    at = pi.applied_pending_get()["at"]
    await client._applied_acked({"t": "applied_ack", "fp": "f" * 16, "at": at})
    assert pi.applied_pending_get().get("fp") == FP, "подтверждение чужого файла вычистило очередь"
    await client._applied_acked({"t": "applied_ack", "fp": FP, "at": "2000-01-01T00:00:00+03:00"})
    assert pi.applied_pending_get().get("fp") == FP, "подтверждение прежнего применения вычистило новое"
    await client._applied_acked({"t": "applied_ack", "fp": FP, "at": at})
    assert pi.applied_pending_get() == {}
