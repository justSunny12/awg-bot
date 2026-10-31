"""
outbound.py — часть пакета reserve (см. __init__): «мои пользователи» на стороне
основы — переезд кнопкой, away и здоровье; шаги возврата — в outbound_return,
слияние — в merge_ops (автомат и сроки шагов — общие, здесь).

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

from awgbot.core import config, settings
from awgbot.util import livestep, timeutil
from awgbot.util.rsvpkg import bot as pkgbot, seal

from ..types import Notification, ServiceError
from ..gateway_link.common import log
from .state import _LOCK

PKG_WAIT_S = 120          # свежий пакет до `go_req`
EPH_WAIT_S = 60           # разовый ключ процесса открытия (`open_eph`) после `go_req`
UP_WAIT_S = 300           # резерв поднял клон
RECORD_WAIT_S = 1800      # факт записи
RELEASED_WAIT_S = 300     # `taken` после `released`; возврат: `released` после `back_req`
FORCE_OFFLINE_S = 10 * 60        # «вернуть без резерва» — не раньше, чем он не на связи столько
STUCK_IDLE_S = 10 * 60           # столько подряд запись на мне, а резерв пользователей не несёт — вебхук зависший
STUCK_PAIR_S = 30 * 60           # пары нет на связи столько, запись на мне — вебхук резерва зависший
HEALTHY_S = 300           # «здоров» — столько подряд
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
            return "пользователи уже не на этом сервере"
        if self.reserve_hosting():
            return f"сейчас здесь пользователи «{self.reserve_peer_name()}» — сначала их возврат"
        if not self.reserve_online():
            return f"«{self.reserve_peer_name()}» не на связи"
        ok, why = self.reserve_directions()["out"]
        if not ok:
            return why
        if not self.reserve_dns_name():
            return "вместо доменного имени сервера — IP, записи DNS нет: переезд невозможен"
        if not self.reserve_peer_ip():
            return f"адрес «{self.reserve_peer_name()}» не определился"
        if settings.get_bool("app.reserve.ready", False) is False:
            return "«Готов быть резервом» выключен"
        if "seal1" not in (self.reserve_peer_state().get("caps") or []):
            # ключ пакета уходит только конвертом к разовому ключу процесса открытия; открытым — нет
            return f"«{self.reserve_peer_name()}» не принимает зашифрованный ключ — необходима v4.0.2 или выше"
        return ""

    def reserve_move_dialog_data(self) -> dict:
        d = {"name": self.reserve_peer_name(), "record": self.reserve_dns_name(), "target": self.reserve_peer_ip(),
             "manual": not self.reserve_dns_automatic(), "own": self.reserve_own_ip()}
        count = self.reserve_quorum_count() if hasattr(self, "reserve_quorum_count") else {}
        tie = self.reserve_quorum_tie_view() if hasattr(self, "reserve_quorum_tie_view") else {}
        if count and (count.get("role") == "fenced" or tie):
            d["quorum"] = {"y": int(count["y"]), "v": int(count["v"])}          # Т-К18: лишён кворума или ничья
        return d

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
                                       taken_at="", reason="", gen=0, guest_stopped=False, await_merge=False)
            self._out_step("pkg", PKG_WAIT_S, pkg_unchanged=False)
            self.reserve_pkg_mark()                           # журнал: такт соберёт снимок, только если изменилось вне базы
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
        elif out.get("record_set_at"):
            record_back = "переезд не состоялся, запись меняется вручную"   # Т-РУ11: вернуть её — дело человека
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
            return self._tick_away(out)
        if state == "returning":
            return self._tick_returning(out)
        return self._tick_home(out) + self.reserve_tail_settle() + self._home_back_resend()   # свой хвост при держателе дома — переносить

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
        fact = self._peer_serving_after_home(out, peer) or self._quorum_adopted_past_me(out)
        if fact or self._webhook_away_past_me(out, peer):
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
        if not at or at <= str(out.get("since") or ""):
            return False                                      # принято до перехода домой — остаток
        try:
            if self.db.jrn_meta()["holder"] != "peer":
                return False
        except Exception:                                     # noqa: BLE001
            return False
        return not self._quorum_peer_idle_fact(at)

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
            why = {"pkg": "данные для переезда не подтверждены резервом", "eph": f"разовый ключ от «{self.reserve_peer_name()}» не пришёл",
                   "up": f"«{self.reserve_peer_name()}» не поднял профили",
                   "record": "запись не изменена за 30 минут", "release": "передача бота не завершилась"}.get(step, "срок шага вышел")
            # резерву — `back` и на шагах ключа и подъёма: его процесс открытия ждёт или мог кончиться позже срока
            return self._move_fail(why, released=step in ("eph", "up", "record", "release"))
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

    # ── события канала (переезд) ────────────────────────────────────────────
    def reserve_open_eph_in(self, gen: int, eph_hex: str, oid: str) -> list[tuple]:
        """`open_eph` от резерва: разовый ключ его процесса открытия — `K`
        конвертом к нему в `go{sealed, oid}`; шаг up. Повтор на шаге up от того же
        процесса (тот же `oid` и поколение) — `go` снова: резерв повторяет `open_eph`,
        только пока процесс ждёт конверт, значит прежний `go` потерялся (резерв второй
        конверт тому же процессу не передаёт — молча). Срок шага up не продлевается."""
        out = self._out()
        if out.get("state") != "leaving" or int(out.get("gen") or 0) != int(gen):
            return []
        again = out.get("step") == "up" and out.get("go_sent") and str(out.get("oid") or "") == str(oid)
        if out.get("step") != "eph" and not again:
            return []
        try:
            eph = bytes.fromhex(str(eph_hex))
        except ValueError:
            eph = b""                                            # не hex — та же причина, что не та длина
        try:
            sealed = seal.seal_k_to(eph, self.reserve_k(), int(gen), str(oid))
        except seal.PackageError as e:
            return self._move_fail(f"разовый ключ от «{self.reserve_peer_name()}» не годится: {e}", released=True)
        if again:
            log.info("резерв: «%s» снова просит ключ того же открытия — go повторён", self.reserve_peer_name())
        else:
            self._out_step("up", UP_WAIT_S, oid=str(oid))
        self.rsv_update("rsv_out", go_sent=True)                 # K отдан — своя отметка, не слово резерва
        return [("send", "go", {"gen": int(gen), "kind": str(out.get("kind") or "button"),
                                "ttl": int(self.rsv_get("rsv_dns").get("ttl") or 0),
                                "sealed": sealed.hex(), "oid": str(oid)}), ("live",)]

    def reserve_up_in(self) -> list[tuple]:
        out = self._out()
        if out.get("state") != "leaving" or out.get("step") != "up":
            return []
        self._out_step("record", RECORD_WAIT_S, up_at=timeutil.now_iso())
        return [("live",)]

    def reserve_down_in(self, reason: str) -> list[tuple]:
        """`down` резерва на переезде: срыв; `back` не нужен — резерв зону снял сам
        (и когда отказал конверту ключа: ждавший его процесс открытия он гасит)."""
        out = self._out()
        if out.get("state") == "leaving":
            return self._move_fail(f"«{self.reserve_peer_name()}» не поднял профили — {reason}")
        return []

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
        log.info("резерв: пользователи и бот — на «%s»", self.reserve_peer_name())
        return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.moved_to_reserve(
            self.reserve_peer_name(), str(out.get("kind") or "button"), timeutil.now_iso(),
            dead_since=str(out.get("dead_since") or ""), reserve_ip=str(out.get("target_ip") or "")))])]

    # ── away: здоровье и возврат ────────────────────────────────────────────
    def reserve_healthy(self) -> bool:
        """Свой AWG отвечает, линк пары жив, шлюз (если есть) на связи — сейчас. Под
        кворумом «свой AWG отвечает» и «шлюз на связи» заменяет счёт Q2: «да» за меня
        путём пользователей строго больше половины голосующих."""
        q = self.reserve_quorum_healthy() if hasattr(self, "reserve_quorum_healthy") else None
        if q is not None:
            return bool(q and self.reserve_online() and self.reserve_peer_seen() == "ok" and self._tg_reachable())
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
        if str(out.get("by") or "") == "webhook" and self._webhook_remainder(out, self.reserve_peer_state()):
            # в away по одному слову Telegram, а живой резерв после перехода говорит, что моих не несёт:
            # остаток вебхука — домой, запись здесь; сам вебхук снимет проверка зависшего (outbound_stuck)
            # отметки home — прежние (home_since/home_at_ts): как будто автомат и не уходил, роль и
            # проверка зависшего вебхука считают от них
            with _LOCK:
                cur = dict(self.rsv_get("rsv_out"))
                cur.update(state="home", kind="", step="", by="", await_merge=False, since=str(cur.get("home_since") or ""),
                           webhook_rem_ts=self.rsv_get("rsv_poll").get("at_ts"))   # это слово Telegram — остаток, второй раз не ведёт
                cur.pop("at_ts", None)
                if cur.get("home_at_ts") is not None:
                    cur["at_ts"] = cur["home_at_ts"]
                cur.pop("home_since", None)
                cur.pop("home_at_ts", None)
                self.rsv_put("rsv_out", cur)
            self.reserve_epoch_sync()
            log.warning("резерв: «%s» на связи без моего гостя — вебхук у него остаток, я дома", self.reserve_peer_name())
            return [("live",)]
        if self.reserve_poll() == "away" and self._webhook_stuck(out, now):
            return [("take_home",)]
        if self.reserve_healthy():
            since = float(out.get("healthy_mono") or 0)
            if not since or since > now + 1.0:                # отметки нет или она прежней загрузки — счёт заново
                self.rsv_update("rsv_out", healthy_mono=now, healthy_since=timeutil.now_iso())
                return []
            if now - since < HEALTHY_S:
                return []
            if out.get("merge_blocked"):
                # слияние дважды упало с той же причиной — сам не повторяю, но кнопка «↩️ Вернуть пользователей»
                # (у основы и у гостя) должна быть: can_return держится, пока основа здорова
                if not out.get("can_return"):
                    self.rsv_update("rsv_out", can_return=True)
                    return [("live",)]
                return []
            if str(settings.get("app.reserve.return", "immediate") or "immediate") == "immediate" \
                    and not self.reserve_return_check():
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
