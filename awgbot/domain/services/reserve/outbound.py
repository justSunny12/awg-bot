"""
outbound.py — часть пакета reserve (см. __init__): «мои клиенты» на стороне
основы — переезд кнопкой и возврат.

Автомат в `rsv_out` (`state`: home | leaving | away | returning; `step` —
шаг внутри состояния; сроки шагов — `deadline`). Такт (`reserve_out_tick`,
3 с) двигает автомат и отдаёт действия слою runtime списком кортежей:
`("send", вид, тело)` — по каналу пары, `("release",)` — остановить поллинг,
подтвердить пачку, опрос вне очереди, финальная реплика (`reserve_release_build`),
`("restart",)` — перезапуск бота после слияния, `("take_tg",)` —
`deleteWebhook` и поллинг, `("notify", [...])`, `("live",)` — перерисовать
живое сообщение хода.

Переезд кнопкой: leaving/pkg (свежий пакет → `pkg_ok`) → `go{K, gen}` →
leaving/up (`up` от резерва) → leaving/record (запись → адрес резерва,
факт у авторитетного сервера) → leaving/release → `released{реплика}` →
leaving/released (`taken` от резерва) → away (без `taken` в срок — away и
`wake_poll`: право отвечать решает getWebhookInfo). Пачка обновлений не
подтвердилась — передача откладывается, поллинг обратно. Отказ на любом
шаге — `back` резерву (снять зону) и home; после отправки `go` (`go_sent`) —
ещё новый `K` и пакет. Финальная реплика передачи ложится и снимком слияния
`base-<gen>.db` (с неё стартует гость), а не снимок из пакета.

Зависший вебхук резерва (away по поллингу, запись на собственный адрес,
резерв `inn = idle` или пары нет ≥ STUCK_PAIR_S; так STUCK_IDLE_S подряд) —
`("take_home",)`: `deleteWebhook`, поллинг, home без возврата, новый `K`.

Возврат без резерва (С9, рычаг админа): away, резерв не на связи ≥
FORCE_OFFLINE_S — запись на свой адрес (API, если провайдер не «вручную»),
`await_merge` и тот же `("take_home",)`, но home без нового `K`. Резерв вышел
на связь и всё ещё несёт гостя — returning/back_req (`return_why = force`) и
дальше обычный возврат; вышел пустым — отметка снята, новый `K`, пакет и
уведомление «не вольются». Пересылки хвоста до его выхода на связь нет.

Сроки шагов и отметки проб — monotonic в базе; отметка прежней загрузки
хоста считается вышедшей (util/livestep `mono_*`).

Возврат: away (здоров ≥ 5 мин: свой AWG, линк пары, шлюз) → по рычагу
«сразу» сам, «по кнопке» — Т-РУ6 через бот основы на резерве → returning/
back_req → `released` (реплика) → слияние, пиры, блокировки → restart →
take_tg (`deleteWebhook`, поллинг, `taken`) → record_back (запись → свой
адрес, факт) → `back` → relay (`relay{until}`, `wiped`) → новый `K`, пакет →
home. Повторы: `back_req` по сроку (резерв на повтор отдаёт реплику из
rep.bin), merge по сроку и после рестарта посреди — снова back_req,
record_back без факта 30 мин — `back` всё равно и Т-РУ11, `back` раз в минуту
до `relay`. Ошибка слияния — away со сбросом здоровья и `last_switch`
(анти-дребезг). Поколение главного ключа после возврата — по своей отметке
`go_sent` (K отдан мной — прямая цела), не по слову резерва. Подробности —
в докстрингах шагов.
"""
from __future__ import annotations

import json
import os
import time

from awgbot.core import config, settings
from awgbot.util import livestep, timeutil
from awgbot.util.rsvpkg import bot as pkgbot, seal

from ..types import Notification, ServiceError
from ..gateway_link.common import log
from .state import _LOCK

PKG_WAIT_S = 120          # свежий пакет до `go`
UP_WAIT_S = 300           # резерв поднял клон
RECORD_WAIT_S = 1800      # факт записи
RELEASED_WAIT_S = 300     # `taken` после `released`; возврат: `released` после `back_req`
FORCE_OFFLINE_S = 10 * 60        # «вернуть без резерва» — не раньше, чем он не на связи столько
STUCK_IDLE_S = 10 * 60           # столько подряд запись на мне, а резерв клиентов не несёт — вебхук зависший
STUCK_PAIR_S = 30 * 60           # пары нет на связи столько, запись на мне — вебхук резерва зависший
HEALTHY_S = 300           # «здоров» — столько подряд
RELAY_WAIT_S = 600        # `wiped` после `back`
MOVE_STEPS = ("pkg", "up", "record", "release", "released")
RETURN_STEPS = ("back_req", "merge", "restart", "take_tg", "record_back", "relay")


