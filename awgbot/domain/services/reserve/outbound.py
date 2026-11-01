"""
outbound.py — часть пакета reserve (см. __init__): «мои пользователи» на стороне
основы — автомат, такт и шаги переезда кнопкой, передача бота; проверка, старт,
срыв и слова резерва на переезде — в outbound_move, away и здоровье — в
outbound_health, шаги возврата — в outbound_return, слияние — в merge_ops
(автомат и сроки шагов — общие, здесь).

Автомат в `rsv_out` (`state`: home | leaving | away | returning; `step` —
шаг внутри состояния; сроки шагов — `deadline`). Такт (`reserve_out_tick`,
3 с) двигает автомат и отдаёт действия слою runtime списком кортежей:
`("send", вид, тело)` — по каналу пары, `("release",)` — остановить поллинг,
подтвердить пачку, опрос вне очереди, финальная реплика (`reserve_release_build`),
`("restart",)` — перезапуск бота после слияния, `("take_tg",)` —
`deleteWebhook` и поллинг, `("notify", [...])`, `("live",)` — перерисовать
живое сообщение хода.

Переезд кнопкой: leaving/pkg (свежий пакет → `pkg_ok`) → `go_req{gen}` →
leaving/eph (`open_eph{eph, oid}` от резерва — разовый ключ его процесса
открытия) → `go{sealed, oid}` (`K` конвертом к разовому ключу: бот резерва
видит только шифротекст; резерв без `seal1` — переезд не предлагается) →
leaving/up (`up` от резерва) → leaving/record (запись → адрес резерва,
факт у авторитетного сервера) → leaving/release → `released{реплика}` →
leaving/released (`taken` от резерва) → away. Без `taken` в срок решает
слово Telegram, а не своя отметка: `wake_poll` (getWebhookInfo, до трёх раз
по 2 мин), и только ответ, записанный после released: вебхук у резерва —
away с уведомлением, вебхука нет или Telegram молчит — срыв. Передача бота
(`("release",)`) повторяется раз в 30 с до срока шага. Пачка обновлений не
подтвердилась — передача откладывается, поллинг обратно. Отказ на любом
шаге — `back` резерву (снять зону) и home; запись, если успела смениться, —
обратно на себя (API) или Т-РУ11 (вручную, отказ API); после отправки `go`
(`go_sent`) — ещё новый `K` и пакет. Финальная реплика передачи ложится и
снимком слияния `base-<gen>.db` (с неё стартует гость), а не снимок из
пакета; с журналом (`reserve_jrn_moved`, journal/handover) реплики и базы
слияния нет — `released{epoch, last}` по подтверждённому, гость уже следует и
догонит по `last`. В home факт «пользователи у резерва» мимо меня — живой
`in = serving` в `st`, полученном строго после моего перехода в home (одно
правило с ролью эпохи, journal/epoch `_peer_serving_after_home`).

Зависший вебхук резерва (away по поллингу, запись на собственный адрес,
резерв `inn = idle` или пары нет ≥ STUCK_PAIR_S; так STUCK_IDLE_S подряд) —
`("take_home",)`: `deleteWebhook`, поллинг, home без возврата, новый `K`. Так же
и дома: Telegram сказал «вебхук не здесь» — основа следует (journal/epoch), и
зависший вебхук без гостя держал бы её без права записи бессрочно.

Возврат без резерва (С9, рычаг админа) и обычный возврат — outbound_return.

Сроки шагов и отметки проб — monotonic в базе; отметка прежней загрузки
хоста считается вышедшей (util/livestep `mono_*`).

Away: здоров ≥ HEALTHY_S (свой AWG, линк пары, шлюз, Telegram достижим с
этого сервера) → по рычагу «сразу» возврат сам (не чаще min_interval_minutes;
после повторной той же ошибки слияния — `merge_blocked`, сам больше не
начинается), «по кнопке» — Т-РУ6 через бот основы на резерве. Поколение
главного ключа после возврата — по своей отметке `go_sent` (K отдан мной —
прямая цела), не по слову резерва. Подробности — в докстрингах шагов.
"""
from __future__ import annotations

import time

from awgbot.core import config
from awgbot.util import livestep, timeutil
from awgbot.util.rsvpkg import bot as pkgbot, seal

