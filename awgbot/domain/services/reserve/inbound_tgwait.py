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

Слово гостю «ставить ли вебхук» (`tg{go, foreign}` по локальному каналу, на hello и по
смене — reserve_guest_tg_view): без него гость вебхук не ставит, и отметка `tg_take` на
диске сама по себе — больше не право (гость после рестарта иначе отнимал бы Telegram у
основы, которая его уже забрала). `go` — пользователи у меня (taking | serving), факт
передачи бота (отметка `tg_take`, `released` журнала, принудительный захват, вебхук уже
был у гостя) и основа Telegram не забирала: её живой `st` после начала эпизода не говорит
`home`/`returning`, её `taken` в этом эпизоде не приходил, гость не сказал «вебхук не
здесь» (`webhook_foreign`, в том числе отметкой с прошлого запуска в hello).

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

    def reserve_guest_tg_view(self) -> dict:
        """`tg{go, foreign}` гостю: ставить ли вебхук (см. шапку); `foreign` — я знаю, что вебхук
        был не у гостя (Telegram у основы): гость с такой отметкой без этого знания не ставит."""
        inn = self._inn()
        foreign = bool(inn.get("webhook_foreign"))
        why = "гость сказал «вебхук не здесь» — Telegram у основы" if foreign else self._guest_tg_hold_why(inn)
        if self.__dict__.get("_rsv_guest_tg_why") != why:
            self.__dict__["_rsv_guest_tg_why"] = why
            if why and str(inn.get("state") or "") in ("taking", "serving"):
                log.info("резерв: боту основы у меня вебхук не ставить — %s", why)
            elif not why:
                log.info("резерв: боту основы у меня — ставить вебхук")
        return {"go": not why, "foreign": foreign}

    def _guest_tg_hold_why(self, inn: dict) -> str:
        """Почему гостю сейчас не ставить вебхук; пусто — можно."""
        from awgbot.infra import rsvzone
        if str(inn.get("state") or "") not in ("taking", "serving"):
            return "пользователей основы у меня нет"
        if not (rsvzone.TG_TAKE_FILE.exists() or inn.get("jrn_released") or inn.get("tg_take_forced")
                or inn.get("tg_taken")):
            return "бот основы ещё не передан"
        if inn.get("peer_tg_taken"):
            return "основа сказала taken — Telegram у неё"
        peer = self.reserve_peer_state()
        if str(peer.get("out") or "") in ("home", "returning") and self._peer_alive(peer) \
                and str(peer.get("at") or "") > str(inn.get("open_started") or ""):   # st до начала эпизода — о прошлом
            return f"основа на связи и {peer.get('out')} — Telegram у неё"
        return ""

    def reserve_peer_tg_taken_in(self) -> None:
        """`taken` основы, пока её пользователи у меня (возврат: она забрала Telegram) — гостю
        вебхук больше не ставить до конца эпизода."""
        if str(self._inn().get("state") or "") in ("taking", "serving", "releasing") and not self._inn().get("peer_tg_taken"):
            self.rsv_update("rsv_in", peer_tg_taken=timeutil.now_iso())
            log.info("резерв: «%s» забрала Telegram (taken) — её боту у меня вебхук не ставить",
                     self.reserve_peer_name())
