
"""
liveness.py — часть пакета reserve (см. __init__): живость линка пары —
пропуски keepalive и проба по подозрению.

Источник — счётчик `rx` линка пары (`awg show awgpair transfer`, локально,
каждый такт в 5 с): keepalive второй стороны идёт в 25–35 с, и тишина
≥ `suspect_seconds` (40) — пропущен хотя бы один. Тогда проба: `ping` по
каналу пары трижды через 5 с (любой ответ или пакет снимает подозрение);
нет ответа `probe_seconds` (15) — «недоступна» с момента `t0`. Одна
потерянная keepalive «недоступна» не даёт: её закрывает проба. Без подозрения
прикладных сообщений нет (в простое канал молчит).

Состояния: `ok` → `suspect` (с `since`) → `down` (с `t0`); любой признак
жизни (рост `rx`, слово по каналу, `pong`) — снова `ok`. Машина чистая
(`step`), сервис держит её состояние в памяти процесса.
"""
from __future__ import annotations

import secrets
import subprocess
import time
from dataclasses import dataclass, field

from awgbot.core import config, settings
from awgbot.domain.services.types import Notification
from awgbot.util import timeutil

from awgbot.domain.notices import reserve as rn

from ..gateway_link.common import log

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
        return _alive(lv, now)
    if rx is not None and rx != lv.last_rx:
        if lv.last_rx >= 0 and rx > lv.last_rx or lv.last_rx < 0:
            lv.last_rx = rx
            return _alive(lv, now)
        lv.last_rx = rx                                # счётчик обнулился (переподнятие) — не жизнь, но и не тишина
    silence = now - lv.last_life
    if lv.state == "ok":
        if silence >= suspect_s:
            lv.state, lv.since, lv.pings, lv.last_ping = "suspect", now, 0, 0.0
            return _ping(lv, now)
        return ""
    if lv.state == "suspect":
        if now - lv.since >= probe_s:
            lv.state, lv.t0 = "down", now
            return "down"
        if lv.pings < PROBE_COUNT and now - lv.last_ping >= PROBE_EVERY:
            return _ping(lv, now)
        return ""
    return ""                                              # down: ждём жизни


def _ping(lv: Liveness, now: float) -> str:
    lv.pings += 1
    lv.last_ping = now
    lv.nonce = secrets.token_hex(8)
    return "ping"


def _alive(lv: Liveness, now: float) -> str:
    lv.last_life = now
    was = lv.state
    lv.state, lv.pings = "ok", 0
    return "up" if was == "down" else ""


def _word(lv: Liveness, now: float) -> None:
    """Слово по каналу (hello, любое сообщение, pong): жизнь. После «недоступен»
    состояние не меняем здесь — «снова на связи» со всеми отметками отдаст
    ближайший такт (один путь для счётчика и для слова)."""
    lv.last_life = now
    if lv.state == "down":
        lv.pending_up = True
    else:
        lv.state, lv.pings = "ok", 0


class ReserveLivenessMixin:

    @property
    def _lv(self) -> Liveness:
        return self.__dict__.setdefault("_reserve_liveness", Liveness())

    def _pair_rx(self) -> int | None:
        """Счётчик rx линка пары; None — интерфейса нет или не прочитан."""
        env = self._pair_env()
        try:
            proc = subprocess.run(["awg", "show", env["PAIR_IF"], "transfer"], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        total = 0
        for line in proc.stdout.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                total += int(parts[1])
        return total

    def reserve_liveness_tick(self, now: float | None = None) -> tuple[str, list[Notification]]:
        """Такт живости (5 с). Возвращает (действие для канала: ping/"", уведомления)."""
        if not self.reserve_paired():
            self.__dict__.pop("_reserve_liveness", None)
            return "", []
        now = time.monotonic() if now is None else now
        lv = self._lv
        act = step(lv, now, self._pair_rx(), suspect_s=settings.get_int("app.reserve.suspect_seconds", 40),
                   probe_s=settings.get_int("app.reserve.probe_seconds", 15))
        notes: list[Notification] = []
        if act == "down":
            lv.told = True
            self.rsv_update("rsv_peer_state", down_since=timeutil.now_iso())
            notes.append(Notification(tg_id=int(config.ADMIN_ID),
                                      text=rn.peer_down(self.reserve_peer_name(), self.reserve_down_since())))
            log.warning("резерв: «%s» недоступен — линк пары молчит", self.reserve_peer_name())
            return "", notes
        if act == "up":
            return "", self._liveness_up()
        return act, notes

    def _liveness_up(self) -> list[Notification]:
        """Связь вернулась (по счётчику, по слову или по pong): отметка и Т-РУ8 один раз."""
        self.rsv_update("rsv_peer_state", down_since="")
        log.info("резерв: «%s» снова на связи", self.reserve_peer_name())
        return [Notification(tg_id=int(config.ADMIN_ID), text=rn.peer_up(self.reserve_peer_name()))]

    def reserve_liveness_word(self) -> list[Notification]:
        """Слово по каналу — жизнь; «снова на связи» после «недоступен» отдаст такт."""
        _word(self._lv, time.monotonic())
        return []

    def reserve_pong_in(self, nonce: str) -> list[Notification]:
        lv = self._lv
        if nonce == lv.nonce:
            _word(lv, time.monotonic())
        return []

    def reserve_ping_nonce(self) -> str:
        return self._lv.nonce

    def reserve_peer_seen(self) -> str:
        """`ok` / `suspect` / `down` — для карточки и st."""
        return self._lv.state if self.reserve_paired() else "ok"

    def reserve_down_since(self) -> str:
        return str(self.reserve_peer_state().get("down_since") or "")
