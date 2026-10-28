"""
follow.py — часть пакета journal (см. __init__): основа в следовании.

Пока решения пишет бот основы на резерве, у основы остаются сессии
пользователей, ещё не переподключившихся к резерву. Их блокировки решает
держатель: бот основы на резерве шлёт адреса заблокированных устройств
(сообщение `blocks`, обратное прежнему), основа держит DROP по ним, а не по
своей устаревшей базе. Месячный сброс в следовании откладывается: его делает
держатель, а после возврата он уже учтён (`reserve_monthly_settle`).

С журналом (`jrn1` у обоих) решения держателя приходят событиями и ложатся в
базу (`_follow_jev_in`: строго подряд, разрыв — `jneed`; пиры — при смене ключа,
адреса, интерфейса или PSK; блокировки — по итогу; сводка эпохи для Т-РУ5б),
пачки счётчиков клона складываются (`_follow_jcnt_in`); сообщения `blocks` нет.
Отставание дольше минуты — Т-Ж2 в карточке и Т-Ж6.
"""
from __future__ import annotations

import html

from awgbot.core import config
from awgbot.util import timeutil

from ...gateway_link.common import log
from ..away import AWAY_REFUSED
from ...types import Notification, ServiceError


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


class ReserveFollowMixin:

    # ── блокировки держателя ────────────────────────────────────────────────
    def reserve_holder_blocks_in(self, addrs: list[str]) -> None:
        """`blocks` от бота основы на резерве: DROP оставшимся здесь сессиям по его решениям."""
        if not self.reserve_following():
            log.info("резерв: блокировки от «%s» вне следования — пропущены", self.reserve_peer_name())
            return
        clean = sorted({str(a) for a in addrs if a})[:4096]
        self.rsv_update("rsv_follow", blocks=clean, blocks_at=timeutil.now_iso())
        try:
            self.reconcile_blocks(True)
        except Exception as e:                                # noqa: BLE001
            log.warning("резерв: блокировки держателя не наложены: %s", e)

    def reserve_holder_blocks(self) -> list[str] | None:
        """Адреса для DROP в следовании; None — не следование или держатель их ещё не прислал
        (тогда — по своей базе: решения в ней с начала следования не меняются)."""
        if not self.reserve_following() or self.reserve_jrn_moved():
            return None                                       # журнал: решения держателя уже в моей базе
        f = self.rsv_get("rsv_follow")
        if not f.get("blocks_at"):
            return None
        return [str(a) for a in (f.get("blocks") or [])]

    # ── месячный сброс ──────────────────────────────────────────────────────
    def reserve_monthly_defer(self, ym: str) -> None:
        """Сброс месяца пришёлся на следование: его сделал держатель."""
        if str(self.rsv_get("rsv_follow").get("monthly_deferred") or "") < ym:
            self.rsv_update("rsv_follow", monthly_deferred=ym)
            log.info("резерв: месячный сброс %s — у бота на «%s»", ym, self.reserve_peer_name())

    def reserve_monthly_settle(self) -> None:
        """Слияние реплики прошло: сброс, сделанный держателем, учтён — отметка месяца
        здесь (иначе догон на старте сбросил бы месяц второй раз)."""
        ym = str(self.rsv_get("rsv_follow").get("monthly_deferred") or "")
        if ym and str(self.db.get_state("last_monthly_reset") or "") < ym:
            self.db.set_state("last_monthly_reset", ym)
            log.info("резерв: месячный сброс %s учтён слиянием", ym)
        if ym:
            self.rsv_update("rsv_follow", monthly_deferred="")

    def reserve_follow_clear(self) -> None:
        """Конец следования: блокировки держателя прочь; отложенный месячный сброс
        остаётся — его учитывает слияние после возврата (reserve_monthly_settle)."""
        f = self.rsv_get("rsv_follow")
        self.rsv_put("rsv_follow", {"monthly_deferred": f["monthly_deferred"]} if f.get("monthly_deferred") else None)
        self.__dict__.pop("_follow_seen_top", None)
        self.__dict__.pop("_follow_lag_since", None)

    # ── журнал: события и счётчики держателя ────────────────────────────────
    def _follow_jev_in(self, body: dict) -> list[tuple]:
        """События держателя линии (бот основы на резерве — через резерв; у гостя,
        следующего до передачи бота, — события основы): применить строго подряд с
        `applied + 1`, затем пиры и блокировки по итогу; `jack` — после применения.
        Следующий принимает эпоху держателя как есть (в любую сторону: смена роли без
        событий поднимает мою, а держатель — один); держатель здесь на события эпохи
        меньше своей отвечает `jstale`, остальное пропускает (роль решает сверка)."""
        from awgbot.util.rsvjrn import event
        from .ship import epoch_sane
        line, e_body = str(body["line"]), int(body["e"])
        meta = self.db.jrn_meta()
        top = int(body.get("from") or 0) + int(body.get("n") or 0) - 1
        if top > int(self.__dict__.get("_follow_seen_top") or 0):
            self.__dict__["_follow_seen_top"] = top            # что держатель уже прислал — мерка отставания
        if meta["role"] != "follower" or meta["holder"] != "peer":
            c9 = self.rsv_get("rsv_c9") if not getattr(self, "guest", False) else {}
            if c9 and e_body == int(c9.get("e") or -1):
                return self.reserve_c9_tail_in(body, c9)      # «вернуть без резерва»: его правки — хвост
            if e_body < int(meta["epoch"]):
                # точка расхождения — с какого номера моя эпоха: бывшему держателю — для хвоста
                return [("send", "jstale", {"line": line, "e": int(meta["epoch"]), "F": int(meta["fork_i"])})]
            log.info("резерв: события эпохи %s от «%s», а эпоха здесь — пропущены", e_body, self.reserve_peer_name())
            return []
        if not epoch_sane(e_body, int(meta["epoch"])):
            log.warning("резерв: события эпохи %s от «%s» — вне разумного, пропущены", e_body, self.reserve_peer_name())
            return []
        try:
            k = self.reserve_k()
            evs = [event.open_event(k, r, line) for r in event.split(bytes.fromhex(str(body["blob"])))]
        except (ValueError, event.RecordError) as e:
            log.warning("резерв: события от «%s» не открылись: %s", self.reserve_peer_name(), e)
            return [("send", "jneed", {"line": line, "from": int(meta["applied"]) + 1})]
        if any(ev.e != e_body for ev in evs):
            return [("send", "jneed", {"line": line, "from": int(meta["applied"]) + 1})]
        if evs and self.db.jrn_tail_held():
            if evs[0].i == int(meta["applied"]) + 1:
                self.reserve_tail_finalize(int(meta["applied"]))   # точка — моя же: держатель начал сразу после
                meta = self.db.jrn_meta()
            else:
                # мой хвост отложен, а точку расхождения держатель не назвал: из разрыва в номерах её
                # не угадывать — ждать st резерва (точка гостя); держатель повторит без подтверждения
                log.info("резерв: события держателя с %s, мой хвост ждёт точку расхождения — жду её", evs[0].i)
                return []
        applied = int(meta["applied"])
        evs = [ev for ev in evs if ev.i > applied]
        if evs and [ev.i for ev in evs] != list(range(applied + 1, applied + 1 + len(evs))):
            return [("send", "jneed", {"line": line, "from": applied + 1})]   # разрыв или перестановка — не применять
        if e_body != int(meta["epoch"]):
            self.db.jrn_adopt_epoch(e_body)                   # держатель один — номер его эпохи и здесь
        if evs:
            before = self._follow_watch()
            res = self.db.jrn_apply(evs)
            self._follow_after(res.done, before)
            if any(ev.t == "devices" for ev in res.done):
                self.db.jrn_keys_restore()                    # закрытые ключи откаченных устройств — на место
            for ev, why in res.lost:
                log.warning("резерв: событие %s (%s %s) не встало: %s", ev.i, ev.op, ev.t, why)
            applied = res.last
        return [("send", "jack", {"line": line, "e": e_body, "i": applied})] + self._jrn_back_caught_up()

    def _follow_jcnt_in(self, body: dict) -> list[tuple]:
        """Пачка счётчиков второй стороны — сложить: у основы — клона (b), расход через
        резерв — в итог; у бота основы на резерве — оставшихся на основе сессий (a)."""
        from awgbot.util.rsvjrn import event
        line = str(body["line"])
        try:
            side, batch = event.open_counts(self.reserve_k(), bytes.fromhex(str(body["blob"])), line)
        except (ValueError, event.RecordError) as e:
            log.warning("резерв: пачка счётчиков от «%s» не открылась: %s", self.reserve_peer_name(), e)
            return []
        guest = bool(getattr(self, "guest", False))
        if side != ("a" if guest else "b") or batch.bn != int(body["s"]):
            return []
        got = self.db.jrn_counts_apply(batch)
        if got is None:
            return []                                         # месяц пачки новее моего — повторит после сброса
        if got and not guest:
            rx = sum(v[0] for devs in batch.months.values() for v in devs.values())
            tx = sum(v[1] for devs in batch.months.values() for v in devs.values())
            try:
                self.reserve_traffic_out_add(rx, tx)
            except Exception as e:                            # noqa: BLE001
                log.info("резерв: расход через резерв не учтён: %s", e)
        return [("send", "jcack", {"line": line, "side": "b", "s": batch.bn})]

    def _follow_watch(self) -> dict:
        admin = self.db.get_client_by_tg(int(config.ADMIN_ID)) if hasattr(self.db, "get_client_by_tg") else None
        return {"admin": int(admin.id) if admin is not None else 0}

    def _follow_after(self, done: list, before: dict) -> None:
        """Пиры и блокировки по применённому; сводка эпохи для Т-РУ5б."""
        from awgbot.infra import awg
        rep = dict(self.rsv_get("rsv_out").get("jrn_report") or {})
        for k in ("clients", "devices", "extensions", "blocks", "peers_added", "peers_removed"):
            rep[k] = int(rep.get(k) or 0)
        lost = list(rep.get("lost") or [])
        blocks = False
        name = self.reserve_peer_name()
        for ev in done:
            if ev.t == "clients":
                if ev.op == "I":
                    rep["clients"] += 1
                old_tg = (ev.o or {}).get("tg_id")
                new_tg = (ev.c or {}).get("tg_id")
                if ev.op == "U" and old_tg and "tg_id" in (ev.c or {}) and new_tg != old_tg:
                    cl = self.db.get_client(int(ev.k.get("id") or 0)) if hasattr(self.db, "get_client") else None
                    cname = cl.name if cl is not None else f"#{ev.k.get('id')}"
                    lost.append(f"⚠️ У профиля «{_e(cname)}» на «{_e(name)}» сменён Telegram-аккаунт "
                                f"({old_tg} → {new_tg}) — проверь, что это сделал ты")
                blocks = blocks or "block_reason" in (ev.c or {})
            elif ev.t == "client_subscription" and ev.op == "U":
                rep["extensions"] += 1
            elif ev.t == "client_block_histories" and ev.op == "I":
                rep["blocks"] += 1
            elif ev.t == "devices":
                blocks = blocks or "block_reason" in (ev.c or {})
                if ev.op == "I":
                    rep["devices"] += 1
                    if before.get("admin") and int((ev.c or {}).get("client_id") or 0) == before["admin"]:
                        lost.append(f"⚠️ У профиля админа появилось устройство «{_e((ev.c or {}).get('name'))}», "
                                    f"заведённое на «{_e(name)}» — проверь, что это твоё")
                rep["peers_added"] += self._follow_peer(awg, ev)
                rep["peers_removed"] += self._follow_peer_gone(awg, ev)
        rep["lost"] = lost
        self.rsv_update("rsv_out", jrn_report=rep)
        if blocks:
            try:
                self.reconcile_blocks(True)
            except Exception as e:                            # noqa: BLE001
                log.warning("резерв: блокировки по решениям держателя не наложены: %s", e)

    def _follow_peer(self, awg, ev) -> int:
        """Устройство появилось или сменило ключ, адрес, интерфейс — пир по строке базы
        после применения (прежний ключ снимается)."""
        c, old = ev.c or {}, ev.o or {}
        keys = ("public_key", "address", "iface", "preshared_key")
        if ev.op not in ("I", "U"):
            return 0
        # событие U несёт строку целиком — пир трогаем, только если сменилось то, что в нём
        if ev.op == "U" and not any(k in c and c.get(k) != old.get(k) for k in keys):
            return 0
        if ev.op == "U" and old.get("public_key"):
            try:
                awg.remove_peer(str(old["public_key"]), awg.iface_of(old.get("iface") if "iface" in old else c.get("iface")))
            except Exception as e:                            # noqa: BLE001
                log.info("резерв: прежний пир не снят: %s", e)
        try:
            dev = self.db.get_device(int(ev.k.get("id") or 0))
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: устройство %s по событию не прочитано — пир не поставлен: %s", ev.k.get("id"), e)
            dev = None
        if dev is None or not dev.public_key or getattr(dev, "is_gateway", 0):
            return 0
        try:
            awg.add_peer(dev.public_key, dev.preshared_key or "", dev.address, awg.iface_of(dev.iface))
            return 1
        except Exception as e:                                # noqa: BLE001
            log.warning("резерв: пир устройства «%s» не добавлен: %s", dev.name, e)
            return 0

    @staticmethod
    def _follow_peer_gone(awg, ev) -> int:
        if ev.op != "D" or not (ev.o or {}).get("public_key"):
            return 0
        try:
            awg.remove_peer(str(ev.o["public_key"]), awg.iface_of((ev.o or {}).get("iface")))
            return 1
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: пир удалённого устройства не снят: %s", e)
            return 0

    # ── отставание (Т-Ж1, Т-Ж2, Т-Ж6) ───────────────────────────────────────
    FOLLOW_LAG_S = 60

    def _follow_lag(self) -> tuple[bool, int, int]:
        """(отстаю ли дольше FOLLOW_LAG_S, применено, последнее у бота на резерве). Последнее —
        по тому, что он прислал (`_follow_seen_top`, и не применённое тоже), а не только по
        `st.jrn.keep.guest_last`: `st` уходит по смене линии, эпохи и снимка, в следовании
        число в нём застывает."""
        import time
        meta = self.db.jrn_meta()
        applied = int(meta["applied"])
        try:
            last = int((self.reserve_peer_state().get("jrn_keep") or {}).get("guest_last") or 0)
        except (TypeError, ValueError):
            last = 0
        last = max(last, int(self.__dict__.get("_follow_seen_top") or 0))
        now = time.monotonic()
        if last <= applied:
            self.__dict__.pop("_follow_lag_since", None)
            return False, applied, last
        since = self.__dict__.setdefault("_follow_lag_since", now)
        return now - since > self.FOLLOW_LAG_S, applied, last

    def reserve_follow_view(self) -> dict:
        """Строка карточки в отъезде с журналом: «идут за ботом … по <время>» или «догоняют»."""
        if not self.reserve_following() or not self.reserve_jrn_moved():
            return {}
        lag, applied, _last = self._follow_lag()
        at = ""
        rows = self.db.jrn_events_after(max(0, applied - 1), 1)
        if rows and rows[0].i == applied:
            at = rows[0].at
        return {"lag": lag, "at": at}

    def reserve_follow_lag_tick(self) -> list[tuple]:
        """Т-Ж6: отставание дольше минуты — один раз на расхождение; догнал — 🟢."""
        from awgbot.domain.notices import reserve as rn
        if not self.reserve_following() or not self.reserve_jrn_moved():
            return []
        lag, applied, last = self._follow_lag()
        told = bool(self.rsv_get("rsv_follow").get("lag_told"))
        if lag and not told:
            self.rsv_update("rsv_follow", lag_told=True)
            log.warning("резерв: журнал отстаёт от бота на «%s»: применено %s, у него %s", self.reserve_peer_name(),
                        applied, last)
            view = self.reserve_follow_view()
            why = (f"данные по {timeutil.fmt_clock_ui(timeutil.parse_iso(view['at']))}" if view.get("at") else
                   "отстают")
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID), silent=True,
                                             text=rn.follow_stalled(self.reserve_peer_name(), why))])]
        if told and not lag and last <= applied:
            self.rsv_update("rsv_follow", lag_told=False)
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID), silent=True,
                                             text=rn.follow_ok(self.reserve_peer_name()))])]
        return []

    # ── отказ базы ──────────────────────────────────────────────────────────
    def reserve_refusal(self, exc: BaseException) -> ServiceError | None:
        """Отказ базы в записи решения (следование) — словами раздела; не отказ — None."""
        from awgbot.util import rsvjrn
        if rsvjrn.is_refusal(exc):
            return ServiceError(AWAY_REFUSED.format(name=self.reserve_peer_name()))
        return None
