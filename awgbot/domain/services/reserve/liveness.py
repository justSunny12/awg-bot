
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

import subprocess
import time

from awgbot.core import config, settings
from awgbot.domain.services.types import Notification
from awgbot.util import timeutil

from awgbot.domain.notices import reserve as rn

from ..gateway_link.common import log

# машина живости — util/livestep (общая с агентом шлюза); имена оставлены для тестов
from awgbot.util.livestep import (Liveness, step, PROBE_EVERY, PROBE_COUNT,  # noqa: E402,F401
                                  ping as _ping, alive as _alive, word as _word)

__all__ = ["Liveness", "step", "PROBE_EVERY", "PROBE_COUNT", "_ping", "_alive", "_word", "ReserveLivenessMixin"]


class ReserveLivenessMixin:

    @property
    def _lv(self) -> Liveness:
        lv = self.__dict__.get("_reserve_liveness")
        if lv is None:
            lv = Liveness()
            if str(self.rsv_get("rsv_peer_state").get("down_since") or ""):
                # перезапуск бота посреди «недоступен»: эпизод тот же (отметка на диске, Т-РУ8 ушло) — не
                # «на связи» до первой жизни; первый отсчёт rx — база, не жизнь (`_lv_base`)
                lv.state, lv.told = "down", True
                self.__dict__["_lv_base"] = True
            self.__dict__["_reserve_liveness"] = lv
        return lv

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
            self.__dict__.pop("_lv_base", None)
            return "", []
        now = time.monotonic() if now is None else now
        lv = self._lv
        rx = self._pair_rx()
        if self.__dict__.get("_lv_base") and not lv.pending_up:
            if rx is not None:
                lv.last_rx = rx                               # база счётчика восстановленного «недоступен»
                self.__dict__.pop("_lv_base", None)
            return "", []
        self.__dict__.pop("_lv_base", None)
        act = step(lv, now, rx, suspect_s=settings.get_int("app.reserve.suspect_seconds", 40),
                   probe_s=settings.get_int("app.reserve.probe_seconds", 15))
        notes: list[Notification] = []
        if act == "down":
            lv.told = True
            # st до потери связи — устаревший, пока не придёт новый (journal/epoch: «гостя нет»)
            self.rsv_update("rsv_peer_state", down_since=timeutil.now_iso(), st_stale=True)
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
