"""
livestep.py — живость постоянного линка AWG по пропускам keepalive и пробе по
подозрению: чистая машина состояний, одна на обе роли (линк пары и линк
слота у сервера, линк к резерву и линк слота у шлюза).

Источник — счётчик `rx` интерфейса (`awg show <линк> transfer`, локально,
каждый такт в 5 с): keepalive второй стороны идёт в 25–35 с, тишина ≥
`suspect_s` (40) — пропущен хотя бы один. Тогда проба: `ping` по каналу
трижды через 5 с (любой ответ или пакет снимает подозрение); нет ответа
`probe_s` (15) — «недоступна» с момента `t0`. Одна потерянная keepalive
«недоступна» не даёт: её закрывает проба.

Состояния: `ok` → `suspect` (с `since`) → `down` (с `t0`); любой признак
жизни (рост `rx`, слово по каналу, `pong`) — снова `ok`. Слово по каналу
после «недоступна» ставит `pending_up`: «снова на связи» отдаёт ближайший
такт (один путь для счётчика и для слова).

Внизу — помощники для отметок `time.monotonic()`, записанных в базу (сроки
шагов переезда и возврата, пробы, демпфер, повторы просьб): после
перезагрузки хоста monotonic начинается заново, и отметка прежней загрузки
оказывается в будущем. `mono_since`/`mono_due` считают её давней (пора),
`mono_expired` — сроком, который вышел (в том числе срок дальше
`MONO_SANE_S`: ни одно ожидание в проекте не длиннее часа, а срок «аптайм до
перезагрузки + ожидание» после перезагрузки лежит дальше — иначе шаг ждал бы
столько, сколько хост работал до неё).
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

PROBE_EVERY = 5
PROBE_COUNT = 3


@dataclass
class Liveness:
    state: str = "ok"               # ok | suspect | down
    last_rx: int = -1               # счётчик линка на прошлом такте
    last_life: float = 0.0          # когда последний раз видели жизнь (monotonic)
    since: float = 0.0              # когда началось подозрение
    t0: float = 0.0                 # когда стало «недоступна»
    pings: int = 0                  # отправлено проб в этом подозрении
    last_ping: float = 0.0
    nonce: str = ""
    told: bool = False              # уведомление «недоступен» ушло
    pending_up: bool = False        # слово по каналу после «недоступен»: «снова на связи» отдаст такт
    events: list = field(default_factory=list)


def step(lv: Liveness, now: float, rx: int | None, *, suspect_s: int, probe_s: int) -> str:
    """Один такт. Возвращает действие: "" / "ping" / "down" / "up"."""
    if lv.last_life == 0.0:
        lv.last_life = now
    if lv.pending_up:
        lv.pending_up = False
        if rx is not None:
            lv.last_rx = rx
        return alive(lv, now)
    if rx is not None and rx != lv.last_rx:
        if lv.last_rx >= 0 and rx > lv.last_rx or lv.last_rx < 0:
            lv.last_rx = rx
            return alive(lv, now)
        lv.last_rx = rx                                # счётчик обнулился (переподнятие) — не жизнь, но и не тишина
    silence = now - lv.last_life
    if lv.state == "ok":
        if silence >= suspect_s:
            lv.state, lv.since, lv.pings, lv.last_ping = "suspect", now, 0, 0.0
            return ping(lv, now)
        return ""
    if lv.state == "suspect":
        if now - lv.since >= probe_s:
            lv.state, lv.t0 = "down", now
            return "down"
        if lv.pings < PROBE_COUNT and now - lv.last_ping >= PROBE_EVERY:
            return ping(lv, now)
        return ""
    return ""                                              # down: ждём жизни


def ping(lv: Liveness, now: float) -> str:
    lv.pings += 1
    lv.last_ping = now
    lv.nonce = secrets.token_hex(8)
    return "ping"


def alive(lv: Liveness, now: float) -> str:
    lv.last_life = now
    was = lv.state
    lv.state, lv.pings = "ok", 0
    return "up" if was == "down" else ""


def word(lv: Liveness, now: float) -> None:
    """Слово по каналу (hello, любое сообщение, pong): жизнь. После «недоступен»
    состояние не меняем здесь — «снова на связи» со всеми отметками отдаст
    ближайший такт."""
    lv.last_life = now
    if lv.state == "down":
        lv.pending_up = True
    else:
        lv.state, lv.pings = "ok", 0


def pong(lv: Liveness, now: float, nonce: str) -> bool:
    """Ответ на пробу с нашим нонсом — слово; чужой нонс — нет."""
    if nonce and nonce == lv.nonce:
        word(lv, now)
        return True
    return False


# ── монотонные отметки в базе и перезагрузка хоста ───────────────────────────
MONO_SANE_S = 3600                # срок дальше этого в будущем — отметка прежней загрузки (ожидания короче часа)

def mono_since(mark, now: float) -> float:
    """Секунды с отметки monotonic. Отметка из прежней загрузки хоста (после
    перезагрузки monotonic начинается с нуля, и записанная в базе отметка
    оказывается в будущем) — считается давней: срок истёк, проба пора."""
    try:
        m = float(mark or 0)
    except (TypeError, ValueError):
        return float("inf")
    if not m or m > now + 1.0:
        return float("inf")
    return now - m


def mono_expired(deadline, now: float) -> bool:
    """Срок (отметка в будущем) вышел: прошёл — или он дальше MONO_SANE_S, то есть
    записан в прежней загрузке хоста."""
    try:
        d = float(deadline or 0)
    except (TypeError, ValueError):
        return False
    if not d:
        return False
    return now > d or d > now + MONO_SANE_S


def mono_due(mark, now: float, interval: float) -> bool:
    """Пора ли: с отметки прошло не меньше interval (или отметки нет / она из прежней загрузки)."""
    return mono_since(mark, now) >= float(interval)
