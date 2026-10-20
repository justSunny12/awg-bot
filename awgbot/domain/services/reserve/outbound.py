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
leaving/released (`taken` от резерва) → away. Отказ на любом шаге —
`back` резерву (снять зону) и home.

Возврат: away (здоров ≥ 5 мин: свой AWG, линк пары, шлюз) → по рычагу
«сразу» сам, «по кнопке» — Т-РУ6 через бот основы на резерве → returning/
back_req → `released` (реплика) → слияние, пиры, блокировки → restart →
take_tg (`deleteWebhook`, поллинг, `taken`) → record_back (запись → свой
адрес, факт) → `back` → relay (`relay{until}`, `wiped`) → новый `K`, пакет →
home. Подробности — в докстрингах шагов.
"""
from __future__ import annotations

import json
import os
import time

from awgbot.core import config, settings
from awgbot.util import timeutil
from awgbot.util.rsvpkg import bot as pkgbot, seal

from ..types import Notification, ServiceError
from ..gateway_link.common import log
from .state import _LOCK

PKG_WAIT_S = 120          # свежий пакет до `go`
UP_WAIT_S = 300           # резерв поднял клон
RECORD_WAIT_S = 1800      # факт записи
RELEASED_WAIT_S = 300     # `taken` после `released`; возврат: `released` после `back_req`
HEALTHY_S = 300           # «здоров» — столько подряд
RELAY_WAIT_S = 600        # `wiped` после `back`
MOVE_STEPS = ("pkg", "up", "record", "release", "released")
RETURN_STEPS = ("back_req", "merge", "restart", "take_tg", "record_back", "record_wait", "back", "relay")


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
        return bool(out.get("deadline")) and time.monotonic() > float(out["deadline"])

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

    def reserve_move_start(self, chat_id: int = 0, message_id: int = 0) -> dict:
        """«🔀 Перевести»: leaving/pkg, свежий пакет вне очереди."""
        why = self.reserve_move_check()
        if why:
            raise ServiceError(why)
        with _LOCK:
            out = self.reserve_set_out("leaving", kind="button", started_at=timeutil.now_iso(),
                                       msg=[int(chat_id), int(message_id)] if chat_id else [],
                                       target_ip=self.reserve_peer_ip(), record=self.reserve_dns_name(),
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
        if str(peer.get("inn") or "") in ("taking", "serving") and self.reserve_online():
            self.reserve_set_out("away", kind=str(peer.get("in_kind") or "dead"), step="", taken_at=timeutil.now_iso())
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
                return [("send", "go", {"k": self.reserve_k().hex(), "gen": int(sent["gen"]), "kind": "button"}), ("live",)]
            return []
        if step == "record":
            return self._tick_record(out, str(out.get("target_ip") or ""), "release")
        if step == "released" and self._expired(out):
            # резерв не ответил `taken` — право отвечать всё равно у вебхука: смотрим по факту
            self.reserve_set_out("away", kind="button", step="", taken_at=timeutil.now_iso())
            self.reserve_set_poll("away")
            return [("live",)]
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
        last = float(out.get("record_probe") or 0)
        if time.monotonic() - last < 20:
            return actions
        self.rsv_update("rsv_out", record_probe=time.monotonic())
        if self.reserve_dns_points_to(target):
            self._out_step(next_step, RELEASED_WAIT_S if next_step == "release" else 0, record_fact_at=timeutil.now_iso())
            actions += [("live",), ("release",) if next_step == "release" else ("send", "back", {})]
            if next_step == "back":
                self._out_step("relay", RELAY_WAIT_S, back_at=timeutil.now_iso())
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
        self._out_step("released", RELEASED_WAIT_S, released_at=timeutil.now_iso())
        self.reserve_set_poll("away")
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
            self.reserve_peer_name(), "button", timeutil.now_iso()))])]

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
        return bool(awg_ok and pair_ok and gw_ok)

    def _tick_away(self, out: dict) -> list[tuple]:
        now = time.monotonic()
        if self.reserve_healthy():
            since = float(out.get("healthy_mono") or 0)
            if not since:
                self.rsv_update("rsv_out", healthy_mono=now, healthy_since=timeutil.now_iso())
                return []
            if now - since < HEALTHY_S:
                return []
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
        if step == "back_req":
            if self._expired(out):
                # реплика не пришла: повторить просьбу (резерв мог лечь) — не чаще раза в срок
                self._out_step("back_req", RELEASED_WAIT_S)
                return [("send", "back_req", {})]
            return []
        if step == "restart":
            return [("restart",)]
        if step == "take_tg":
            return [("take_tg",)]
        if step == "record_back":
            return self._tick_record(out, self.reserve_own_ip(), "back")
        if step == "relay":
            if self._expired(out):
                return self._return_done([])
            return []
        return []

    # ── возврат: события канала ─────────────────────────────────────────────
    def reserve_released_in(self, blob: bytes) -> list[tuple]:
        """Реплика бота основы с резерва: слияние, пиры, блокировки → restart."""
        out = self._out()
        if out.get("state") != "returning" or out.get("step") not in ("back_req", "merge"):
            log.info("резерв: released вне возврата — пропущен")
            return []
        self._out_step("merge")
        try:
            report = self._return_merge(blob, int(out.get("gen") or 0))
        except Exception as e:                                # noqa: BLE001
            log.error("резерв: слияние не удалось: %s — клиенты остаются у резерва", e)
            self.reserve_set_out("away", kind=str(out.get("kind") or ""), step="", merge_error=str(e)[:200])
            from awgbot.domain.notices import reserve as rn
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                             text=rn.return_failed(self.reserve_peer_name(), str(e)))])]
        self.rsv_update("rsv_out", merge_report=report)
        self._out_step("restart")
        return [("restart",)]

    def _return_merge(self, blob: bytes, gen: int) -> dict:
        from awgbot.domain.backup.merge import reserve as merge_mod
        from awgbot.infra import awg
        snap = seal.snapshot_open(self.reserve_k(), blob)
        if not isinstance(snap, dict) or not snap.get("db"):
            raise ServiceError("реплика не того вида")
        base_dir = os.path.join(str(config.DATA_DIR), "reserve", "out")
        base = os.path.join(base_dir, f"base-{gen}.db")
        if not os.path.exists(base):
            raise ServiceError(f"нет снимка base-{gen}.db")
        rep = os.path.join(base_dir, f"rep-{gen}.db")
        with open(rep, "wb") as f:
            f.write(bytes.fromhex(str(snap["db"])))
        os.chmod(rep, 0o600)
        try:
            removed = self._pubkeys_removed(base, rep)
            rpt = merge_mod.merge(self._db_path(), base, rep, admin_tg=int(config.ADMIN_ID))
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
                      "lost": [self._lost_line(x) for x in rpt.lost[:5]]}
            log.info("резерв: слияние: %s", json.dumps(report, ensure_ascii=False))
            return report
        finally:
            try:
                os.unlink(rep)
            except OSError:
                pass

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
        """На старте: возврат на шаге restart — пора забирать Telegram."""
        out = self._out()
        if out.get("state") == "returning" and out.get("step") in ("restart", "take_tg"):
            self._out_step("take_tg")
            return True
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
            # wiped может прийти первым сообщением новой сессии после обрыва — тоже в счёт
            if out.get("state") == "home" and out.get("await_wiped"):
                self.rsv_update("rsv_out", await_wiped=False)
                self.reserve_new_k()
                self.reserve_pkg_mark()
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
                             merge_report=dict(out.get("merge_report") or {}))
        tr = self._reserve_traffic_view().get("out") or ""
        notes = [Notification(tg_id=int(config.ADMIN_ID), text=rn.returned_home(name, dict(out.get("merge_report") or {}), tr))]
        if str(out.get("kind") or "") in ("dead", "rf", "nogw", "code"):
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
                "own": self.reserve_own_ip(), "merge_report": dict(out.get("merge_report") or {})}

    def _online_count(self) -> int:
        try:
            return int(self.db.get_state("online_count") or 0)
        except Exception:                                     # noqa: BLE001
            return 0