def livestep_mono_sane() -> float:
    return float(livestep.MONO_SANE_S)


class ReserveOutboundMixin:
    # ── общее ────────────────────────────────────────────────────────────────
    def _out(self) -> dict:
        return self.rsv_get("rsv_out")

    def _out_step(self, step: str, wait_s: int = 0, **fields) -> dict:
        data = {"step": step, "step_at": timeutil.now_iso(), **fields}
        if wait_s:
            data["deadline"] = time.monotonic() + wait_s
        return self.rsv_update("rsv_out", **data)

    @staticmethod
    def _expired(out: dict) -> bool:
        """Срок шага вышел. Отметка из прежней загрузки хоста (monotonic начался
        заново) — срок считается вышедшим: шаг повторится или сорвётся честно."""
        dl = out.get("deadline")
        if not dl:
            return False
        now = time.monotonic()
        return float(dl) > now + livestep_mono_sane() or now > float(dl)

    def reserve_move_gen(self) -> int:
        return int(self._out().get("gen") or 0)

    # ── переезд кнопкой ─────────────────────────────────────────────────────
    def reserve_move_check(self) -> str:
        """Пусто — переезд кнопкой возможен; иначе причина (строка карточки)."""
        if not self.reserve_paired():
            return "резерва нет"
        if self.reserve_out() != "home":
            return "клиенты уже не на этом сервере"
        if self.reserve_hosting():
            return f"сейчас здесь клиенты «{self.reserve_peer_name()}» — сначала их возврат"
        if not self.reserve_online():
            return f"«{self.reserve_peer_name()}» не на связи"
        ok, why = self.reserve_directions()["out"]
        if not ok:
            return why
        if not self.reserve_dns_name():
            return "адрес для клиентов — IP, записи DNS нет: переезд невозможен"
        if not self.reserve_peer_ip():
            return f"адрес «{self.reserve_peer_name()}» не определился"
        if settings.get_bool("app.reserve.ready", False) is False:
            return "«Готов быть резервом» выключен"
        return ""

    def reserve_move_dialog_data(self) -> dict:
        return {"name": self.reserve_peer_name(), "record": self.reserve_dns_name(), "target": self.reserve_peer_ip(),
                "manual": not self.reserve_dns_automatic(), "own": self.reserve_own_ip()}

    def reserve_move_start(self, chat_id: int = 0, message_id: int = 0, kind: str = "button", dead_since: str = "") -> dict:
        """«🔀 Перевести»: leaving/pkg, свежий пакет вне очереди. По просьбе
        резерва (случаи 3 и Н3) — тот же путь с видом rf | nogw."""
        why = self.reserve_move_check()
        if why:
            raise ServiceError(why)
        target_ip = self.reserve_peer_ip()                  # резолв имени — до замка
        with _LOCK:
            out = self.reserve_set_out("leaving", kind=str(kind or "button"), dead_since=str(dead_since or ""), started_at=timeutil.now_iso(),
                                       msg=[int(chat_id), int(message_id)] if chat_id else [],
                                       target_ip=target_ip, record=self.reserve_dns_name(), go_sent=False,
                                       pkg_at="", up_at="", record_set_at="", record_fact_at="", released_at="",
                                       taken_at="", reason="", gen=0)
            self._out_step("pkg", PKG_WAIT_S)
            self.reserve_pkg_mark()
        log.info("резерв: переезд кнопкой на «%s» начат", self.reserve_peer_name())
        return out

    def _move_fail(self, reason: str, released: bool = False) -> list[tuple]:
        """Переезд не состоялся: домой, резерву — `back` (снять зону, если он
        её поднял или ещё поднимает), запись — обратно на себя, если успела
        смениться, итог админу."""
        from awgbot.domain.notices import reserve as rn
        name = self.reserve_peer_name()
        out = self._out()
        if out.get("go_sent"):
            # K ушёл резерву — он его знает: новый K и свежий пакет под ним (после `back` резерв
            # сносит прежний пакет и долю, досылка — по отметке)
            self.reserve_new_k()
            self.reserve_pkg_mark()
        record_back = ""
        if out.get("record_set_at") and self.reserve_dns_automatic():
            own = self.reserve_own_ip()
            try:
                if own:
                    self.reserve_dns_switch(own)
            except ServiceError as e:
                record_back = str(e)
        self.reserve_set_out("home", reason=reason, failed_at=timeutil.now_iso(), step="")
        log.warning("резерв: переезд на «%s» не состоялся: %s", name, reason)
        notes = [Notification(tg_id=int(config.ADMIN_ID), text=rn.move_failed(name, reason, released=released))]
        if record_back:
            notes.append(Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                      text=rn.record_not_changed(self.reserve_dns_name(), record_back, self.reserve_own_ip())))
        actions: list[tuple] = [("live",), ("notify", notes)]
        if released:
            actions.insert(0, ("send", "back", {}))
        return actions

    def reserve_out_tick(self) -> list[tuple]:
        """Такт автомата «мои клиенты»."""
        if not self.reserve_paired():
            return []
        out = self._out()
        state = str(out.get("state") or "home")
        if state == "leaving":
            return self._tick_leaving(out)
        if state == "away":
            return self._tick_away(out)
        if state == "returning":
            return self._tick_returning(out)
        return self._tick_home(out)

    def _tick_home(self, out: dict) -> list[tuple]:
        """Дома: факт «клиенты у резерва» мимо меня (переезд по взглядам, пока я
        лежал) — `in = serving` у резерва → away."""
        peer = self.reserve_peer_state()
        if out.get("await_merge"):
            return self._tick_await_merge(out)
        fresh = str(peer.get("at") or "") >= str(out.get("since") or "")   # st получен после моего перехода домой
        if str(peer.get("inn") or "") == "serving" and self.reserve_online() and fresh:
            # переезд прошёл мимо меня — пакет открыт не моим K (своего `go` не было): по долям или
            # по коду; слово резерва (in_by) здесь не слушаем — поколение после возврата сменится
            self.reserve_set_out("away", kind=str(peer.get("in_kind") or "dead"), step="", taken_at=timeutil.now_iso(),
                                 by="share", go_sent=False, gen=int((peer.get("pkg") or {}).get("gen") or 0),
                                 last_switch=timeutil.now_iso(), healthy_mono=0, healthy_since="", can_return=False)
            self.reserve_set_poll("away")
            log.warning("резерв: «%s» несёт моих клиентов — я в состоянии away", self.reserve_peer_name())
            return [("live",)]
        return []

    def _tick_leaving(self, out: dict) -> list[tuple]:
        step = str(out.get("step") or "pkg")
        if self._expired(out) and step not in ("released",):
            why = {"pkg": "данные для переезда не подтверждены резервом", "up": f"«{self.reserve_peer_name()}» не поднял клиентов",
                   "record": "запись не изменена за 30 минут", "release": "передача бота не завершилась"}.get(step, "срок шага вышел")
            # резерву — `back` и на шаге подъёма: его процесс открытия мог кончиться позже срока
            return self._move_fail(why, released=step in ("up", "record", "release"))
        if step == "pkg":
            sent = self.rsv_get("rsv_pkg_sent")
            if sent.get("gen") and str(sent.get("at") or "") >= str(out.get("started_at") or ""):
                self._out_step("up", UP_WAIT_S, gen=int(sent["gen"]), pkg_at=str(sent.get("at") or ""))
                self.rsv_update("rsv_out", go_sent=True)             # K отдан — своя отметка, не слово резерва
                return [("send", "go", {"k": self.reserve_k().hex(), "gen": int(sent["gen"]),
                                        "kind": str(out.get("kind") or "button")}), ("live",)]
            return []
        if step == "record":
            return self._tick_record(out, str(out.get("target_ip") or ""), "release")
        if step == "release":
            # передача бота: действие выдаётся снова, пока не прошло (сбой подтверждения пачки,
            # реплики или отправки — не повод срывать переезд через 5 минут)
            now = time.monotonic()
            if livestep.mono_due(out.get("release_try"), now, 30):
                self.rsv_update("rsv_out", release_try=now)
                return [("release",)]
            return []
        if step == "released" and self._expired(out):
            # резерв не ответил `taken` — право отвечать решает Telegram, не моя отметка:
            # разбудить поллинг (getWebhookInfo) и решать по его слову, записанному после released
            from awgbot.domain.notices import reserve as rn
            poll = self.rsv_get("rsv_poll")
            fresh = str(poll.get("since") or "") >= str(out.get("released_at") or "")
            verdict = str(poll.get("why") or "") if fresh else ""
            if verdict in ("webhook", "webhook_info"):          # Telegram: вебхук стоит — клиенты у резерва
                self.reserve_set_out("away", kind=str(out.get("kind") or "button"), step="", taken_at=timeutil.now_iso(),
                                     last_switch=timeutil.now_iso(), healthy_since="")
                log.info("резерв: `taken` не пришёл, но вебхук у «%s» стоит — away", self.reserve_peer_name())
                return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.moved_to_reserve(
                    self.reserve_peer_name(), str(out.get("kind") or "button"), timeutil.now_iso(),
                    dead_since=str(out.get("dead_since") or ""), reserve_ip=str(out.get("target_ip") or "")))])]
            if fresh and poll.get("state") == "mine":           # Telegram: вебхука нет — переезд не состоялся
                return self._move_fail("резерв не взял право отвечать — вебхук не поставлен", released=True)
            woken = int(out.get("poll_woken") or 0)
            if woken < 3:                                       # слова Telegram ещё нет — разбудить и подождать
                self.rsv_update("rsv_out", poll_woken=woken + 1)
                self._out_step("released", 120, released_at=str(out.get("released_at") or ""))
                return [("wake_poll",)]
            return self._move_fail("право отвечать не проверено — Telegram не ответил", released=True)
        return []

    def _tick_record(self, out: dict, target: str, next_step: str) -> list[tuple]:
        """Шаг записи: сменить (API) один раз, дальше ждать факта у авторитетного сервера."""
        from awgbot.domain.notices import reserve as rn
        actions: list[tuple] = []
        if not out.get("record_set_at"):
            if self.reserve_dns_automatic():
                try:
                    self.reserve_dns_switch(target)
                except ServiceError as e:
                    if out.get("state") == "leaving":
                        return self._move_fail(f"запись не изменена — {e}", released=True)
                    self.rsv_update("rsv_out", record_error=str(e))
                    actions.append(("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                                            text=rn.record_not_changed(self.reserve_dns_name(), str(e), target))]))
            self.rsv_update("rsv_out", record_set_at=timeutil.now_iso())
            actions.append(("live",))
        if not livestep.mono_due(out.get("record_probe"), time.monotonic(), 20):
            return actions
        self.rsv_update("rsv_out", record_probe=time.monotonic())
        if self.reserve_dns_points_to(target):
            self._out_step(next_step, RELEASED_WAIT_S if next_step == "release" else 0, record_fact_at=timeutil.now_iso(),
                           release_try=time.monotonic())
            actions += [("live",), ("release",) if next_step == "release" else ("send", "back", {})]
            if next_step == "back":
                self._out_step("relay", RELAY_WAIT_S, back_at=timeutil.now_iso(), back_resent=time.monotonic())
        return actions

    # ── события канала (переезд) ────────────────────────────────────────────
    def reserve_up_in(self) -> list[tuple]:
        out = self._out()
        if out.get("state") != "leaving" or out.get("step") != "up":
            return []
        self._out_step("record", RECORD_WAIT_S, up_at=timeutil.now_iso())
        return [("live",)]

    def reserve_down_in(self, reason: str) -> list[tuple]:
        out = self._out()
        if out.get("state") == "leaving":
            return self._move_fail(f"«{self.reserve_peer_name()}» не поднял клиентов — {reason}")
        return []

    def reserve_release_build(self) -> dict | None:
        """После остановки поллинга и опроса вне очереди: финальная реплика под
        `K` — {"gen", "blob": hex}; шаг released."""
        out = self._out()
        if out.get("state") != "leaving" or out.get("step") != "release":
            return None
        db_bytes = pkgbot.replica(self._db_path())
        try:
            pkgbot.assert_clean(db_bytes)
        except pkgbot.BotPartError as e:
            log.warning("резерв: финальная реплика не прошла сторожа: %s", e)
            return None
        blob = seal.snapshot_seal(self.reserve_k(), {"pkg_gen": int(out.get("gen") or 0), "at": timeutil.now_iso(),
                                                     "db": db_bytes.hex()})
        # снимок для слияния при возврате — именно эта реплика: с неё стартует гость, и всё,
        # что было записано здесь между пакетом и передачей, не удвоится и не станет «чужим»
        try:
            self._pkg_store_base(int(out.get("gen") or 0), db_bytes)
        except OSError as e:
            log.warning("резерв: снимок финальной реплики не сохранён: %s", e)
        self._out_step("released", RELEASED_WAIT_S, released_at=timeutil.now_iso(), poll_woken=0)
        self.reserve_set_poll("away", why="released")           # своя отметка; слово Telegram — webhook/webhook_info
        return {"gen": int(out.get("gen") or 0), "blob": blob.hex()}

    def reserve_taken_in(self) -> list[tuple]:
        """`taken` от резерва: бот основы на резерве отвечает в Telegram."""
        from awgbot.domain.notices import reserve as rn
        out = self._out()
        if out.get("state") != "leaving":
            return []
        self.reserve_set_out("away", kind=str(out.get("kind") or "button"), step="", taken_at=timeutil.now_iso(),
                             last_switch=timeutil.now_iso(), healthy_since="")
        self.reserve_set_poll("away")
        log.info("резерв: клиенты и бот — на «%s»", self.reserve_peer_name())
        return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.moved_to_reserve(
            self.reserve_peer_name(), str(out.get("kind") or "button"), timeutil.now_iso(),
            dead_since=str(out.get("dead_since") or ""), reserve_ip=str(out.get("target_ip") or "")))])]

    # ── away: здоровье и возврат ────────────────────────────────────────────
    def reserve_healthy(self) -> bool:
        """Свой AWG отвечает, линк пары жив, шлюз (если есть) на связи — сейчас."""
        try:
            awg_ok = str(self.db.get_state("last_server_ok") or "") == "1"
        except Exception:                                     # noqa: BLE001
            awg_ok = False
        pair_ok = self.reserve_online() and self.reserve_peer_seen() == "ok"
        gw_ok = True
        if self.reserve_has_gateway() and hasattr(self, "gwlink_session"):
            gw_ok = any(bool(self.gwlink_session(g.id)) for g in self.db.gateways())
        return bool(awg_ok and pair_ok and gw_ok and self._tg_reachable())

    def _tg_reachable(self) -> bool:
        """Telegram достижим с этого сервера (TCP к api.telegram.org:443, кэш минуту):
        возврат без Telegram оставил бы бота немым обеим сторонам."""
        import socket
        cache = self.__dict__.setdefault("_tg_reach", {})
        now = time.monotonic()
        if cache.get("at") and now - cache["at"] < 60:
            return bool(cache.get("ok"))
        ok = False
        try:
            with socket.create_connection(("api.telegram.org", 443), timeout=3):
                ok = True
        except OSError:
            ok = False
        cache.update(at=now, ok=ok)
        return ok

    def _tick_away(self, out: dict) -> list[tuple]:
        now = time.monotonic()
        if self.reserve_poll() == "away" and self._webhook_stuck(out, now):
            return [("take_home",)]
        if self.reserve_healthy():
            since = float(out.get("healthy_mono") or 0)
            if not since:
                self.rsv_update("rsv_out", healthy_mono=now, healthy_since=timeutil.now_iso())
                return []
            if now - since < HEALTHY_S:
                return []
            if out.get("merge_blocked"):
                return []                                     # слияние дважды упало с той же причиной — только по кнопке
            if str(settings.get("app.reserve.return", "immediate") or "immediate") == "immediate":
                last = str(out.get("last_switch") or "")
                gap = settings.get_int("app.reserve.min_interval_minutes", 30) * 60
                if not last or timeutil.elapsed(timeutil.parse_iso(last)) >= gap:
                    return self.reserve_return_start("auto")
            elif not out.get("can_return"):
                self.rsv_update("rsv_out", can_return=True)
                return [("live",)]
        elif out.get("healthy_mono") or out.get("can_return"):
            self.rsv_update("rsv_out", healthy_mono=0, healthy_since="", can_return=False)
        return []

    def _webhook_stuck(self, out: dict, now: float) -> bool:
        """Право отвечать осталось у вебхука резерва, хотя клиентов он не несёт:
        запись указывает на меня (проба не чаще раза в 2 мин), а резерв говорит
        `in = idle` (или пары нет на связи дольше STUCK_PAIR_S). Возврат оборвался
        после передачи бота — снять вебхук и поллить самому."""
        if not livestep.mono_due(out.get("stuck_probe"), now, 120):
            return False
        self.rsv_update("rsv_out", stuck_probe=now)
        own = self.reserve_own_ip()
        stuck = False
        if own and self.reserve_dns_points_to(own):
            peer = self.reserve_peer_state()
            if self.reserve_online():
                stuck = str(peer.get("inn") or "idle") == "idle"
            else:
                since = str(peer.get("down_since") or "")
                try:
                    stuck = bool(since) and timeutil.elapsed(timeutil.parse_iso(since)) >= STUCK_PAIR_S
                except ValueError:
                    stuck = False
        if not stuck:
            if out.get("stuck_since"):
                self.rsv_update("rsv_out", stuck_since="")
            return False
        since_iso = str(out.get("stuck_since") or "")
        if not since_iso:
            self.rsv_update("rsv_out", stuck_since=timeutil.now_iso())
            return False
        try:
            return timeutil.elapsed(timeutil.parse_iso(since_iso)) >= STUCK_IDLE_S
        except ValueError:
            return False

    def reserve_stuck_home(self) -> list[tuple]:
        """deleteWebhook прошёл (take): клиенты и так здесь — домой без возврата. По
        рычагу «без резерва» (await_merge) — домой с ожиданием его реплики."""
        from awgbot.domain.notices import reserve as rn
        name = self.reserve_peer_name()
        out = self._out()
        forced = bool(out.get("await_merge"))
        self.reserve_set_poll("mine")
        self.reserve_set_out("home", step="", kind=str(out.get("kind") or "") if forced else "",
                             stuck_home_at=timeutil.now_iso(), await_merge=forced, last_switch=timeutil.now_iso())
        if not forced:
            self.reserve_new_k()
            self.reserve_pkg_mark()
            log.warning("резерв: вебхук «%s» снят — запись на мне, клиентов он не нёс; поллю сам", name)
            return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.webhook_stuck_removed(name))])]
        log.warning("резерв: клиенты возвращены без «%s» — его реплика вольётся, когда он выйдет на связь", name)
        return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.forced_home(name))])]

    def reserve_can_return(self) -> bool:
        out = self._out()
        return str(out.get("state") or "") == "away" and bool(out.get("can_return"))

    def reserve_return_dialog_data(self) -> dict:
        """Т-Р11б у основы: имя резерва, запись и свой адрес."""
        return {"name": self.reserve_peer_name(), "record": self.reserve_dns_name(), "own": self.reserve_own_ip()}

    def reserve_return_check(self) -> str:
        if self.reserve_out() != "away":
            return "клиенты не у резерва"
        if not self.reserve_online():
            return f"«{self.reserve_peer_name()}» не на связи"
        return ""

    # ── С9: резерв не на связи — вернуть клиентов без него ──────────────────
    def reserve_force_check(self) -> str:
        """Пусто — рычаг «вернуть без резерва» возможен: мои клиенты у резерва, а он
        не на связи дольше FORCE_OFFLINE_S."""
        if self.reserve_out() != "away":
            return "клиенты не у резерва"
        if self.reserve_online():
            return f"«{self.reserve_peer_name()}» на связи — верни клиентов обычным путём"
        since = self.reserve_down_since()
        try:
            if not since or timeutil.elapsed(timeutil.parse_iso(since)) < FORCE_OFFLINE_S:
                return f"«{self.reserve_peer_name()}» не на связи меньше {FORCE_OFFLINE_S // 60} мин — подожди"
        except ValueError:
            return "момент потери связи не известен"
        return ""

    def reserve_force_dialog_data(self) -> dict:
        return {"name": self.reserve_peer_name(), "since": self.reserve_down_since(), "record": self.reserve_dns_name(),
                "own": self.reserve_own_ip(), "manual": not self.reserve_dns_automatic()}

    def reserve_force_home(self) -> list[tuple]:
        """Рычаг: запись на себя (API — если автоматически), deleteWebhook и поллинг;
        дома с отметкой `await_merge`: реплика резерва вольётся, когда он выйдет на
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
        log.warning("резерв: возврат без резерва «%s» — запись на себя, забираю Telegram", self.reserve_peer_name())
        return actions + [("take_home",)]

    def _tick_await_merge(self, out: dict) -> list[tuple]:
        """Дома после возврата без резерва: он вышел на связь и всё ещё несёт мой бот —
        обычный возврат с шага back_req (слияние, рестарт, запись уже на мне)."""
        peer = self.reserve_peer_state()
        if not self.reserve_online():
            return []
        if str(peer.get("inn") or "idle") not in ("taking", "serving", "releasing"):
            return self._forced_no_merge()
        with _LOCK:
            self.reserve_set_out("returning", kind=str(out.get("kind") or ""), return_why="force",
                                 return_started_at=timeutil.now_iso(), await_merge=False)
            self._out_step("back_req", RELEASED_WAIT_S)
        log.info("резерв: «%s» снова на связи — сливаю его реплику", self.reserve_peer_name())
        return [("send", "back_req", {}), ("live",)]

    def _forced_no_merge(self) -> list[tuple]:
        """Резерв вернулся пустым (перезагрузка — гость жил в памяти): сливать нечего —
        дома, отметка снята, новый K."""
        from awgbot.domain.notices import reserve as rn
        out = self._out()
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
            self.reserve_set_out("returning", kind=str(self._out().get("kind") or ""), return_why=why,
                                 return_started_at=timeutil.now_iso())
            self._out_step("back_req", RELEASED_WAIT_S)
        log.info("резерв: возврат клиентов с «%s» начат (%s)", self.reserve_peer_name(), why)
        return [("send", "back_req", {}), ("live",)]

    def reserve_evict_in(self) -> list[tuple]:
        """`evict` резерва («вернуть клиентов к нему»): возврат, если клиенты у него."""
        try:
            return self.reserve_return_start("evict")
        except ServiceError as e:
            log.info("резерв: evict отклонён: %s", e)
            return []

    def _tick_returning(self, out: dict) -> list[tuple]:
        step = str(out.get("step") or "back_req")
        now = time.monotonic()
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
                # запись не вернулась в срок (API или факт): клиентов держит пересылка —
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
        self.rsv_update("rsv_out", merge_report={"lost": True})
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
            log.error("резерв: слияние не удалось: %s — клиенты остаются у резерва", e)
            self.reserve_set_out("away", kind=str(out.get("kind") or ""), step="", merge_error=reason,
                                 merge_blocked=reason if same else "",
                                 healthy_mono=0, healthy_since="", can_return=False, last_switch=timeutil.now_iso())
            from awgbot.domain.notices import reserve as rn
            # повтор той же причины — молча (громко один раз), автоматический возврат дальше не идёт
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=not same, silent=same,
                                             text=rn.return_failed(self.reserve_peer_name(), reason, final=same))])]
        self.rsv_update("rsv_out", merge_report=report, merged_fp=fp, merge_error="", merge_blocked="")
        self._out_step("restart", restart_at=0)
        return [("restart",)]

    def _return_merge(self, blob: bytes, gen: int) -> dict:
        from awgbot.domain.backup.merge import reserve as merge_mod
        from awgbot.infra import awg
        snap = seal.snapshot_open(self.reserve_k(), blob)
        if not isinstance(snap, dict) or not snap.get("db"):
            raise ServiceError("реплика не того вида")
        base_dir = os.path.join(str(config.DATA_DIR), "reserve", "out")
        snap_gen = int(snap.get("pkg_gen") or 0)             # поколение пакета, с которого стартовал гость
        if snap_gen and (not gen or not os.path.exists(os.path.join(base_dir, f"base-{gen}.db"))):
            gen = snap_gen
        base = os.path.join(base_dir, f"base-{gen}.db")
        if not os.path.exists(base):
            raise ServiceError(f"нет снимка base-{gen}.db")
        rep = os.path.join(base_dir, f"rep-{gen}.db")
        with open(rep, "wb") as f:
            f.write(bytes.fromhex(str(snap["db"])))
        os.chmod(rep, 0o600)
        try:
            removed = self._pubkeys_removed(base, rep)
            before = self._merge_watch_snapshot(base)
            rpt = merge_mod.merge(self._db_path(), base, rep, admin_tg=int(config.ADMIN_ID))
            warnings = self._merge_watch_lines(before)
            # пиры — до сверки: устройства с резерва должны попасть в конфиг раньше,
            # чем сверка посчитает их пропавшими
            added = 0
            conf_pubs = {p["public_key"] for p in awg.show_dump(config.AWG_INTERFACE)}
            for d in self.db.list_all_devices():
                if d.public_key and d.public_key not in conf_pubs and not getattr(d, "is_gateway", 0):
                    try:
                        awg.add_peer(d.public_key, d.preshared_key or "", d.address, awg.iface_of(d.iface))
                        added += 1
                    except Exception as e:                    # noqa: BLE001
                        log.warning("резерв: пир %s не добавлен: %s", d.name, e)
            for pub in removed:
                try:
                    awg.remove_peer(pub)
                except Exception as e:                        # noqa: BLE001
                    log.warning("резерв: пир снят с ошибкой: %s", e)
            try:
                self.reconcile_blocks(True)
            except Exception as e:                            # noqa: BLE001
                log.warning("резерв: блокировки после слияния: %s", e)
            self._return_traffic(base, rep)
            report = {"clients": int(rpt.inserted.get("clients", 0)), "devices": int(rpt.inserted.get("devices", 0)),
                      "extensions": int(rpt.updated.get("client_subscription", 0)),
                      "blocks": int(rpt.updated.get("client_block", 0) + rpt.updated.get("devices", 0) * 0),
                      "peers_added": added, "peers_removed": len(removed),
                      "lost": [self._lost_line(x) for x in rpt.lost[:5]] + warnings[:5]}
            log.info("резерв: слияние: %s", json.dumps(report, ensure_ascii=False))
            return report
        finally:
            try:
                os.unlink(rep)
            except OSError:
                pass

    @staticmethod
    def _merge_watch_snapshot(base: str) -> dict:
        """Что в снимке стоило отдельного взгляда: Telegram у профилей, устройства
        профиля админа — правки этого с резерва не должны пройти незаметно."""
        import sqlite3
        con = sqlite3.connect(base)
        try:
            tg = {int(r[0]): (r[1], r[2]) for r in con.execute("SELECT id, tg_id, name FROM clients")}
            dev = {int(r[0]) for r in con.execute("SELECT id FROM devices")}
        except sqlite3.Error:
            return {}
        finally:
            con.close()
        return {"tg": tg, "dev": dev}

    def _merge_watch_lines(self, before: dict) -> list[str]:
        """Строки Т-РУ5б: «сменён Telegram у профиля …» (в снимке был непустой),
        «новое устройство у профиля админа …»."""
        if not before:
            return []
        lines: list[str] = []
        try:
            for c in self.db.list_clients() if hasattr(self.db, "list_clients") else []:
                old = before["tg"].get(int(c.id))
                if old and old[0] and c.tg_id != old[0]:
                    lines.append(f"⚠️ У профиля «{c.name}» на «{self.reserve_peer_name()}» сменён Telegram-аккаунт "
                                 f"({old[0]} → {c.tg_id}) — проверь, что это сделал ты")
            admin = self.db.get_client_by_tg(int(config.ADMIN_ID)) if hasattr(self.db, "get_client_by_tg") else None
            if admin is not None:
                for d in self.db.list_all_devices():
                    if int(getattr(d, "client_id", 0) or 0) == int(admin.id) and int(d.id) not in before["dev"]:
                        lines.append(f"⚠️ У профиля админа появилось устройство «{d.name}», заведённое на "
                                     f"«{self.reserve_peer_name()}» — проверь, что это твоё")
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: сверка правок резерва не прошла: %s", e)
        return lines

    @staticmethod
    def _pubkeys_removed(base: str, rep: str) -> list[str]:
        import sqlite3
        def pubs(path):
            con = sqlite3.connect(path)
            try:
                return {r[0] for r in con.execute("SELECT public_key FROM devices WHERE public_key IS NOT NULL")}
            finally:
                con.close()
        return sorted(pubs(base) - pubs(rep))

    def _lost_line(self, lost: dict) -> str:
        from awgbot.domain.notices import reserve as rn
        return rn.lost_device(str(lost.get("name") or "?"), str(lost.get("owner") or "?"), str(lost.get("taken_by") or "?"),
                              str(lost.get("taken_owner") or "?"), self.reserve_peer_name())

    def _return_traffic(self, base: str, rep: str) -> None:
        """Расход через резерв: сумма дельт счётчиков реплики против снимка."""
        import sqlite3
        try:
            def totals(path):
                con = sqlite3.connect(path)
                try:
                    row = con.execute("SELECT COALESCE(SUM(traffic_rx_month),0), COALESCE(SUM(traffic_tx_month),0) "
                                      "FROM device_traffic").fetchone()
                    return int(row[0]), int(row[1])
                finally:
                    con.close()
            brx, btx = totals(base)
            rrx, rtx = totals(rep)
            self.reserve_traffic_out_add(max(0, rrx - brx), max(0, rtx - btx))
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: расход через резерв не посчитан: %s", e)

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
        self.reserve_set_out("home", step="", kind="", relay_until=str(out.get("relay_until") or ""),
                             returned_at=timeutil.now_iso(), last_switch=timeutil.now_iso(),
                             back_fact_at=str(out.get("record_fact_at") or timeutil.now_iso()), notified=False,
                             merge_report=dict(out.get("merge_report") or {}), go_sent=False, await_merge=False,
                             merged_fp="", merge_error="", merge_blocked="")
        self.db.set_state("move_notified", "")              # «переподключи VPN» — заново на следующий переезд
        tr = self._reserve_traffic_view().get("out") or ""
        notes = [Notification(tg_id=int(config.ADMIN_ID), text=rn.returned_home(name, dict(out.get("merge_report") or {}), tr))]
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
        log.info("резерв: клиенты и бот вернулись с «%s»", name)
        return actions + [("live",), ("notify", notes)]

    # ── живое сообщение хода (Т-Р10б) ───────────────────────────────────────
    def reserve_move_live(self) -> dict:
        out = self._out()
        return {"name": self.reserve_peer_name(), "state": str(out.get("state") or "home"), "step": str(out.get("step") or ""),
                "pkg_at": str(out.get("pkg_at") or ""), "up_at": str(out.get("up_at") or ""),
                "record": str(out.get("record") or self.reserve_dns_name()), "target": str(out.get("target_ip") or ""),
                "record_set_at": str(out.get("record_set_at") or ""), "record_fact_at": str(out.get("record_fact_at") or ""),
                "released_at": str(out.get("released_at") or ""), "taken_at": str(out.get("taken_at") or ""),
                "reason": str(out.get("reason") or ""), "manual": not self.reserve_dns_automatic(),
                "msg": list(out.get("msg") or []), "online": self._online_count(), "relay_until": str(out.get("relay_until") or ""),
                "there": int(self.reserve_peer_state().get("there") or 0),
                "own": self.reserve_own_ip(), "merge_report": dict(out.get("merge_report") or {})}

    def _online_count(self) -> int:
        try:
            return int(self.db.get_state("online_count") or 0)
        except Exception:                                     # noqa: BLE001
            return 0
