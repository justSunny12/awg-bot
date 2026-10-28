"""
ship.py — часть пакета journal (см. __init__): своя линия данных к резерву —
события держателя и пачки счётчиков основы.

Направление идёт журналом, когда у обеих сторон `jrn1` (compat.journal_pair):
захват событий включён (`jrn_meta.capture`), пакет на каждое изменение не
собирается — вместо него события пачками ≤ 1 с (такт журнала) и снимок
(`pkg3`, package) по правилам: нет снимка у резерва, разрыв журнала, много
событий после снимка, раз в сутки, смена ключей и частей вне базы.

Сверка до первой отправки в сессии: пока не пришёл `st` резерва с его
взглядом на мою линию (`st.jrn.keep`), не уходит ничего. Снимка этой базы у
резерва нет, у резерва номер больше моего последнего (база потеряла хвост
при пропаже питания или восстановлена) или нужных ему событий у меня уже
нет — снимок со сбросом (`reset`: резерв снимает всё своё по этой линии и
начинает с номера снимка); иначе — досылка с его последнего + 1.
Подтверждённое резервом (`jack`, не дальше отправленного) удаляется; без
подтверждения дольше RESEND_S — повтор с подтверждённого. Смена `K` — тоже
снимок со сбросом: записи под прежним ключом новым не открыть.

Не шлётся ничего, пока идёт возврат (решения нового держателя не уходят под
прежним `K`, который резерв знал), и событий — пока резерв несёт моих
(serving): держатель там, сверка роли переведёт меня в следование.

Пачки счётчиков — по таймеру `pkg_minutes` ± 40 % и с ближайшей пачкой
событий, по одной: неподтверждённая повторяется той же (infra/db/jrn
`jrn_counts_out`), новая — после `jcack`; пока снимок в пути — не
сворачиваются (строки по `cs` снимка учтены в нём, пачки по `cb` — тоже).
Получатели пачек основы — резерв (для открытия) и, в отъезде, бот основы на
резерве (лимит по сумме).
"""
from __future__ import annotations

import random
import time

from awgbot.core import settings
from awgbot.util import pairproto, timeutil

from ...gateway_link.common import log
from .. import compat
from ..state import _LOCK

EV_LIMIT = 256                    # событий в одной пачке
EV_BYTES = pairproto.CHUNK_BYTES - 4096   # запись целиком в одно сообщение (hex — в MAX_BLOB)
RESEND_S = 20
SNAP_EVENTS = 2000                # событий у резерва после снимка — пора снимок
SNAP_BYTES = 1024 * 1024
SNAP_DAY_S = 24 * 3600
CAPS_TTL_S = 60
EPOCH_STEP_MAX = 1000             # эпоха от второй стороны — не дальше моей на столько (мусор и подделка)
EPOCH_MAX = 2 ** 62


def epoch_sane(e: int, mine: int) -> bool:
    """Эпоха из сообщения второй стороны годится: в пределах заголовка и недалеко от моей."""
    return 0 <= int(e) < EPOCH_MAX and int(e) <= int(mine) + EPOCH_STEP_MAX


