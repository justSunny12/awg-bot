"""
outbound_return.py — часть пакета reserve (см. __init__): возврат пользователей с
резерва на стороне основы (выделено из outbound: там переезд кнопкой и away).

returning/back_req → `released{реплика}` → merge (merge_ops) → restart →
take_tg (`deleteWebhook`, поллинг) → record_back (запись → свой адрес, факт)
→ `back` → relay (`relay{until}`, `wiped`) → home с новым `K`. Рычаг
«вернуть без резерва» (резерв не на связи): запись на себя, Telegram
забирается, реплика вольётся по выходу резерва на связь (await_merge) — или
не вольётся (резерв вернулся пустым). Резерв на связи без гостя (по `st`
после начала возврата) или пустая реплика — возврат без слияния (сразу
take_tg). Та же реплика (по отпечатку, `merged_fp`) второй раз не сливается.
Повтор той же ошибки слияния — молча и без автоматического возврата
(`merge_blocked`). Повторы: `back_req` по сроку (резерв на повтор отдаёт
реплику из rep.bin), merge по сроку и после рестарта посреди — снова
back_req, restart — не чаще раза в минуту, take_tg — каждый такт, ~5 мин
отказов — уведомление, record_back без факта 30 мин — `back` всё равно и
Т-РУ11 (при «вручную» просьба сменить запись — сразу), `back` раз в минуту до
`relay`.
"""
from __future__ import annotations

import time

from awgbot.core import config
from awgbot.util import livestep, timeutil

from ..types import Notification, ServiceError
from ..gateway_link.common import log
from .outbound import FORCE_OFFLINE_S, RECORD_WAIT_S, RELAY_WAIT_S, RELEASED_WAIT_S
from .state import _LOCK


