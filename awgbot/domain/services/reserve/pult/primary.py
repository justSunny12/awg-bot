"""
primary.py — часть пакета pult (см. __init__): сторона основы.

Проверка — в такте away (outbound `reserve_out_tick`), не чаще раза в минуту с
разбросом (`("pult_probe",)`: runtime спрашивает getWebhookInfo, вебхук не
снимается, и отдаёт ответ в `reserve_pult_probe_in`). Счёт «подряд» (`pult_bad`
в `rsv_out`: начало, очередь в начале и на прошлой пробе) идёт, пока линк пары
молчит (нет живого слова резерва — journal/epoch `_peer_alive`) и каждая проба
показывает вебхук со свежей ошибкой доставки (FORCE_HOOK_ERR_S, как у вердикта
рычага) и непустой очередью; очередь убыла (Telegram доставляет), проба не
ответила или линк ожил — счёт заново. Прошло PULT_AFTER_S и очередь выросла с
начала — `("pult_take",)`: `deleteWebhook` без сброса очереди, поллинг здесь,
отметка `pult_at` (переживает рестарт: `rsv_out` в базе), уведомление админу со
звуком. Пульт — только при away или returning; дома и в новом эпизоде отметки нет
(state: reserve_set_out).

Пока пульт здесь: в `st` — `tg_pult` (бот резерва не даёт гостю ставить вебхук),
пользователям — «бот на техработах» (bot/middleware), в карточке — строка и
кнопки. «📤 Вернуть бота на «Beta»» — `pult_back{at}` второй стороне (только при
живом линке, её `pult1` и пока она несёт моих); отметка снимается по факту 409
поллинга (вебхук снова у гостя — `reserve_pult_webhook_back` из pollctl), а не по
нажатию.
"""
from __future__ import annotations

import random
import time

from awgbot.core import config
from awgbot.util import livestep, timeutil

from ...gateway_link.common import log
from ...types import Notification, ServiceError
from .. import compat
from ..outbound_return import FORCE_HOOK_ERR_S
from ..state import HOSTING, _LOCK

PULT_AFTER_S = 5 * 60            # столько подряд бот на резерве не отвечает в Telegram, а линк молчит — пульт сюда
PULT_PROBE_S = 60                # getWebhookInfo в такте away — не чаще (с разбросом вверх до 40 %)
PULT_GIVE_WAIT_S = 3 * 60        # «отдаю Telegram» в карточке, пока гость не поставил вебхук (409), — не дольше
PULT_ON_STATES = ("away", "returning")


def hook_failing(info: dict, now: float) -> bool:
    """Ответ getWebhookInfo «бот на резерве не принимает обновления»: вебхук стоит, очередь
    непуста, ошибка доставки свежее FORCE_HOOK_ERR_S. Без вебхука — не этот случай (его ставит
    только гость; снятый — уже не у резерва)."""
    if not str(info.get("url") or ""):
        return False
    try:
        pending = int(info.get("pending_update_count") or 0)
        err = float(info.get("last_error_date") or 0)
    except (TypeError, ValueError):
        return False
    return pending > 0 and err > 0 and now - err < FORCE_HOOK_ERR_S


