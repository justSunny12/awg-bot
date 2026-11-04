"""
outbound_move.py — часть пакета reserve (см. __init__): «мои пользователи» —
переезд кнопкой: проверка и данные диалога, старт (leaving/pkg), срыв
(`_move_fail`: домой, `back` резерву, запись — обратно на себя), слова резерва
на переезде (`open_eph` — `K` конвертом к разовому ключу, `down`, `taken`) и
данные живого сообщения хода (вынесено из outbound: там автомат, такт шагов,
сроки и передача бота).
"""
from __future__ import annotations

from awgbot.core import config, settings
from awgbot.util import timeutil
from awgbot.util.rsvpkg import seal

from ..types import Notification, ServiceError
from ..gateway_link.common import log
from .outbound import PKG_WAIT_S, UP_WAIT_S
from .state import _LOCK


class ReserveMoveMixin:
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
            return "не настроен домен сервера (укажи в «🖥 Сервер AWG»)"
        if not self.reserve_peer_ip():
            return f"адрес «{self.reserve_peer_name()}» не определился"
        if settings.get_bool("reserve.ready", False) is False:
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
                                       taken_at="", reason="", gen=0, guest_stopped=False, await_merge=False,
                                       release_error="")
            self._out_step("pkg", PKG_WAIT_S, pkg_unchanged=False)
            self.reserve_pkg_mark()                           # журнал: такт соберёт снимок, только если изменилось вне базы
        log.info("резерв: переезд кнопкой на «%s» начат", self.reserve_peer_name())
        return out

    def _move_fail(self, reason: str, released: bool = False, steps=()) -> list[tuple]:
        """Переезд не состоялся: домой, резерву — `back` (снять зону, если он
        её поднял или ещё поднимает), запись — обратно на себя, если успела
        смениться, итог админу. Переход home — под замком и только из leaving (на
        одном из `steps`, если даны): срыв по сроку из такта и `taken` из потока
        канала в одном окне — переход один; кто опоздал, тот ничего не делает."""
        from awgbot.domain.notices import reserve as rn
        name = self.reserve_peer_name()
        with _LOCK:
            out = self._out()
            if not self._out_is("leaving", steps):
                log.info("резерв: срыв переезда (%s) опоздал — автомат уже %s/%s", reason,
                         out.get("state") or "home", out.get("step") or "")
                return []
            self.reserve_set_out("home", reason=reason, failed_at=timeutil.now_iso(), step="")
        if out.get("go_sent"):
            # K ушёл резерву — он его знает: новый K и свежий пакет под ним (после `back` резерв
            # сносит прежний пакет, досылка — по отметке)
            self.reserve_new_k()
            self.reserve_pkg_mark()
        record_back, own_unknown = "", False
        if out.get("record_set_at") and self.reserve_dns_automatic():
            own = self.reserve_own_ip()
            try:
                if own:
                    self.reserve_dns_switch(own)
                else:
                    own_unknown = True                        # запись на резерве, а свой адрес не определился
            except ServiceError as e:
                record_back = str(e)
        elif out.get("record_set_at"):
            record_back = "переезд не состоялся, запись меняется вручную"   # Т-РУ11: вернуть её — дело человека
        log.warning("резерв: переезд на «%s» не состоялся: %s", name, reason)
        notes = [Notification(tg_id=int(config.ADMIN_ID), text=rn.move_failed(name, reason, released=released))]
        if own_unknown or (record_back and not self.reserve_own_ip()):
            notes.append(self._own_unknown_note())
        elif record_back:
            notes.append(Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                      text=rn.record_not_changed(self.reserve_dns_name(), record_back, self.reserve_own_ip())))
        actions: list[tuple] = [("live",), ("notify", notes)]
        if released:
            # передача могла остановить поллинг (`release`, rsv_poll = away): право отвечать снова
            # решает Telegram — разбудить сразу, а не ждать перепроверки (до 7 мин)
            actions[:0] = [("send", "back", {}), ("wake_poll",)]
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

    @staticmethod
    def _word_foreign(out: dict, word: str, oid: str) -> bool:
        """`up`/`taken` с id открытия (ret1) не моего — слово прошлого эпизода; без поля — как прежде."""
        if oid and out.get("oid") and str(oid) != str(out.get("oid")):
            log.warning("резерв: «%s» открытия %s, а моё — %s: слово прошлого эпизода — пропущено", word, oid, out.get("oid"))
            return True
        return False

    def reserve_down_in(self, reason: str, rec: bool = False) -> list[tuple]:
        """`down` резерва на переезде: срыв; `back` не нужен — резерв зону снял сам
        (и когда отказал конверту ключа: ждавший его процесс открытия он гасит). Вне
        переезда с `rec` — запись уже сменил его процесс открытия: вернуть её на себя
        (outbound_keep)."""
        out = self._out()
        if out.get("state") == "leaving":
            return self._move_fail(f"«{self.reserve_peer_name()}» не поднял профили — {reason}")
        if rec and str(out.get("state") or "home") in ("home", "away"):
            return self.reserve_rec_back_mark(reason)
        return []

    def reserve_taken_in(self, oid: str = "") -> list[tuple]:
        """`taken` от резерва: гость отвечает в Telegram. Только на шагах
        передачи бота (release/released): запоздалое `taken` прошлого эпизода на шаге pkg/eph/up/record
        переводило бы в away с `poll=away`, пока запись DNS ещё на мне. Вне leaving — это `taken`
        основы на её возврате с меня: её боту у меня вебхук больше не ставить (inbound_tgwait)."""
        from awgbot.domain.notices import reserve as rn
        with _LOCK:                                           # проверка и переход — одним замком с тактом (срыв по сроку)
            out = self._out()
            if out.get("state") != "leaving":
                self.reserve_peer_tg_taken_in()               # её пользователи у меня, а Telegram она забрала (возврат)
                return []
            if str(out.get("step") or "") not in ("release", "released"):
                log.warning("резерв: «taken» на шаге %s — вне передачи бота, пропущено", out.get("step") or "pkg")
                return []
            if self._word_foreign(out, "taken", oid):
                return []
            self.reserve_set_out("away", kind=str(out.get("kind") or "button"), step="", taken_at=timeutil.now_iso(),
                                 last_switch=timeutil.now_iso(), healthy_since="")
            self.reserve_set_poll("away")
        log.info("резерв: пользователи и бот — на «%s»", self.reserve_peer_name())
        return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.moved_to_reserve(
            self.reserve_peer_name(), str(out.get("kind") or "button"), timeutil.now_iso(),
            dead_since=str(out.get("dead_since") or ""), reserve_ip=str(out.get("target_ip") or "")))])]

    # ── живое сообщение хода ────────────────────────────────────────────────
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