class ReserveShipMixin:

    # ── вкл/выкл ────────────────────────────────────────────────────────────
    def _jrn_caps(self) -> list[str]:
        c = self.__dict__.get("_jrn_caps_cache")
        now = time.monotonic()
        if c is None or now - c[0] > CAPS_TTL_S:
            c = (now, compat.caps())
            self.__dict__["_jrn_caps_cache"] = c
        return c[1]

    def reserve_jrn_on(self) -> bool:
        """Моя линия идёт к резерву журналом: пара, `jrn1` у обоих."""
        if not self.reserve_paired():
            return False
        peer = self.reserve_peer_state()
        return compat.journal_pair(self._jrn_caps(), list(peer.get("caps") or []))

    def reserve_jrn_moved(self) -> bool:
        """Мои пользователи у резерва ушли журналом: у него открыт снимок `pkg3` (а не
        пакет прежнего пути, если журнал включился, а снимок не успел)."""
        return self.reserve_jrn_on() and bool(self.rsv_get("rsv_pkg_sent").get("jrn"))

    def reserve_jrn_sync(self) -> None:
        """Захват — по направлению; смена — снимок (или пакет прежнего пути) заново.
        У бота основы на резерве захват ставит процесс открытия, а не пара."""
        if getattr(self, "guest", False):
            return
        try:
            changed = self.db.jrn_capture_set(self.reserve_jrn_on())
            self.__dict__["_jrn_capture_on"] = bool(self.db.jrn_meta()["capture"])
        except Exception as e:                                # noqa: BLE001 — база старше журнала (тесты)
            log.debug("резерв: захват журнала не переключён: %s", e)
            return
        if changed:
            on = bool(self.db.jrn_meta()["capture"])
            log.info("резерв: данные для переезда к «%s» — %s", self.reserve_peer_name(),
                     "журналом правок" if on else "пакетом целиком (у резерва нет журнала)")
            self.rsv_put("rsv_jrn_out", None)
            self._jrn_sess_reset()
            self.reserve_pkg_mark()

    def _jrn_touch(self) -> None:
        self.__dict__["_jrn_due_at"] = time.monotonic()

    # ── сессия ──────────────────────────────────────────────────────────────
    def _jrn_sess(self) -> dict:
        s = self.__dict__.get("_jrn_sess_d")
        if s is None:
            s = self.__dict__["_jrn_sess_d"] = {"ok": False, "hold": True, "sent": 0, "sent_at": 0.0, "top": 0}
        return s

    def _jrn_sess_reset(self) -> None:
        """Новая сессия пары (или смена направления): ничего не слать до сверки."""
        self.__dict__["_jrn_sess_d"] = None

    def _jrn_out(self) -> dict:
        return self.rsv_get("rsv_jrn_out")

    def _jrn_snap_need(self, why: str, *, reset: bool = True) -> None:
        """Снимок раньше событий; reset — резерв снимает всё своё по этой линии."""
        s = self._jrn_sess()
        if not s.get("snap_why"):
            log.info("резерв: журнал к «%s» — сначала снимок (%s)", self.reserve_peer_name(), why)
        s.update(ok=True, hold=True, snap_why=why)
        if reset:
            self.rsv_update("rsv_jrn_out", reset=True)
        self.reserve_pkg_mark()

    def reserve_jrn_new_k(self) -> None:
        """Новый `K`: записи у резерва под прежним ключом новым не открыть — снимок со сбросом."""
        if self.reserve_jrn_on() and not getattr(self, "guest", False):
            self._jrn_snap_need("новый ключ пакета")

    def reserve_jrn_keep_in(self, keep: dict | None) -> None:
        """Взгляд резерва на мою линию из его `st` — сверка до первой отправки."""
        if not self.reserve_jrn_on():
            return
        s = self._jrn_sess()
        line = self.db.jrn_line()
        keep = dict(keep or {})
        try:
            last, cnt_bn = int(keep.get("last") or 0), int(keep.get("cnt_bn") or 0)
        except (TypeError, ValueError):
            last, cnt_bn = 0, 0
        if int(keep.get("size") or 0) > SNAP_BYTES:
            self.reserve_pkg_mark()                           # плановый снимок: у резерва больше мегабайта событий
        if str(keep.get("line") or "") == line and cnt_bn > self.db.jrn_counts_bn():
            self.db.jrn_counts_raise(cnt_bn)                  # счёт пачек — не ниже принятого резервом
        if str(keep.get("line") or "") == line and keep.get("guest_fork"):
            try:
                self.reserve_tail_finalize(int(keep["guest_fork"]))   # точка гостя пришла — отложенный хвост готовить
            except (TypeError, ValueError):
                pass
        if s.get("ok"):
            return
        s["ok"] = True
        mine = self.db.jrn_last_i()
        if str(keep.get("line") or "") != line or not (keep.get("snap") or int(keep.get("snap_i") or 0)):
            self._jrn_snap_need("у резерва нет снимка этой базы")
            return
        if last > mine:
            self._jrn_snap_need(f"у резерва события до {last}, здесь до {mine}")
            return
        first = self.db.jrn_first_i()
        if last < mine and (not first or first > last + 1):
            self._jrn_snap_need("событий, которых нет у резерва, здесь уже нет")
            return
        self.db.jrn_acked_set(last)
        s.update(hold=False, sent=last, top=max(int(s.get("top") or 0), last), sent_at=0.0, snap_why="",
                 ack_at=timeutil.now_iso())

    def reserve_jrn_snap_ok(self, marks: dict) -> None:
        """Резерв принял снимок `pkg3`: всё по номер снимка у него — подтверждено."""
        i, cs, cb = int(marks.get("i") or 0), int(marks.get("cs") or 0), int(marks.get("cb") or 0)
        self.db.jrn_acked_set(i)
        self._jrn_sess()["ack_at"] = timeutil.now_iso()
        self.db.jrn_snap_set(i)
        self.db.jrn_counts_cover(cs, cb)
        self.__dict__.pop("_jrn_snap_pending", None)
        out = self._jrn_out()
        self.rsv_put("rsv_jrn_out", {**out, "snap_i": i, "cs": cs, "cb": cb, "reset": False,
                                     "snap_at": timeutil.now_iso(), "snap_next": self._jrn_snap_next()})
        s = self._jrn_sess()
        s.update(ok=True, hold=False, sent=max(int(s.get("sent") or 0), i), top=max(int(s.get("top") or 0), i),
                 sent_at=0.0, snap_why="")

    def reserve_jrn_snap_dropped(self) -> None:
        """Снимок не принят или не дошёл — пачки счётчиков снова сворачиваются."""
        self.__dict__.pop("_jrn_snap_pending", None)

    @staticmethod
    def _jrn_snap_next() -> str:
        return timeutil.to_iso(timeutil.shift(timeutil.now(), SNAP_DAY_S * random.uniform(0.6, 1.4)))

    def reserve_jrn_snap_due(self) -> bool:
        """Пора ли плановый снимок: много событий после снимка или сутки, если события были."""
        out = self._jrn_out()
        if self.db.jrn_last_i() - int(out.get("snap_i") or 0) > SNAP_EVENTS:
            return True
        nxt = str(out.get("snap_next") or "")
        if not nxt or self.db.jrn_last_i() <= int(out.get("snap_i") or 0):
            return False
        try:
            return timeutil.parse_iso(nxt) <= timeutil.now()
        except ValueError:
            return True

    # ── такт ────────────────────────────────────────────────────────────────
    def _jrn_quiet(self) -> bool:
        """Возврат идёт — журнал молчит: новый держатель пишет под прежним K, который резерв
        знал; после возврата — новый K и снимок со сбросом."""
        return str(self.rsv_get("rsv_out").get("state") or "home") == "returning"

    def _jrn_peer_serving(self) -> bool:
        return str(self.reserve_peer_state().get("inn") or "idle") == "serving"

    def reserve_jrn_tick(self) -> list[tuple]:
        """Такт журнала (1 с): события и пачки счётчиков к резерву."""
        self.reserve_jrn_sync()
        if not self.reserve_jrn_on() or not self.reserve_online() or self._jrn_quiet():
            return []
        s = self._jrn_sess()
        if not s.get("ok") or s.get("hold"):
            return []
        acts: list[tuple] = self.reserve_tail_out() + self.reserve_follow_lag_tick()
        meta = self.db.jrn_meta()
        now = time.monotonic()
        acked = int(meta["acked"])
        if int(s.get("sent") or 0) > acked and now - float(s.get("sent_at") or 0) > RESEND_S:
            s["sent"] = acked                                  # подтверждения нет — повтор с подтверждённого
        if meta["role"] == "holder" and not self._jrn_peer_serving():
            acts += self._jrn_events_out(s, int(meta["epoch"]), now)
        if acts or self._jrn_cnt_timer_due(now):
            acts += self._jrn_counts_out()
        return acts

    def _jrn_events_out(self, s: dict, epoch: int, now: float) -> list[tuple]:
        from awgbot.util.rsvjrn import event
        evs = self.db.jrn_events_after(int(s.get("sent") or 0), EV_LIMIT, epoch=epoch)
        if not evs:
            return []
        k, line = self.reserve_k(), self.db.jrn_line()
        recs, size = [], 0
        for ev in evs:
            r = event.seal_event(k, line, ev)
            if recs and (size + len(r) > EV_BYTES or ev.i != evs[0].i + len(recs)):
                break
            recs.append(r)
            size += len(r)
        last = evs[0].i + len(recs) - 1
        s.update(sent=last, top=max(int(s.get("top") or 0), last), sent_at=now)
        return [("send", "jev", {"line": line, "e": epoch, "from": evs[0].i, "n": len(recs),
                                 "blob": b"".join(recs).hex()})]

    def _jrn_cnt_timer_due(self, now: float) -> bool:
        nxt = self.__dict__.get("_jrn_cnt_at")
        if nxt is None:
            self.__dict__["_jrn_cnt_at"] = now + self._jrn_cnt_delay()
            return False
        if now >= nxt:
            self.__dict__["_jrn_cnt_at"] = now + self._jrn_cnt_delay()
            return True
        return False

    @staticmethod
    def _jrn_cnt_delay() -> float:
        return max(1, settings.get_int("app.reserve.pkg_minutes", 30)) * 60 * random.uniform(0.6, 1.4)

    def _jrn_counts_out(self, side: str = "a") -> list[tuple]:
        from awgbot.util.rsvjrn import event
        with _LOCK:                                           # снимок собирается под тем же замком
            if self.__dict__.get("_jrn_snap_pending"):
                return []                                     # строки по cs снимка учтены в нём — не сворачивать
            line = self.db.jrn_line()
            batch = self.db.jrn_counts_out(f"{line}:{side}")
        if batch is None:
            return []
        rec = event.seal_counts(self.reserve_k(), line, side, batch)
        if len(rec) * 2 > pairproto.MAX_BLOB:
            if not self.__dict__.get("_jrn_cnt_big"):
                log.warning("резерв: пачка счётчиков %d КБ больше части канала — не уходит", len(rec) // 1024)
                self.__dict__["_jrn_cnt_big"] = True
            return []
        return [("send", "jcnt", {"line": line, "side": side, "s": batch.bn, "blob": rec.hex()})]

    def reserve_jrn_flush(self) -> list[tuple]:
        """Передача эпохи: всё неотправленное — сейчас (события и счётчики)."""
        if not self.reserve_jrn_on():
            return []
        s = self._jrn_sess()
        if not s.get("ok") or s.get("hold"):
            return []
        meta = self.db.jrn_meta()
        acts = self._jrn_events_out(s, int(meta["epoch"]), time.monotonic()) if meta["role"] == "holder" else []
        return acts + self._jrn_counts_out()

    def reserve_jrn_synced_at(self) -> str:
        """С какого времени данные у резерва такие же, как здесь (строка карточки «данные для
        переезда у … от …»): всё подтверждено и связь есть — сейчас; иначе — последнее
        подтверждение этой сессии. Пусто — журнал не идёт (тогда — время снимка)."""
        if not self.reserve_jrn_on():
            return ""
        s = self._jrn_sess()
        if s.get("ok") and not s.get("hold") and self.reserve_online() and self.reserve_jrn_all_acked():
            return timeutil.now_iso()
        return str(s.get("ack_at") or "")

    def reserve_jrn_all_acked(self) -> bool:
        """Резерв подтвердил всё моё (события этой эпохи): эпоху можно передавать."""
        meta = self.db.jrn_meta()
        return not self.db.jrn_events_after(int(meta["acked"]), 1, epoch=int(meta["epoch"]))

    # ── ответы резерва на мою линию ─────────────────────────────────────────
    def _jrn_ack_in(self, i: int) -> None:
        s = self._jrn_sess()
        if int(i) > int(s.get("top") or 0):
            log.warning("резерв: «%s» подтверждает событие %s, а отправлено по %s — пропущено",
                        self.reserve_peer_name(), i, s.get("top") or 0)
            return
        self.db.jrn_acked_set(i)
        s["ack_at"] = timeutil.now_iso()
        if int(s.get("sent") or 0) < i:
            s["sent"] = i

    def _jrn_need_in(self, frm: int) -> None:
        s = self._jrn_sess()
        if frm <= 0:
            self._jrn_snap_need("резерв просит снимок")
            return
        last = self.db.jrn_last_i()
        if frm > last + 1:
            log.warning("резерв: «%s» просит события с %s, а последнее здесь %s — пропущено",
                        self.reserve_peer_name(), frm, last)
            return
        first = self.db.jrn_first_i()
        if first and first <= frm:
            s.update(sent=frm - 1, sent_at=0.0)
        elif frm == last + 1:
            s.update(sent=last, sent_at=0.0)
        else:
            self._jrn_snap_need(f"резерв просит события с {frm}, их здесь уже нет")

    def _jrn_cack_in(self, bn: int) -> None:
        self.db.jrn_counts_acked(int(bn))
