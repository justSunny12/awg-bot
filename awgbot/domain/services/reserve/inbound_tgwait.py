"""
inbound_tgwait.py — часть пакета reserve (см. __init__): «пользователи основы у
меня» — VPN у меня, а бот основы здесь Telegram не взял (вынесено из inbound).

Аварийный путь (открытие по точкам кворума или по коду): процесс открытия сам
сменил запись DNS (`done` без `record=skipped`) — пользователи уже идут на клон.
Срыв по боту (гость не поднялся, вебхук не поставлен за TAKEN_WAIT_S) зону больше
не снимает: снять её — оставить пользователей на адресе, где клона нет, из-за
Telegram. Состояние — serving без `tg_taken` (`tg_wait` — причина), админу
резерва — уведомление; такт serving раз в TG_RETRY_S (± 40 %) повторяет захват:
отметка `tg_take` на месте, юнит гостя — снова, если не активен (вебхук гость
ставит сам, пока отметка есть). Гость взял — `tg_taken`, Т-РУ4 обычным путём.
Зону снимает только `back` основы (запись она вернёт сама) или снятие мимо бота
(`awg-bot reserve teardown` — такт serving видит фазу зоны). Срыв до смены записи
(`record=skipped`) и путь с K основы — как прежде, `_take_fail`.

`st.in_tg` основе: `forced` — гость держит Telegram по принудительному захвату
(основа не отдала бота и пропала), `wait` — VPN здесь, бота нет; основа по `forced`
на сроке шага переезда уходит в away, а не в срыв (outbound_forced).
"""
from __future__ import annotations

import random
import subprocess
import time

from awgbot.core import config
from awgbot.util import livestep, timeutil

from ..types import Notification
from ..gateway_link.common import log
from .state import _LOCK

TG_RETRY_S = 60                   # повтор захвата Telegram гостем, пока VPN здесь без бота


class ReserveInboundTgWaitMixin:

    def _take_keep_vpn(self, reason: str) -> list[tuple]:
        """Срыв по боту после смены записи процессом открытия: зона остаётся, serving без
        `tg_taken`; один раз под замком (такт и обработчик слова гостя — разные потоки)."""
        from awgbot.domain.notices import reserve as rn
        now = time.monotonic()
        with _LOCK:
            inn = self._inn()
            if str(inn.get("state") or "") != "taking" or inn.get("tg_taken"):
                return []
            self.reserve_set_in("serving", kind=str(inn.get("kind") or ""), gen=int(inn.get("gen") or 0),
                                tg_taken=False, served=True, tg_wait=str(reason)[:200], tg_retry_mono=now,
                                taken_at=timeutil.now_iso(), step="")
        name = self.reserve_peer_name()
        log.warning("резерв: пользователи «%s» у меня (запись уже здесь), а бот основы Telegram не взял: %s — "
                    "зона остаётся, захват повторяю", name, reason)
        return [("fw",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.hosting_without_bot(name, reason))])]

    def _tick_tg_wait(self, inn: dict) -> list[tuple]:
        """serving без `tg_taken`: раз в TG_RETRY_S ± 40 % — отметка `tg_take` на месте и юнит
        гостя, если не активен; вебхук гость ставит сам, `tg_taken` уведёт в обычный serving."""
        from awgbot.infra import rsvzone
        now = time.monotonic()
        if not livestep.mono_due(inn.get("tg_retry_mono"), now, TG_RETRY_S * random.uniform(0.6, 1.4)):
            return []
        self.rsv_update("rsv_in", tg_retry_mono=now)
        try:
            if not rsvzone.TG_TAKE_FILE.exists():
                rsvzone.write_private(rsvzone.TG_TAKE_FILE, "1")
        except OSError as e:
            log.warning("резерв: отметка tg_take для бота основы не записана: %s", e)
        if rsvzone.guest_unit_active():
            return []
        try:
            subprocess.run(["systemctl", "reset-failed", rsvzone.GUEST_UNIT], capture_output=True, timeout=30)
            proc = subprocess.run(["systemctl", "start", rsvzone.GUEST_UNIT], capture_output=True, timeout=60)
            if proc.returncode != 0:
                raise subprocess.SubprocessError(f"код {proc.returncode}")
            log.info("резерв: бот основы на резерве запущен снова — ставить вебхук")
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("резерв: бот основы на резерве не запустился: %s", e)
        return []

    def reserve_in_tg(self) -> str:
        """`st.in_tg`: forced — гость держит Telegram по принудительному захвату, wait — VPN
        здесь без бота; иначе пусто."""
        inn = self._inn()
        if str(inn.get("state") or "") != "serving":
            return ""
        if not inn.get("tg_taken"):
            return "wait"
        return "forced" if inn.get("tg_take_forced") else ""