from ..types import Notification, ServiceError
from ..gateway_link.common import log
from .state import _LOCK

PKG_WAIT_S = 120          # свежий пакет до `go_req`
EPH_WAIT_S = 60           # разовый ключ процесса открытия (`open_eph`) после `go_req`
UP_WAIT_S = 300           # резерв поднял клон
RECORD_WAIT_S = 1800      # факт записи
RECORD_NOIP_TELL_S = 2 * 60       # шаг записи без адреса дольше — Т-РУ11 «свой адрес не определился»
RELEASED_WAIT_S = 300     # `taken` после `released`; возврат: `released` после `back_req`
RELEASED_WAKE_S = 120     # `taken` не пришёл: поллинг разбужен — столько ждать слова Telegram (до трёх раз)
FORCE_OFFLINE_S = 10 * 60        # «вернуть без резерва» — не раньше, чем он не на связи столько
STUCK_IDLE_S = 10 * 60           # столько подряд запись на мне, а резерв пользователей не несёт — вебхук зависший
STUCK_PAIR_S = 30 * 60           # пары нет на связи столько, запись на мне — вебхук резерва зависший
HEALTHY_S = 300           # «здоров» — столько подряд; умолчание reserve.healthy_seconds
RELAY_WAIT_S = 600        # `wiped` после `back`


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
        out = self.rsv_update("rsv_out", **data)
        self.reserve_epoch_sync()                         # шаг меняет право записи (released, merge, …)
        return out

    def _out_step_cas(self, state: str, steps, step: str, wait_s: int = 0, **fields) -> bool:
        """`_out_step`, только если автомат всё ещё в `state` на одном из `steps` (под замком,
        как `_out_cas`): повтор шага по сроку из такта не ложится поверх перехода, который
        за это время сделал обработчик слова канала."""
        with _LOCK:
            if not self._out_is(state, steps):
                return False
            self._out_step(step, wait_s, **fields)
            return True

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

    # ── такт и шаги переезда ────────────────────────────────────────────────
    def reserve_out_tick(self) -> list[tuple]:
        """Такт автомата «мои пользователи»."""
        self.reserve_epoch_check()
        if not self.reserve_paired():
            return []
        out = self._out()
        state = str(out.get("state") or "home")
        if state == "leaving":
            return self._tick_leaving(out)
        if state == "away" and out.get("await_merge"):
            return self._tick_force_take(out)                 # возврат без резерва: deleteWebhook ещё не прошёл
        if state == "away":
            return self._tick_away(out) + self._tick_pult()   # бот на резерве не отвечает, линк молчит — пульт (pult/primary)
        if state == "returning":
            return self._tick_returning(out)
        acts = self._tick_home(out) + self.reserve_tail_settle() + self._home_back_resend()   # свой хвост при держателе дома — переносить
        if self.reserve_out() == "home":
            acts += self.reserve_rec_back_settle()            # резерв не принял моих после смены записи — запись на себя
        notes = self.reserve_c9_settle()                      # отметка С9 без рычага — пока правки бота на резерве возможны
        return acts + ([("notify", notes)] if notes else [])

    def _tick_home(self, out: dict) -> list[tuple]:
        """В состоянии home: после «вернуть без резерва» — ждать его (outbound_return); вебхук
        резерва завис (запись на мне, он моих не несёт — outbound_stuck) — снять его
        (`take_home`); факт «пользователи у резерва» мимо меня (переезд по взглядам, пока я
        лежал) — живой `in = serving` у резерва → away."""
        peer = self.reserve_peer_state()
        if out.get("await_merge"):
            return self._tick_await_merge(out)
        if self.reserve_poll() == "away" and self._webhook_stuck(out, time.monotonic()):
            return [("take_home",)]                           # дома: вебхук не здесь, запись на мне, резерв молчит
        serving = self._peer_serving_after_home(out, peer)
        fact = serving or self._quorum_adopted_past_me(out)
        if fact or self._webhook_away_past_me(out, peer):
            if serving:
                self._quorum_adopted_hosting_seen()           # резерв НЕСЁТ моих: это не «забираю сорвалось» — возврат только обычным путём
            # переезд прошёл мимо меня (то же правило, что у роли, journal/epoch; и по принятой эпохе
            # кворума, и по слову Telegram «вебхук у резерва») — пакет открыт не моим K (своего `go` не
            # было): по долям или по коду; слово резерва (in_by) здесь не слушаем — поколение после
            # возврата сменится. `by = webhook` — только слово Telegram: живой резерв, сказавший потом,
            # что моих не несёт, вернёт домой (остаток вебхука, _tick_away)
            self.reserve_set_out("away", kind=str(peer.get("in_kind") or "dead"), step="", taken_at=timeutil.now_iso(),
                                 by="share" if fact else "webhook", go_sent=False, gen=int((peer.get("pkg") or {}).get("gen") or 0),
                                 home_since=str(out.get("since") or ""), home_at_ts=out.get("at_ts"),
                                 guest_stopped=False, await_merge=False,
                                 last_switch=timeutil.now_iso(), healthy_mono=0, healthy_since="", can_return=False)
            self.reserve_set_poll("away")
            log.warning("резерв: «%s» несёт моих пользователей — я в состоянии away", self.reserve_peer_name())
            return [("live",)]
        return []

    def _quorum_adopted_past_me(self, out: dict) -> bool:
        """Кворум (4.2.0): эпоху держит вторая сторона по принятой пробе или `qstate` —
        кандидат сказал голосующим «забираю», мои пользователи уходят к нему. Это тот же
        факт, что `in = serving`, только раньше его `st` (или вовсе без него — резерв мог
        умереть сразу после взятия, в том числе пока я лежал: отметка на диске). Дома с ним
        основа следовала бы бессрочно: Q1 о своём боте на резерве и рычаг «⚠️ Вернуть без
        …» есть только в away. Исключение — живая вторая сторона уже сказала после принятия,
        что моих у неё нет: эпоху вернёт правило лидера (quorum/fence)."""
        if out.get("await_merge") or not hasattr(self, "_quorum_rules_home") or not self._quorum_rules_home():
            return False
        adopted = dict(self.rsv_get("rsv_qlog").get("adopted") or {})
        at = str(adopted.get("at") or "")
        if not at:
            return False
        if adopted.get("at_ts") is not None and out.get("at_ts") is not None:
            if float(adopted["at_ts"]) <= float(out["at_ts"]):
                return False                                  # принято до перехода домой — остаток
        elif at <= str(out.get("since") or ""):
            return False
        try:
            if self.db.jrn_meta()["holder"] != "peer":
                return False
        except Exception:                                     # noqa: BLE001
            return False
        return not self._quorum_adopted_idle_fact(adopted)

    def _webhook_away_past_me(self, out: dict, peer: dict) -> bool:
        """Поллинг дома получил 409 — вебхук у резерва: его ставит только гость после открытия, значит
        мои пользователи у него (взятие по коду или по кворуму, чьё слово до меня не дошло). Остаток
        вебхука у живого резерва без гостя — нет (его снимет зависший вебхук, outbound_stuck). Из away
        работают возвраты: «сразу» по Q2, аварийный по Q1, рычаг «⚠️ Вернуть без …»."""
        if out.get("await_merge") or str(out.get("return_why") or "") == "force":
            return False
        return self._webhook_says_away(out) and not self._webhook_remainder(out, peer) and not self._webhook_word_spent(out)

    def _tick_leaving(self, out: dict) -> list[tuple]:
        step = str(out.get("step") or "pkg")
        if self._expired(out) and step not in ("released",):
            if hasattr(self, "_leaving_forced"):
                forced = self._leaving_forced(out, step)      # гость забрал бота сам, пока я лежал (outbound_forced)
                if forced is not None:
                    return forced
            why = {"pkg": "данные для переезда не подтверждены резервом", "eph": f"разовый ключ от «{self.reserve_peer_name()}» не пришёл",
                   "up": f"«{self.reserve_peer_name()}» не поднял профили",
                   "record": "запись не изменена за 30 минут", "release": "передача бота не завершилась"}.get(step, "срок шага вышел")
            # резерву — `back` и на шагах ключа и подъёма: его процесс открытия ждёт или мог кончиться позже срока;
            # срыв — только с того же шага: слово канала (taken, up) за это время могло шаг сменить
            return self._move_fail(why, released=step in ("eph", "up", "record", "release"), steps=(step,))
        if step == "pkg":
            sent = self.rsv_get("rsv_pkg_sent")
            fresh = str(sent.get("at") or "") >= str(out.get("started_at") or "")
            # журнал: у резерва снимок и все события подтверждены, вне базы ничего не изменилось — снимок тот же
            if sent.get("gen") and (fresh or (out.get("pkg_unchanged") and self.reserve_jrn_all_acked())):
                # K уйдёт только конвертом к разовому ключу процесса открытия — сначала попросить ключ
                self._out_step("eph", EPH_WAIT_S, gen=int(sent["gen"]), pkg_at=str(sent.get("at") or ""))
                return [("send", "go_req", {"gen": int(sent["gen"]), "kind": str(out.get("kind") or "button"),
                                            "ttl": int(self.rsv_get("rsv_dns").get("ttl") or 0)}), ("live",)]   # TTL — гостю
            return []
        if step == "record":
            return self._tick_record(out, str(out.get("target_ip") or ""), "release")
        if step == "release":
            if out.get("release_error"):
                return self._move_fail(str(out["release_error"]), released=True, steps=("release",))   # реплика не уложится в канал
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
            if verdict in ("webhook", "webhook_info"):          # Telegram: вебхук стоит — пользователи у резерва
                # под замком и только с шага released: `taken` из потока канала мог уже увести в away —
                # второй переход был бы вторым уведомлением о переезде
                if not self._out_cas("leaving", ("released",), to="away", kind=str(out.get("kind") or "button"), step="",
                                     taken_at=timeutil.now_iso(), last_switch=timeutil.now_iso(), healthy_since=""):
                    return []
                log.info("резерв: `taken` не пришёл, но вебхук у «%s» стоит — away", self.reserve_peer_name())
                return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.moved_to_reserve(
                    self.reserve_peer_name(), str(out.get("kind") or "button"), timeutil.now_iso(),
                    dead_since=str(out.get("dead_since") or ""), reserve_ip=str(out.get("target_ip") or "")))])]
            if fresh and poll.get("state") == "mine":           # Telegram: вебхука нет — переезд не состоялся
                return self._move_fail("резерв не взял право отвечать — вебхук не поставлен", released=True, steps=("released",))
            woken = int(out.get("poll_woken") or 0)
            if woken < 3:                                       # слова Telegram ещё нет — разбудить и подождать
                if not self._out_step_cas("leaving", ("released",), "released", RELEASED_WAKE_S, poll_woken=woken + 1,
                                          released_at=str(out.get("released_at") or "")):
                    return []                                   # `taken` успел: шаг переезда в запись away не дописывать
                return [("wake_poll",)]
            return self._move_fail("право отвечать не проверено — Telegram не ответил", released=True, steps=("released",))
        return []

    def _tick_record(self, out: dict, target: str, next_step: str) -> list[tuple]:
        """Шаг записи: сменить (API) один раз, дальше ждать факта у авторитетного сервера."""
        from awgbot.domain.notices import reserve as rn
        actions: list[tuple] = []
        if not out.get("record_set_at") and not target:
            return self._record_no_target(out)                # адрес не определился: смена — на следующем такте
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
            elif not (out.get("state") == "leaving" and out.get("msg")):
                # запись меняет человек, а живого сообщения хода с просьбой нет (возврат; переезд по
                # просьбе резерва) — просьба отдельным уведомлением, со звуком
                actions.append(("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                                        text=rn.record_manual_needed(self.reserve_dns_name(), target,
                                                                                     back=out.get("state") == "returning"))]))
            self.rsv_update("rsv_out", record_set_at=timeutil.now_iso())
            actions.append(("live",))
        if not livestep.mono_due(out.get("record_probe"), time.monotonic(), 20):
            return actions
        self.rsv_update("rsv_out", record_probe=time.monotonic())
        if self.reserve_dns_points_to(target):
            if next_step == "release":
                self._out_step("release", RELEASED_WAIT_S, record_fact_at=timeutil.now_iso(), release_try=time.monotonic())
                actions += [("live",), ("release",)]
            else:                                             # шага «back» в автомате нет — сразу relay
                self._out_step("relay", RELAY_WAIT_S, record_fact_at=timeutil.now_iso(), back_at=timeutil.now_iso(),
                               back_resent=time.monotonic())
                actions += [("live",), ("send", "back", {})]
        return actions

    def _record_no_target(self, out: dict) -> list[tuple]:
        """Шаг записи без адреса (свой не определился, прежнего нет): `record_set_at` не
        ставится — повтор на следующем такте; дольше RECORD_NOIP_TELL_S — Т-РУ11 один раз."""
        if not out.get("record_noip_at"):
            self.rsv_update("rsv_out", record_noip_at=timeutil.now_iso(), record_noip_told=False)
            log.warning("резерв: адрес для записи %s не определился — смена на следующем такте", self.reserve_dns_name())
            return []
        try:
            long = timeutil.elapsed(timeutil.parse_iso(str(out["record_noip_at"]))) >= RECORD_NOIP_TELL_S
        except ValueError:
            long = True
        if not long or out.get("record_noip_told") or out.get("state") != "returning":
            return []                                         # переезд: адрес резерва известен до старта (move_check)
        self.rsv_update("rsv_out", record_noip_told=True)
        return [("notify", [self._own_unknown_note()])]

    def _own_unknown_note(self) -> Notification:
        """Т-РУ11: свой адрес не определился — запись на себя бот не сменит."""
        from awgbot.domain.notices import reserve as rn
        from ..selfhost import own_host
        return Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                            text=rn.record_own_unknown(self.reserve_dns_name(), own_host()))

    # ── события канала (переезд; прочие — outbound_move) ────────────────────
    def reserve_up_in(self, oid: str = "") -> list[tuple]:
        out = self._out()
        if out.get("state") != "leaving" or out.get("step") != "up":
            return []
        if self._word_foreign(out, "up", oid):
            return []
        self._out_step("record", RECORD_WAIT_S, up_at=timeutil.now_iso())
        return [("live",)]

    def reserve_release_build(self) -> dict | None:
        """После остановки поллинга и опроса вне очереди: финальная реплика под
        `K` — {"gen", "blob": hex}; шаг released."""
        out = self._out()
        if out.get("state") != "leaving" or out.get("step") != "release":
            return None
        if self._pkg_journal() and self.reserve_jrn_moved():
            return self._release_jrn(out)                     # у резерва открыт снимок журнала — без реплики
        db_bytes = pkgbot.replica(self._db_path())
        try:
            pkgbot.assert_clean(db_bytes)
        except pkgbot.BotPartError as e:
            log.warning("резерв: финальная реплика не прошла сторожа: %s", e)
            return None
        blob = seal.snapshot_seal(self.reserve_k(), {"pkg_gen": int(out.get("gen") or 0), "at": timeutil.now_iso()},
                                  db_bytes)
        too_big = self.pkg_too_big(blob)
        if too_big:
            # до смены шага: иначе ProtocolError при нарезке частей оставил бы шаг released без поллинга
            # и без реплики у резерва; «не собралась» — поллинг обратно, такт шага release срывает
            # переезд по отметке сразу (Т-РУ), а не повторяет передачу каждые 30 с до срока
            log.warning("резерв: финальная реплика не уложится в канал: %s", too_big)
            self.rsv_update("rsv_out", release_error=too_big)
            return None
        # снимок для слияния при возврате — именно эта реплика: с неё стартует гость, и всё,
        # что было записано здесь между пакетом и передачей, не удвоится и не станет «чужим»
        try:
            self._pkg_store_base(int(out.get("gen") or 0), db_bytes)
        except OSError as e:
            log.warning("резерв: снимок финальной реплики не сохранён: %s", e)
        self._out_step("released", RELEASED_WAIT_S, released_at=timeutil.now_iso(), poll_woken=0)
        self.reserve_set_poll("away", why="released")           # своя отметка; слово Telegram — webhook/webhook_info
        return {"gen": int(out.get("gen") or 0), "blob": blob.hex()}

    # ── здоровье: Telegram с этого сервера (outbound_health) ────────────────
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