class ReserveReturnMixin:
    def reserve_can_return(self) -> bool:
        out = self._out()
        return str(out.get("state") or "") == "away" and bool(out.get("can_return"))

    def reserve_return_dialog_data(self) -> dict:
        """Т-Р11б у основы: имя резерва, запись и свой адрес; `manual` — запись меняет человек."""
        return {"name": self.reserve_peer_name(), "record": self.reserve_dns_name(), "own": self.reserve_own_ip(),
                "manual": not self.reserve_dns_automatic()}

    def reserve_return_check(self) -> str:
        if self.reserve_out() != "away":
            return "пользователи не у резерва"
        if not self.reserve_online():
            return f"«{self.reserve_peer_name()}» не на связи"
        return ""

    # ── С9: резерв не на связи — вернуть пользователей без него ─────────────
    def reserve_force_check(self) -> str:
        """Пусто — рычаг «вернуть без резерва» возможен: мои пользователи у резерва, а он
        не на связи дольше FORCE_OFFLINE_S."""
        if self.reserve_out() != "away":
            return "пользователи не у резерва"
        if self.reserve_online():
            return f"«{self.reserve_peer_name()}» на связи — верни пользователей обычным путём"
        since = self.reserve_down_since()
        try:
            if not since or timeutil.elapsed(timeutil.parse_iso(since)) < FORCE_OFFLINE_S:
                return f"«{self.reserve_peer_name()}» не на связи меньше {FORCE_OFFLINE_S // 60} мин — подожди"
        except ValueError:
            return "момент потери связи не известен"
        return ""

    def reserve_force_dialog_data(self) -> dict:
        return {"name": self.reserve_peer_name(), "since": self.reserve_down_since(), "record": self.reserve_dns_name(),
                "own": self.reserve_own_ip(), "manual": not self.reserve_dns_automatic(), "jrn": self.reserve_jrn_moved()}

    def reserve_force_home(self) -> list[tuple]:
        """Рычаг: запись на себя (API — если автоматически), deleteWebhook и поллинг;
        в состоянии home с отметкой `await_merge`: реплика резерва вольётся, когда он выйдет на
        связь (его `released` по моему `back_req`)."""
        why = self.reserve_force_check()
        if why:
            raise ServiceError(why)
        from awgbot.domain.notices import reserve as rn
        actions: list[tuple] = []
        own = self.reserve_own_ip()
        if own and self.reserve_dns_automatic():
            try:
                self.reserve_dns_switch(own)
            except ServiceError as e:
                actions.append(("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                                        text=rn.record_not_changed(self.reserve_dns_name(), str(e), own))]))
        self.rsv_update("rsv_out", await_merge=True, force_at=timeutil.now_iso())
        # решения — снова здесь (await_merge): бот на резерве отрезан, его правки вольются при встрече
        self.reserve_epoch_sync()
        log.warning("резерв: возврат без резерва «%s» — запись на себя, забираю Telegram", self.reserve_peer_name())
        return actions + [("take_home",)]

    def _tick_await_merge(self, out: dict) -> list[tuple]:
        """В состоянии home после возврата без резерва: он вышел на связь и всё ещё несёт мой бот —
        обычный возврат с шага back_req (слияние, рестарт, запись уже на мне)."""
        peer = self.reserve_peer_state()
        if not self.reserve_online():
            return []
        if str(peer.get("inn") or "idle") not in ("taking", "serving", "releasing"):
            return self._forced_no_merge()
        with _LOCK:
            self.reserve_set_out("returning", kind=str(out.get("kind") or ""), return_why="force",
                                 return_started_at=timeutil.now_iso(), await_merge=False, step="back_req")
            self._out_step("back_req", RELEASED_WAIT_S)
        log.info("резерв: «%s» снова на связи — сливаю его реплику", self.reserve_peer_name())
        return [("send", "back_req", {}), ("live",)]

    def _forced_no_merge(self) -> list[tuple]:
        """Резерв вернулся пустым (перезагрузка — гость жил в памяти): сливать нечего —
        состояние home, отметка снята, новый K."""
        from awgbot.domain.notices import reserve as rn
        out = self._out()
        self.rsv_update("rsv_out", guest_stopped=True)
        self.reserve_set_out("home", step="", kind="", await_merge=False, last_switch=timeutil.now_iso(),
                             returned_at=str(out.get("returned_at") or timeutil.now_iso()))
        self.reserve_new_k()
        self.reserve_pkg_mark()
        log.warning("резерв: «%s» на связи без моего гостя — реплики не будет", self.reserve_peer_name())
        return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.forced_no_merge(self.reserve_peer_name()))])]

    def reserve_return_start(self, why: str) -> list[tuple]:
        """Начать возврат: returning/back_req → `back_req` резерву."""
        why_not = self.reserve_return_check()
        if why_not:
            raise ServiceError(why_not)
        with _LOCK:
            # шаг — сразу с состоянием: иначе роль на миг сверилась бы с «returning» без шага
            self.reserve_set_out("returning", kind=str(self._out().get("kind") or ""), return_why=why,
                                 return_started_at=timeutil.now_iso(), step="back_req")
            self._out_step("back_req", RELEASED_WAIT_S)
        log.info("резерв: возврат пользователей с «%s» начат (%s)", self.reserve_peer_name(), why)
        return [("send", "back_req", {}), ("live",)]

    def reserve_evict_in(self) -> list[tuple]:
        """`evict` резерва («вернуть пользователей к нему»): возврат, если пользователи у него."""
        try:
            return self.reserve_return_start("evict")
        except ServiceError as e:
            log.info("резерв: evict отклонён: %s", e)
            return []

    def _tick_returning(self, out: dict) -> list[tuple]:
        step = str(out.get("step") or "back_req")
        now = time.monotonic()
        if step == "back_req" and out.get("jrn_back"):
            return self.reserve_jrn_back_tick(out)            # журнал: бот на резерве передал право — догоняю
        if step == "back_req":
            peer_idle = self.reserve_online() and \
                str(self.reserve_peer_state().get("inn") or "idle") not in ("taking", "serving", "releasing")
            if peer_idle and str(out.get("return_why") or "") == "force":
                return self._forced_no_merge()                # взгляд на него был старым: гостя у него уже нет
            if peer_idle and str(self.reserve_peer_state().get("at") or "") >= str(out.get("return_started_at") or ""):
                # резерв потерял зону (перезагрузка): реплики не будет — забрать Telegram и запись без слияния
                log.warning("резерв: «%s» на связи без моего гостя — возврат без реплики", self.reserve_peer_name())
                return self._return_without_replica()
            if self._expired(out):
                # реплика не пришла: повторить просьбу (резерв мог лечь) — не чаще раза в срок
                self._out_step("back_req", RELEASED_WAIT_S)
                return [("send", "back_req", {})]
            return []
        if step == "restart":
            if livestep.mono_due(out.get("restart_at"), now, 60):   # один перезапуск, повтор не раньше минуты
                self.rsv_update("rsv_out", restart_at=now)
                return [("restart",)]
            return []
        if step == "take_tg":
            tries = int(out.get("take_tg_tries") or 0) + 1
            self.rsv_update("rsv_out", take_tg_tries=tries)
            if tries == 100 and not out.get("take_tg_told"):        # ~5 минут отказов deleteWebhook — Telegram недоступен
                from awgbot.domain.notices import reserve as rn
                self.rsv_update("rsv_out", take_tg_told=True)
                return [("take_tg",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                                               text=rn.take_tg_stuck(self.reserve_peer_name()))])]
            return [("take_tg",)]
        if step == "record_back":
            if self._expired(out):
                # запись не вернулась в срок (API или факт): пользователей держит пересылка —
                # `back` всё равно, админу Т-РУ11 (запись менять руками)
                from awgbot.domain.notices import reserve as rn
                own = self.reserve_own_ip()
                self._out_step("relay", RELAY_WAIT_S, back_at=timeutil.now_iso(), record_timeout=True, back_resent=time.monotonic())
                return [("send", "back", {}), ("live",),
                        ("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                                 text=rn.record_not_changed(self.reserve_dns_name(), "факт записи не наступил за 30 минут", own))])]
            return self._tick_record(out, self.reserve_own_ip(), "back")
        if step == "relay":
            if self._expired(out):
                return self._return_done([])
            if not out.get("relay_until") and livestep.mono_due(out.get("back_resent"), time.monotonic(), 60):
                self.rsv_update("rsv_out", back_resent=time.monotonic())
                return [("send", "back", {})]                # резерв не ответил relay — повторить
            return []
        if step == "merge" and self._expired(out):
            # слияние не завершилось (рестарт посреди): реплику попросить снова
            self._out_step("back_req", RELEASED_WAIT_S)
            return [("send", "back_req", {})]
        return []

    # ── возврат: события канала ─────────────────────────────────────────────
    def _return_without_replica(self) -> list[tuple]:
        """Реплики нет (резерв потерял гостя, пустой released): слияния нет, пиры и
        блокировки — по своей базе, перезапуск не нужен — сразу забирать Telegram."""
        from awgbot.domain.notices import reserve as rn
        self.rsv_update("rsv_out", guest_stopped=True)       # гостя нет — писать там некому
        # отчёт — тот же вид, что у слияния: «lost» — строки Т-РУ5б (прежде здесь стояло True,
        # и текст Т-РУ5б падал на нём уже после перехода домой — без итога и нового поколения)
        self.rsv_update("rsv_out", merge_report={"lost": [rn.returned_without_replica(self.reserve_peer_name())]})
        self._out_step("take_tg", take_tg_tries=0, take_tg_told=False)
        return [("take_tg",), ("live",)]

    def reserve_released_in(self, blob: bytes) -> list[tuple]:
        """Реплика бота основы с резерва: слияние, пиры, блокировки → restart.
        Пустая реплика — возврат без слияния. Та же реплика второй раз (повтор
        back_req после рестарта посреди слияния) не сливается дважды."""
        import hashlib
        out = self._out()
        if out.get("state") != "returning" or out.get("step") not in ("back_req", "merge"):
            log.info("резерв: released вне возврата — пропущен")
            return []
        # бот основы на резерве остановлен и больше не пишет (guest_stopped); право записи —
        # здесь после слияния: пока оно пишет сырым соединением, задания решений не идут
        self.rsv_update("rsv_out", guest_stopped=True)
        if not blob:
            log.warning("резерв: «%s» отдал пустую реплику — возврат без слияния", self.reserve_peer_name())
            return self._return_without_replica()
        fp = hashlib.sha256(blob).hexdigest()
        if fp == str(out.get("merged_fp") or ""):
            log.info("резерв: реплика уже слита — к перезапуску")
            self._out_step("restart")
            return [("restart",)]
        self._out_step("merge", RELEASED_WAIT_S)
        try:
            report = self._return_merge(blob, int(out.get("gen") or 0))
        except Exception as e:                                # noqa: BLE001
            reason = str(e)[:200]
            same = reason == str(out.get("merge_error") or "")
            log.error("резерв: слияние не удалось: %s — пользователи остаются у резерва", e)
            self.reserve_set_out("away", kind=str(out.get("kind") or ""), step="", merge_error=reason,
                                 merge_blocked=reason if same else "",
                                 healthy_mono=0, healthy_since="", can_return=False, last_switch=timeutil.now_iso())
            from awgbot.domain.notices import reserve as rn
            # повтор той же причины — молча (громко один раз), автоматический возврат дальше не идёт
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=not same, silent=same,
                                             text=rn.return_failed(self.reserve_peer_name(), reason, final=same))])]
        self.rsv_update("rsv_out", merge_report=report, merged_fp=fp, merge_error="", merge_blocked="")
        self.reserve_monthly_settle()
        self._out_step("restart", restart_at=0)
        return [("restart",)]

    def reserve_return_after_restart(self) -> bool:
        """На старте: возврат на шаге restart — пора забирать Telegram; на шаге
        merge (рестарт посреди слияния) — реплику попросить снова."""
        out = self._out()
        if out.get("state") == "returning" and out.get("step") in ("restart", "take_tg"):
            self._out_step("take_tg")
            return True
        if out.get("state") == "returning" and out.get("step") == "merge":
            self._out_step("back_req", 1)                    # такт повторит back_req сразу
        return False

    def reserve_tg_taken_mark(self) -> list[tuple]:
        """Telegram снова здесь (deleteWebhook прошёл, поллинг идёт): `taken` и запись на себя."""
        out = self._out()
        if out.get("state") != "returning":
            return []
        self.reserve_set_poll("mine")
        self._out_step("record_back", RECORD_WAIT_S, record_set_at="", record_probe=0)
        return [("send", "taken", {}), ("live",)]

    def reserve_relay_in(self, until: str) -> list[tuple]:
        out = self._out()
        if out.get("state") != "returning":
            return []
        self.rsv_update("rsv_out", relay_until=str(until or ""))
        return [("live",)]

    def reserve_wiped_in(self) -> list[tuple]:
        """`wiped`: резерв снял хранилище и прежний пакет — новый `K`, новый пакет, домой."""
        out = self._out()
        if out.get("state") != "returning":
            return []
        return self._return_done([])

    def _return_done(self, actions: list[tuple]) -> list[tuple]:
        from awgbot.domain.notices import reserve as rn
        out = self._out()
        name = self.reserve_peer_name()
        self.reserve_new_k()
        self.reserve_pkg_mark()
        self.rsv_put("rsv_shares", None)
        if hasattr(self, "_jrn_sess_reset"):
            self._jrn_sess_reset()                            # журнал резерв снял — новый снимок под новым K
            self.rsv_put("rsv_jrn_out", None)
        self.reserve_set_out("home", step="", kind="", relay_until=str(out.get("relay_until") or ""),
                             returned_at=timeutil.now_iso(), last_switch=timeutil.now_iso(),
                             back_fact_at=str(out.get("record_fact_at") or timeutil.now_iso()), notified=False,
                             merge_report=dict(out.get("merge_report") or {}), go_sent=False, await_merge=False,
                             merged_fp="", merge_error="", merge_blocked="")
        self.db.set_state("move_notified", "")              # «переподключи VPN» — заново на следующий переезд
        tr = self._reserve_traffic_view().get("out") or ""
        notes = [Notification(tg_id=int(config.ADMIN_ID), text=rn.returned_home(name, dict(out.get("merge_report") or {}), tr))]
        if hasattr(self, "reserve_tail_notes"):
            notes += self.reserve_tail_notes()                # Т-Ж4/Т-Ж5 — мой хвост после аварии, сразу за Т-РУ5б
        # чем открыт пакет — по СВОЕЙ отметке: K отдан мной (`go`) — прямая цела; иначе
        # резерв собрал M по долям или по коду, и слово резерва тут не спрашивается
        kind = str(out.get("kind") or "")
        own_go = out.get("go_sent")
        if own_go is None:                                    # отметки нет (состояние до этой линии) — по виду переезда
            own_go = kind in ("button", "rf", "nogw")
        by = "k" if own_go else "share"
        if by in ("share", "code"):
            # процесс открытия держал вторую точку прямой (или видел код) и конверт
            # DNS: новое поколение и код (Т-РУ14 — тактом планировщика), просьба сменить токен
            self.reserve_new_generation("возврат после аварийного переезда")
            self.rsv_update("rsv_keys", code_notify_why="возврат после аварийного переезда")
            notes.append(Notification(tg_id=int(config.ADMIN_ID), text=rn.dns_token_rotate(name)))
        log.info("резерв: пользователи и бот вернулись с «%s»", name)
        return actions + [("live",), ("notify", notes)]

    # ── живое сообщение хода (Т-Р10б) ───────────────────────────────────────