class ReservePultPrimaryMixin:

    # ── состояние ───────────────────────────────────────────────────────────
    def reserve_pult_on(self) -> bool:
        """Пульт здесь: Telegram бота у меня, мои пользователи у резерва."""
        try:
            out = self.rsv_get("rsv_out")
        except Exception:                                     # noqa: BLE001 — таблицы нет (агент, старая база)
            return False
        return bool(out.get("pult_at")) and str(out.get("state") or "home") in PULT_ON_STATES

    def reserve_pult_st(self) -> str:
        """Поле `tg_pult` в `st`: с какого момента пульт здесь (пусто — нет)."""
        return str(self.rsv_get("rsv_out").get("pult_at") or "") if self.reserve_pult_on() else ""

    def _pult_link_silent(self) -> bool:
        return not self._peer_alive(self.reserve_peer_state())

    # ── проверка в такте away ───────────────────────────────────────────────
    def _tick_pult(self) -> list[tuple]:
        """Такт away: пора спросить Telegram о вебхуке резерва — `("pult_probe",)`. Линк ожил —
        счёт прерван."""
        out = self._out()
        if str(out.get("state") or "") != "away" or out.get("await_merge") or out.get("pult_at"):
            return []
        if not self._pult_link_silent():
            if out.get("pult_bad"):
                self.rsv_update("rsv_out", pult_bad={})
                log.info("резерв: «%s» снова на связи — счёт пульта прерван", self.reserve_peer_name())
            return []
        now = time.monotonic()
        if not livestep.mono_due(out.get("pult_probe_mono"), now, PULT_PROBE_S * random.uniform(1.0, 1.4)):
            return []
        self.rsv_update("rsv_out", pult_probe_mono=now)
        return [("pult_probe",)]

    def reserve_pult_probe_in(self, info: dict | None) -> list[tuple]:
        """Ответ getWebhookInfo пробы пульта (None — Telegram не ответил). Счёт «подряд»; условие
        выполнено — `("pult_take",)`."""
        name = self.reserve_peer_name()
        with _LOCK:
            out = self._out()
            if str(out.get("state") or "") != "away" or out.get("await_merge") or out.get("pult_at"):
                return []
            streak = dict(out.get("pult_bad") or {})
            if not self._pult_link_silent():
                if streak:
                    self.rsv_update("rsv_out", pult_bad={})
                return []
            if info is None or not hook_failing(info, timeutil.now().timestamp()):
                if streak:
                    self.rsv_update("rsv_out", pult_bad={})
                    log.info("резерв: пульт — %s, счёт заново", "Telegram не ответил" if info is None
                             else f"вебхук «{name}» без свежих ошибок доставки")
                return []
            pending = int(info.get("pending_update_count") or 0)
            if not streak or pending < int(streak.get("last") or 0):
                # начало счёта; очередь убыла — Telegram доставляет: счёт заново с этой пробы
                self.rsv_update("rsv_out", pult_bad={"since": timeutil.now_iso(), "p0": pending, "last": pending})
                if not streak:
                    log.info("резерв: бот на «%s» не принимает обновления (очередь %d), линк молчит — считаю", name, pending)
                return []
            streak["last"] = pending
            self.rsv_update("rsv_out", pult_bad=streak)
            try:
                long = timeutil.elapsed(timeutil.parse_iso(str(streak.get("since") or ""))) >= PULT_AFTER_S
            except ValueError:
                self.rsv_update("rsv_out", pult_bad={})
                return []
            if not long or pending <= int(streak.get("p0") or 0):
                return []                                     # мало времени или очередь не растёт — ждать
        log.warning("резерв: бот на «%s» не отвечает в Telegram %d мин (очередь %d), линк пары молчит — забираю Telegram",
                    name, PULT_AFTER_S // 60, pending)
        return [("pult_take",)]

    # ── взятие ──────────────────────────────────────────────────────────────
    def reserve_pult_taken(self, by: str = "auto") -> list[tuple]:
        """`deleteWebhook` прошёл (поллинг здесь): отметка пульта, админу — со звуком (запросу
        из CLI — без уведомления: итог он видит сам, а рычаг следом скажет своё)."""
        from awgbot.domain.notices import reserve_pult as rp
        with _LOCK:
            out = self._out()
            if str(out.get("state") or "") not in PULT_ON_STATES or out.get("await_merge"):
                log.info("резерв: вебхук снят, а автомат уже %s — отметки пульта нет", out.get("state") or "home")
                return []
            if out.get("pult_at"):
                return []
            self.rsv_update("rsv_out", pult_at=timeutil.now_iso(), pult_by=str(by or "auto"), pult_bad={},
                            pult_give_mono=0)
        name = self.reserve_peer_name()
        log.warning("резерв: пульт здесь — Telegram бота у меня, пользователи по-прежнему на «%s»; журнал следует", name)
        if by == "cli":
            return [("live",)]
        return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                                    text=rp.pult_taken(name, PULT_AFTER_S // 60))])]

    def reserve_pult_webhook_back(self) -> list[tuple]:
        """Поллинг здесь получил 409 (или getWebhookInfo показал вебхук): Telegram снова у бота
        на резерве — отметка пульта снята, админу — итог."""
        from awgbot.domain.notices import reserve_pult as rp
        with _LOCK:
            out = self._out()
            if not out.get("pult_at"):
                return []
            on = str(out.get("state") or "home") in PULT_ON_STATES
            self.rsv_update("rsv_out", pult_at="", pult_by="", pult_bad={}, pult_give_mono=0,
                            pult_probe_mono=time.monotonic())
        if not on:
            return []
        name = self.reserve_peer_name()
        log.warning("резерв: вебхук снова у «%s» — Telegram отдан, пульт снят", name)
        return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rp.pult_handed(name))])]

    # ── отдача ──────────────────────────────────────────────────────────────
    def _pult_giving(self, out: dict) -> bool:
        return bool(out.get("pult_give_mono")) and not livestep.mono_due(out.get("pult_give_mono"), time.monotonic(),
                                                                         PULT_GIVE_WAIT_S)

    def reserve_pult_give_check(self) -> str:
        """Пусто — «📤 Вернуть бота на «Beta»» возможна; иначе причина (всплывашка)."""
        name = self.reserve_peer_name()
        if not self.reserve_pult_on():
            return "Telegram бота не у этого сервера"
        peer = self.reserve_peer_state()
        if not self._peer_alive(peer):
            return f"«{name}» не на связи — отдать Telegram некому"
        if not compat.peer_can(peer, "pult1"):
            return f"«{name}» не умеет принять Telegram обратно — обнови бота на нём"
        if str(peer.get("inn") or "idle") not in HOSTING:
            return f"«{name}» твоих пользователей не несёт — верни их сюда"
        if self._pult_giving(self._out()):
            return f"Telegram уже отдаётся «{name}» — подожди"
        return ""

    def reserve_pult_give(self) -> list[tuple]:
        """«📤 Вернуть бота на «Beta»»: слово `pult_back` с отметкой пульта; снимет её 409."""
        why = self.reserve_pult_give_check()
        if why:
            raise ServiceError(why)
        at = str(self._out().get("pult_at") or "")
        self.rsv_update("rsv_out", pult_give_mono=time.monotonic())
        log.info("резерв: отдаю Telegram бота «%s» — жду вебхук гостя", self.reserve_peer_name())
        return [("send", "pult_back", {"at": at}), ("live",)]

    # ── карточка ────────────────────────────────────────────────────────────
    def reserve_pult_view(self) -> dict:
        """Строка карточки: с какого момента пульт здесь, отдаётся ли сейчас; пусто — пульта нет."""
        if not self.reserve_pult_on():
            return {}
        out = self._out()
        return {"at": str(out.get("pult_at") or ""), "by": str(out.get("pult_by") or ""), "giving": self._pult_giving(out)}
