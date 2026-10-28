"""
follow.py — часть пакета journal (см. __init__): основа в следовании.

Пока решения пишет бот основы на резерве, у основы остаются сессии
пользователей, ещё не переподключившихся к резерву. Их блокировки решает
держатель: бот основы на резерве шлёт адреса заблокированных устройств
(сообщение `blocks`, обратное прежнему), основа держит DROP по ним, а не по
своей устаревшей базе. Месячный сброс в следовании откладывается: его делает
держатель, а после возврата со слиянием он уже учтён (`reserve_monthly_settle`).
"""
from __future__ import annotations

import html

from awgbot.core import config
from awgbot.util import timeutil

from ...gateway_link.common import log
from ..away import AWAY_REFUSED
from ...types import ServiceError


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
        if meta["role"] != "follower" or meta["holder"] != "peer":
            if e_body < int(meta["epoch"]):
                return [("send", "jstale", {"line": line, "e": int(meta["epoch"])})]
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
        except Exception:                                     # noqa: BLE001
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

    # ── отказ базы ──────────────────────────────────────────────────────────
    def reserve_refusal(self, exc: BaseException) -> ServiceError | None:
        """Отказ базы в записи решения (следование) — словами раздела; не отказ — None."""
        from awgbot.util import rsvjrn
        if rsvjrn.is_refusal(exc):
            return ServiceError(AWAY_REFUSED.format(name=self.reserve_peer_name()))
        return None
