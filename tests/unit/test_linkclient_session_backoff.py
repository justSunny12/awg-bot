"""
Клиент канала на шлюзе: бэкофф, ключ линка и коннект, нонсы сессии, запас
своего скачивания фидов и побудка бэкоффа по восстановлению линка.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from awgbot.runtime import linkclient
from awgbot.util import gwlink
from tests.unit import _linkclient_session_common as _c
from tests.unit._linkclient_session_common import KEY, PRIV2, SN, _Agent, _Conn, _delays, _reply

pytestmark = pytest.mark.unit

conf, vps = _c.conf, _c.vps


# ── бэкофф ───────────────────────────────────────────────────────────────────

async def test_a_server_that_hangs_up_at_once_does_not_reset_the_backoff(vps, monkeypatch):
    """Чужой ключ или чужой слот: сервер отвергает hello и закрывает сессию
    сразу. Раньше любая «состоявшаяся»
    сессия обнуляла счёт — выходил стук каждые пять секунд без роста шага.
    Теперь шаг растёт, пока сервер не ответит хоть одним подписанным словом."""
    for _ in range(4):
        vps.hangs_up()
    delays = _delays(monkeypatch, stop_after=4)
    with pytest.raises(asyncio.CancelledError):
        await linkclient.LinkClient(_Agent())._run()
    assert vps.connects == 4
    assert delays == [5, 15, 45, 135], f"шаг не растёт у сессий, закрытых сразу: {delays}"


async def test_a_forged_answer_counts_as_no_answer(vps, monkeypatch):
    """Ответ чужим ключом — не ответ: сессия рвётся, и сбрасывать шаг не за что."""
    vps.forged(); vps.forged(); vps.forged()
    agent = _Agent()
    delays = _delays(monkeypatch, stop_after=3)
    with pytest.raises(asyncio.CancelledError):
        await linkclient.LinkClient(agent)._run()
    assert delays == [5, 15, 45], f"чужая подпись сбросила бэкофф: {delays}"
    assert agent.roles == [], "роль из сообщения с чужой подписью применена"


async def test_one_real_answer_starts_the_count_over(vps, monkeypatch):
    """Сервер ответил подписанным сообщением — сессия была настоящей, и после
    её конца незачем ждать пять минут: следующая попытка снова с малого шага."""
    vps.hangs_up(); vps.hangs_up(); vps.answers(); vps.hangs_up(); vps.hangs_up()
    agent = _Agent()
    delays = _delays(monkeypatch, stop_after=5)
    with pytest.raises(asyncio.CancelledError):
        await linkclient.LinkClient(agent)._run()
    assert delays == [5, 15, 5, 15, 45], f"счёт после настоящей сессии: {delays}"
    assert agent.roles == [True]


# ── ключ и коннект ───────────────────────────────────────────────────────────

async def test_the_link_key_is_read_anew_for_every_connection(vps, conf, monkeypatch):
    """Бандл из чата привёз новый ключ линка, а процесс агента тот же. С ключом,
    закешированным с прошлой сессии, каждая следующая отвергалась бы — канал
    мёртв до перезапуска агента, и никто не понимает почему."""
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    first = vps.answers(active=True)
    await client._connect_once()
    assert agent.roles == [True]
    conf.write_text(f"[Interface]\nPrivateKey = {PRIV2}\n", encoding="utf-8")   # применили новый бандл
    second = vps.answers(key=gwlink.channel_key(PRIV2), active=False)
    await client._connect_once()
    assert agent.roles == [True, False], "вторая сессия подписана новым ключом, а клиент держит старый"
    hello = gwlink.unpack(gwlink.channel_key(PRIV2), second.written[0])
    assert hello["t"] == "hello", "hello новой сессии подписан старым ключом"
    assert gwlink.unpack(KEY, first.written[0])["t"] == "hello"


async def test_a_connect_that_hangs_is_given_up_in_bounded_time(monkeypatch, conf):
    """SYN дропнут где-то по пути: без таймаута попытка висела бы до системного
    — минуты, — и бэкофф считал бы не то. Здесь коннект не отвечает вовсе;
    клиент обязан сдаться сам, за ограниченное время."""
    seen: list = []
    real_wait_for = asyncio.wait_for

    async def open_connection(host, port, limit=None):
        await asyncio.Event().wait()             # ни ответа, ни отказа

    async def fast_wait_for(aw, timeout):
        seen.append(timeout)                     # сколько клиент готов ждать
        return await real_wait_for(aw, 0.05 if timeout is not None else None)

    monkeypatch.setattr(linkclient.asyncio, "open_connection", open_connection)
    monkeypatch.setattr(linkclient.asyncio, "wait_for", fast_wait_for)
    monkeypatch.setattr(linkclient, "server_address", lambda: "10.99.99.1")
    client = linkclient.LinkClient(_Agent())
    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await client._connect_once()
    assert time.monotonic() - t0 < 2, "висящий коннект не брошен"
    assert seen and seen[0] is not None and 0 < seen[0] <= 60, (
        f"коннект без таймаута или с таймаутом в {seen}: попытка висела бы минутами")
    assert client._writer is None


async def test_the_session_mark_follows_the_session(vps):
    """Признак «сессия открыта» живёт у домена агента: пока канал на связи, своё
    скачивание фидов молчит. Сессия кончилась — признак снят, иначе после
    обрыва шлюз не пошла бы за фидами никогда."""
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    seen: list = []
    agent.set_link_role = lambda *a, **k: seen.append(agent.channel.online)
    vps.answers()
    await client._connect_once()
    assert seen == [True], "во время сессии признак не выставлен"
    assert agent.channel.online is False, "сессия кончилась, а признак остался"
    assert agent.touched == 1, "конец сессии не отметил запас на своё скачивание фидов"


# ── нонсы сессии (proto 2) ───────────────────────────────────────────────────

async def test_hello_names_our_nonce_and_everything_after_is_signed_with_the_servers(vps):
    """Повтор отсекают нонсы: hello несёт нонс шлюза, дальше шлюз подписывает
    нонсом сервера. Подпиши клиент снимок чем-то другим — слушатель на ВПС
    оборвёт каждую сессию на первом же снимке, и канал не поднимется ни на
    одной шлюзе."""
    conn = vps.answers()
    await linkclient.LinkClient(_Agent())._connect_once()
    hello = conn.hello()
    assert hello["t"] == "hello" and hello["proto"] == gwlink.PROTO
    assert len(gwlink.nonce_from(hello.get("nonce"))) == gwlink.NONCE_BYTES, "hello не назвал нонс шлюза"
    assert len(conn.written) >= 2, "после ответа сервера снимок не ушёл"
    snap = gwlink.unpack(KEY, conn.written[1], nonce=SN)
    assert snap["t"] == "snap", f"вторым ушло «{snap['t']}»"
    with pytest.raises(gwlink.ProtocolError):
        gwlink.unpack(KEY, conn.written[1])      # без нонса сервера подпись не сходится


async def test_every_session_gets_a_fresh_nonce(vps):
    """Нонс шлюза — свой у каждой сессии. Повтори его клиент, записанный ответ
    сервера из прошлой сессии прошёл бы проверку в новой."""
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    first, second = vps.answers(), vps.answers()
    await client._connect_once()
    await client._connect_once()
    assert first.hello()["nonce"] != second.hello()["nonce"], "нонс шлюза повторился между сессиями"


async def test_a_first_server_word_without_its_nonce_ends_the_session(vps, monkeypatch):
    """Первое слово сервера обязано нести его нонс: без него клиенту нечем
    подписывать, и любая его отправка была бы отвергнута. Такой ответ — не
    ответ: сессия рвётся, роль не применяется, шаг бэкоффа растёт."""
    for _ in range(3):
        vps.sessions.append(_Conn([_reply(KEY, "role", {"active": True}, first=False)]))
    agent = _Agent()
    delays = _delays(monkeypatch, stop_after=3)
    with pytest.raises(asyncio.CancelledError):
        await linkclient.LinkClient(agent)._run()
    assert agent.roles == [], "роль из сообщения без нонса сервера применена"
    assert agent.channel.online is False
    assert delays == [5, 15, 45], f"ответ без нонса сбросил бэкофф: {delays}"


async def test_a_server_word_from_another_session_is_refused(vps, monkeypatch):
    """Записанный ответ сервера из прошлой сессии (подписан прошлым нонсом
    шлюза) под верным ключом — это старая правда: роль «несёшь трафик» от
    вчерашнего переключения. Клиент обязан его отвергнуть."""
    old = gwlink.new_nonce()
    stale = gwlink.pack(KEY, "role", {"active": True, "nonce": gwlink.nonce_b64(SN)}, seq=1, nonce=old)
    vps.sessions.append(_Conn([stale]))
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    with pytest.raises(gwlink.ProtocolError):
        await client._connect_once()
    assert agent.roles == [], "ответ чужой сессии применён"
    assert client._confirmed is False and agent.channel.online is False


async def test_later_server_words_from_another_session_end_it(vps):
    """Внутри сессии каждое слово сервера проверяется нонсом шлюза: подсунутое
    посреди живой сессии сообщение прошлой рвёт её, а не применяется."""
    old = gwlink.new_nonce()
    stale = gwlink.pack(KEY, "role", {"active": False}, seq=2, nonce=old)
    vps.sessions.append(_Conn([_reply(KEY, "role", {"active": True}), stale,
                               _reply(KEY, "role", {"active": False}, seq=3, first=False)]))
    agent = _Agent()
    await linkclient.LinkClient(agent)._connect_once()
    assert agent.roles == [True], f"слово чужой сессии применено или сессия не порвана: {agent.roles}"


async def test_nothing_goes_out_before_the_server_named_its_nonce(vps):
    """Пока сервер не ответил, нонса для подписи нет: снимок из тика или poke,
    ушедший в этот момент, оборвал бы только что открытую сессию. Поэтому до
    первого ответа клиент не «на связи» и отправить ничего не может."""
    conn = _Conn([], hang=True)
    vps.sessions.append(conn)
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    task = asyncio.create_task(client._connect_once())
    for _ in range(50):
        if conn.written:
            break
        await asyncio.sleep(0.01)
    assert len(conn.written) == 1, "hello не ушёл"
    assert agent.channel.online is False, "клиент «на связи», а сервер ещё не ответил"
    assert await client.push(full=True) is False
    assert await client._send("claim", {"token": "x"}) is False
    assert len(conn.written) == 1, "до ответа сервера ушло что-то кроме hello"
    conn.close()
    await asyncio.wait_for(task, 2)


@pytest.mark.parametrize("shift", [3600, -3600])
async def test_server_clock_an_hour_off_does_not_break_the_session(vps, monkeypatch, shift):
    """Шлюз без RTC поднялась раньше NTP и ушла на час. Раньше канал молча
    умирал ровно тогда, когда должен был сказать «часы разошлись»; теперь часы
    в проверку не входят, и сессия живёт."""
    real_pack = gwlink.pack
    monkeypatch.setattr(linkclient.gwlink, "pack",
                        lambda *a, **kw: real_pack(*a, **{**kw, "now": time.time() + shift}))
    vps.answers()
    agent = _Agent()
    await linkclient.LinkClient(agent)._connect_once()
    assert agent.roles == [True], f"слово сервера с часами {shift:+} с отвергнуто"


async def test_the_feed_grace_is_extended_only_by_a_real_session(vps, monkeypatch):
    """lan_feeds_touch в finally каждой попытки продлевал 12-часовой запас и на
    отвергнутых сессиях (старый ключ, чужой слот): фиды застывали навсегда."""
    vps.hangs_up(); vps.hangs_up(); vps.answers(); vps.hangs_up()
    agent = _Agent()
    _delays(monkeypatch, stop_after=4)
    with pytest.raises(asyncio.CancelledError):
        await linkclient.LinkClient(agent)._run()
    assert agent.touched == 1, f"запас продлён {agent.touched} раз — считаются и отвергнутые сессии"
    assert agent.channel.last_word > 0, "слово сервера не отмечено"


async def test_link_recovery_wakes_the_backoff_and_resets_the_step(vps, monkeypatch):
    """Бэкофф спит до 5 минут; линк вернулся — on_tick будит его, и следующая
    попытка идёт сразу и с малого шага."""
    c = linkclient.LinkClient(_Agent())
    monkeypatch.setattr(linkclient.random, "uniform", lambda lo, hi: 1.0)
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, c._wake.set)
    t0 = loop.time()
    assert await c._sleep_or_wake(30) is True
    assert loop.time() - t0 < 1, "побудка не прервала сон"
    assert await c._sleep_or_wake(0.01) is False
