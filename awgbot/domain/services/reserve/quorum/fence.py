"""
fence.py — часть пакета quorum (см. __init__): решения лидера — лишение
кворума и возврат к лидерству через журнал (`jrn_fence`/`jrn_unfence`),
старт без права записи, принятие чужой эпохи по пробе (`jrn_adopt`),
плановая пауза (`qpause`), загородка бота (концепт «кворум» §3.4, §3.7.12,
§5.2, §6.3).

Лишённый кворума лидер — держатель `self` с ролью `follower`: запрет решений
в базе и выключенные задания — те же, что у следования; Telegram
обслуживается только экранами резерва (Т-К10 админу, Т-К9 остальным). Отметка
эпизода — `rsv_qlog.fenced` (строка Т-К2, Т-К7 один раз). Всё — только при
`app.reserve.quorum.act` (решение 9); иначе счёт идёт, решений нет.
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

    def _quorum_jrn_fenced(self) -> bool:
        try:
            meta = self.db.jrn_meta()
        except Exception:                                     # noqa: BLE001
            return False
        return meta["holder"] == "self" and meta["role"] == "follower"

    # ── действия лидера ─────────────────────────────────────────────────────
    def reserve_quorum_fence_apply(self, act: str, ld) -> list[tuple]:
        """`fence` / `unfence` / `start` правила лидера → журнал и уведомления."""
        if not self.reserve_quorum_act():
            return []
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
                if not self._quorum_peer_idle_fact(str(adopted.get("at") or "")):
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
        if hasattr(self, "reserve_out") and self.reserve_out() in ("leaving", "away", "returning"):
            return                                            # в ходе переезда (по просьбе лидера) ограждение держится
        why = self.reserve_quorum_off_reasons() if hasattr(self, "reserve_quorum_off_reasons") else []
        if why and all(c in ("keys", "not_acked") or c.startswith("timing:") for c in why):
            return                                            # выключено только у меня (ключи, состав, цифры): вторая сторона может ещё решать
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
        нужно (держатель — не я). Отметка принятия — на диске (`rsv_qlog.adopted`) и
        раньше записи в журнал: страховка такта в другом потоке и после перезапуска
        эпоху не вернёт — вернёт правило лидера по факту второй стороны."""
        if not self.reserve_quorum_act():
            return
        self.rsv_update("rsv_qlog", adopted={"e": int(e), "at": timeutil.now_iso(), "who": str(who)[:64]})
        try:
            meta = self.db.jrn_adopt(int(e), why=f"кворум: эпоха {int(e)} у {who}")
        except Exception as err:                              # noqa: BLE001
            self.rsv_update("rsv_qlog", adopted=None)
            log.warning("кворум: эпоха %s от %s не принята: %s", e, who, err)
            return
        if meta is None:
            if self.db.jrn_meta()["holder"] != "peer":
                self.rsv_update("rsv_qlog", adopted=None)
            return
        self.__dict__["_quo_fenced"] = False
        log.warning("кворум: эпоха %s у %s — здесь следование", e, who)

    def reserve_quorum_adopted(self) -> bool:
        """Эпоха принята по кворуму и ещё не возвращена (отметка на диске)."""
        return bool(self.rsv_get("rsv_qlog").get("adopted"))

    def _quorum_peer_idle_fact(self, since_iso: str) -> bool:
        """Вторая сторона жива, её `st` получен после принятия эпохи и говорит, что моих
        пользователей у неё нет: только такой факт возвращает запись сюда."""
        peer = self.reserve_peer_state()
        if not (self.reserve_online() and self.reserve_peer_seen() != "down" and not peer.get("st_stale")):
            return False
        if str(peer.get("inn") or "idle") in ("taking", "serving", "releasing"):
            return False
        return str(peer.get("at") or "") > str(since_iso or "")

    # ── плановая пауза ──────────────────────────────────────────────────────
    def reserve_quorum_pause_actions(self, seconds: int = 300) -> list[tuple]:
        """`qpause{l, until}` второму серверу и своим шлюзам перед плановой остановкой
        (обновление, перезапуск бота): до срока их «не вижу» кандидату не уходит."""
        m = self.reserve_quorum_mine() if hasattr(self, "reserve_quorum_mine") else None
        if m is None or not self.reserve_quorum_active():
            return []
        until = timeutil.to_iso(timeutil.shift(timeutil.now(), int(seconds)))
        body = {"l": m.l, "until": until}
        actions: list[tuple] = []
        if self.reserve_online() and self.reserve_quorum_peer_quo1():
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
