"""
fence.py — часть пакета quorum (см. __init__): решения лидера — лишение
кворума и возврат к лидерству через журнал (`jrn_fence`/`jrn_unfence`),
старт без права записи, принятие чужой эпохи по пробе (`jrn_adopt`),
плановая пауза (`qpause`), загородка бота (концепт «кворум» §3.4, §3.7.12,
§5.2, §6.3).

Лишённый кворума лидер — держатель `self` с ролью `follower`: запрет решений
в базе и выключенные задания — те же, что у следования; Telegram
обслуживается только экранами резерва (Т-К10 админу, Т-К9 остальным). Отметка
эпизода — `rsv_qlog.fenced` (строка Т-К2, Т-К7 один раз). Режима «только
наблюдать» нет: решения — всегда, когда кворум включён и v ≥ 2
(`reserve_quorum_decisions`); при одном голосующем (v = 1) ограждения нет —
судьбу сервера не решает один голос.
"""
from __future__ import annotations

import time

from awgbot.core import config
from awgbot.util import timeutil

from ...gateway_link.common import log


class ReserveQuorumFenceMixin:

    def reserve_quorum_fenced(self) -> bool:
        """Лишён ли кворума этот процесс (для загородки бота): по отметке такта."""
        return bool(self.__dict__.get("_quo_fenced"))

    def reserve_quorum_barred(self) -> bool:
        """Решений от админа не исполнять: лишён кворума по отметке такта или по
        журналу (держатель `self` с ролью `follower` — так и после перезапуска,
        пока такт отметку не поставил). Страховка доменных действий за загородкой бота."""
        return self.reserve_quorum_fenced() or self._quorum_jrn_fenced()

    def _quorum_jrn_fenced(self) -> bool:
        try:
            meta = self.db.jrn_meta()
        except Exception:                                     # noqa: BLE001
            return False
        return meta["holder"] == "self" and meta["role"] == "follower"

    # ── действия лидера ─────────────────────────────────────────────────────
    def reserve_quorum_fence_apply(self, act: str, ld) -> list[tuple]:
        """`fence` / `unfence` / `start` правила лидера → журнал и уведомления."""
        from awgbot.domain.notices import reserve as rn
        from ...types import Notification
        now = time.monotonic()
        y, v = ld.yes_count(now), ld.v
        name = str(config.SERVER_NAME or "")
        qlog = self.rsv_get("rsv_qlog")
        fenced = dict(qlog.get("fenced") or {})
        if act in ("fence", "start"):
            why = "кворум: да %d из %d" % (y, v) if act == "fence" else "кворум: старт — жду большинства «да»"
            meta = self.db.jrn_fence(why=why)
            if meta is None and not self._quorum_jrn_fenced():
                return []
            self.__dict__["_quo_fenced"] = True
            if not fenced.get("since") or fenced.get("on") is False:
                fenced = {"on": True, "since": timeutil.now_iso(), "told": False, "start": act == "start"}
            fenced["on"] = True
            self.rsv_update("rsv_qlog", fenced=fenced)
            if act == "fence" and not fenced.get("told"):
                fenced["told"] = True
                self.rsv_update("rsv_qlog", fenced=fenced)
                return [("notify", [Notification(tg_id=int(config.ADMIN_ID), silent=True,
                                                 text=rn.quorum_fenced(name, y, v, fenced["since"]))])]
            return []
        if act == "unfence":
            why_u = "кворум: да %d из %d" % (y, v)
            meta_now = self.db.jrn_meta()
            adopted = dict(qlog.get("adopted") or {})
            if meta_now["holder"] == "peer" and adopted:
                # эпоху принял по пробе (кандидат забирал). «Да» большинства — не доказательство, что
                # пользователей не взяли (это обычное Q2): к записи — только по факту живой второй
                # стороны, сказавшей после принятия, что моих пользователей у неё нет (§3.4.2)
                if not self._quorum_adopted_idle_fact(adopted):
                    if not self.__dict__.get("_quo_adopt_wait_told"):
                        self.__dict__["_quo_adopt_wait_told"] = True
                        log.warning("кворум: большинство «да» снова здесь, но эпоху %s держит «%s» — жду её слова",
                                    meta_now["epoch"], self.reserve_peer_name())
                    ld.fence_now(now)                         # правило спросит снова через окно U
                    return []
                meta = self.db.jrn_pass(holder="self", role="holder", why=why_u, fork_i=int(meta_now["applied"]))
                self.db.jrn_tail_drop_held()
                self.rsv_update("rsv_qlog", adopted=None)
                self.__dict__.pop("_quo_adopt_wait_told", None)
                if meta is not None and hasattr(self, "reserve_follow_clear"):
                    self.reserve_follow_clear()
                log.warning("кворум: «%s» на связи без моих пользователей, большинство «да» здесь — эпоха %s здесь",
                            self.reserve_peer_name(), (meta or meta_now)["epoch"])
            else:
                meta = self.db.jrn_unfence(why=why_u)
            self.__dict__["_quo_fenced"] = False
            told = bool(fenced.get("told"))
            if fenced:
                self.rsv_update("rsv_qlog", fenced={**fenced, "on": False, "until": timeutil.now_iso()})
            if meta is not None and told:
                return [("notify", [Notification(tg_id=int(config.ADMIN_ID), silent=True, text=rn.quorum_unfenced(name, y, v))])]
            return []
        return []

    def reserve_quorum_unfence_if_off(self) -> None:
        """Кворум выключен или я больше не лидер своей линии, а ограждение кворума стоит —
        снять: лидер без голосующих решений не лишается."""
        if not self.__dict__.get("_quo_fenced") and not (self.rsv_get("rsv_qlog").get("fenced") or {}).get("on"):
            return
        if hasattr(self, "reserve_quorum_guest_phase") and self.reserve_quorum_guest_phase() == "waiting":
            return                                            # гость ждёт состав: это не «кворум выключен»
        if hasattr(self, "reserve_out") and self.reserve_out() in ("leaving", "away", "returning"):
            return                                            # в ходе переезда (по просьбе лидера) ограждение держится
        why = self.reserve_quorum_off_reasons() if hasattr(self, "reserve_quorum_off_reasons") else []
        if why and all(c in ("keys", "not_acked") for c in why):
            # выключено только у меня и ненадолго (ключи, подтверждение состава): вторая сторона может ещё
            # решать. Цифры (`timing:`) — не ненадолго: линия в `st.quo` выключена, кандидат второй стороны
            # без `active` не решает, и снять ограждение было бы некому — снимаю, как при структурном выключении
            return
        if self._quorum_jrn_fenced():
            self.db.jrn_unfence(why="кворум выключен")
            log.info("кворум: ограждение снято — кворум выключен или лидер не здесь")
        self.__dict__["_quo_fenced"] = False
        fenced = dict(self.rsv_get("rsv_qlog").get("fenced") or {})
        if fenced:
            self.rsv_update("rsv_qlog", fenced={**fenced, "on": False, "until": timeutil.now_iso()})

    def reserve_quorum_adopt(self, e: int, who: str) -> None:
        """Проба или слово несут эпоху больше моей: эпоху держит другая сторона —
        следование без приращения (`jrn_adopt`); ограждение кворума при этом не
        нужно (держатель — не я), но машина лидера огораживается (её «да» не должны
        снимать ограждение, пока эпоху не вернул журнал). Отметка принятия — на диске
        (`rsv_qlog.adopted`) и раньше записи в журнал: страховка такта в другом потоке и
        после перезапуска эпоху не вернёт — вернёт правило лидера по факту второй
        стороны; второй стороне уходит `qstate` — её свежий `st` и есть этот факт.
        В ходе передачи кнопкой (leaving до released) эпоха пробы — поднятая кандидатом
        после «забираю», а не факт: передачу делает моя рука (hand_over), принимать нечего."""
        from ..journal.ship import epoch_sane
        mine = self.reserve_quorum_epoch()
        if not epoch_sane(int(e), int(mine)):
            log.warning("кворум: эпоха %s от %s — вне разумного при моей %s, не принимаю", e, who, mine)
            return
        if hasattr(self, "reserve_out") and self.reserve_out() == "leaving":
            log.info("кворум: эпоха %s от %s в ходе передачи — не принимаю (передачу делаю сам)", e, who)
            return
        self.rsv_update("rsv_qlog", adopted={"e": int(e), "at": timeutil.now_iso(), "at_ts": timeutil.now().timestamp(),
                                             "who": str(who)[:64]})
        try:
            meta = self.db.jrn_adopt(int(e), why=f"кворум: эпоха {int(e)} у {who}", fork_i=self._quorum_adopt_fork())
        except Exception as err:                              # noqa: BLE001
            self.rsv_update("rsv_qlog", adopted=None)
            log.warning("кворум: эпоха %s от %s не принята: %s", e, who, err)
            return
        if meta is None:
            if self.db.jrn_meta()["holder"] != "peer":
                self.rsv_update("rsv_qlog", adopted=None)
            return
        self.__dict__["_quo_fenced"] = False
        self.__dict__["_quo_qstate_due"] = True               # второй стороне — qstate: пусть скажет свежий st
        ld = self.__dict__.get("_rsv_qleader")
        if ld is not None and not ld.q2:
            ld.fence_now(time.monotonic())                    # «да» большинства одного не вернут запись (§3.4.2)
        peer_fork = int((self.reserve_peer_state().get("jrn_keep") or {}).get("guest_fork") or 0)
        if peer_fork and hasattr(self, "reserve_tail_finalize"):
            self.reserve_tail_finalize(peer_fork)             # точка гостя уже известна — хвост готовить сразу
        log.warning("кворум: эпоха %s у %s — здесь следование", e, who)

    def _quorum_adopt_fork(self) -> int:
        """Точка расхождения для принятия эпохи по пробе: подтверждённое резервом (мои события
        после него — в хвост, не затираются событиями гостя); если его `st` уже несёт точку
        гостя в моих номерах — она (как при `jstale`, journal/route)."""
        meta_now = self.db.jrn_meta()
        acked = int(meta_now["acked"])
        peer = self.reserve_peer_state()
        fork = int((peer.get("jrn_keep") or {}).get("guest_fork") or 0) if str(peer.get("inn") or "") == "serving" else 0
        if fork and acked <= fork <= max(self.db.jrn_last_i(), acked) + 1:
            return fork
        return acked

    def reserve_quorum_adopted(self) -> bool:
        """Эпоха принята по кворуму и ещё не возвращена (отметка на диске)."""
        return bool(self.rsv_get("rsv_qlog").get("adopted"))

    def _quorum_peer_idle_fact(self, since_iso: str, since_ts: float | None = None) -> bool:
        """Вторая сторона жива, её `st` получен после отметки (по at_ts — доли секунды; без
        него — по ISO) и говорит, что моих пользователей у неё нет: только такой факт
        возвращает запись сюда."""
        from ..state import HOSTING
        peer = self.reserve_peer_state()
        if not (self.reserve_online() and self.reserve_peer_seen() != "down" and not peer.get("st_stale")):
            return False
        if str(peer.get("inn") or "idle") in HOSTING:
            return False
        if since_ts is not None and peer.get("at_ts") is not None:
            return float(peer["at_ts"]) > float(since_ts)
        return str(peer.get("at") or "") > str(since_iso or "")

    def _quorum_adopted_idle_fact(self, adopted: dict) -> bool:
        """Факт «моих у второй стороны нет» после принятия эпохи: увиденный однажды
        (`idle_seen` — автомат вернулся домой по нему) остаётся фактом и когда её `st`
        потом устарел; новое «забираю» — новая отметка принятия без него."""
        if adopted.get("idle_seen"):
            return True
        return self._quorum_peer_idle_fact(str(adopted.get("at") or ""), adopted.get("at_ts"))

    # ── плановая пауза ──────────────────────────────────────────────────────
    def reserve_quorum_pause_actions(self, seconds: int = 300) -> list[tuple]:
        """`qpause{l, until}` второму серверу и своим шлюзам перед плановой остановкой
        (обновление, перезапуск бота): до срока их «не вижу» кандидату не уходит."""
        m = self.reserve_quorum_mine() if hasattr(self, "reserve_quorum_mine") else None
        if m is None or not self.reserve_quorum_active():
            return []
        until = timeutil.to_iso(timeutil.shift(timeutil.now(), int(seconds)))
        body = {"l": m.l, "until": until, "dur": int(seconds)}   # dur — длительность: часы сторон могут расходиться
        actions: list[tuple] = []
        if self.reserve_online():
            actions.append(("send", "qpause", body))
        from awgbot.util.quorum import members as qm
        for node in m.gateways:
            try:
                actions.append(("gwlink", qm.parse_node(node)[1], "qpause", body))
            except ValueError:
                continue
        return actions

    def reserve_quorum_pause_sync(self, seconds: int = 300) -> None:
        """То же из синхронного кода (шаг обновления сервера): действия — на цикл событий."""
        actions = self.reserve_quorum_pause_actions(seconds)
        if not actions:
            return
        try:
            from awgbot.runtime import rsvactions
            rsvactions.run_threadsafe(actions)
        except Exception as e:                                # noqa: BLE001
            log.info("кворум: qpause не ушла: %s", e)
