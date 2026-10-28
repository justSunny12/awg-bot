"""
keep.py — часть пакета journal (см. __init__): сторона резерва — хранение
журнала второй стороны (её линии данных).

События основы (`jev`) и её пачки счётчиков (`jcnt`, сторона a) ложатся
шифротекстом под её `K` в `reserve/jrn/` (rsvjrn.segments): бот резерва
проверяет только открытые заголовки — линия та, что у снимка, эпоха не
меньше известной, номера подряд — и подтверждает после fsync (`jack`,
`jcack`). Разрыв в номерах — `jneed` с первого недостающего; линия без
снимка — `jneed` с нуля (основа шлёт снимок); эпоха меньше известной —
`jstale`. Снимок (`pkg3`) снимает всё, что в нём учтено.

Пока у меня бот основы на резерве (гость), записи основы идут и ему — по
локальному каналу (он следует за основой до передачи бота и складывает
счётчики её оставшихся сессий); его записи и подтверждения основы —
транзитом (route). Его эпоха и номера — из его hello/online
(`reserve_guest_jrn_seen`) — в `st.jrn.keep.guest_*`. `st.jrn.keep` — что я
храню: основа сверяется по нему до первой отправки.
"""
from __future__ import annotations

import os
import time

from awgbot.core import config
from awgbot.util import timeutil

from ...types import Notification

from ...gateway_link.common import log
from ..package import reserve_dir


class ReserveKeepMixin:

    def reserve_keep_store(self):
        from awgbot.util.rsvjrn.segments import Store
        return Store(os.path.join(reserve_dir(), "jrn"))

    def _keep(self) -> dict:
        return self.rsv_get("rsv_jrn_in")

    def reserve_keep_line(self) -> str:
        return str(self._keep().get("line") or "")

    def reserve_keep_snapshot_in(self, marks: dict) -> None:
        """Принят снимок `pkg3` с отметками журнала: новая линия или сброс (база основы
        потеряла хвост, восстановлена, новый K) — хранилище заново; учтённое снимком
        (события по `i`, пачки по `cb`) — прочь."""
        try:
            line, e, i, cb = (str(marks.get("line") or ""), int(marks.get("e") or 0), int(marks.get("i") or 0),
                              int(marks.get("cb") or 0))
        except (TypeError, ValueError):
            return
        if not line:
            return
        st = self.reserve_keep_store()
        cur = self._keep()
        prev_e = int(cur.get("e") or 0) if str(cur.get("line") or "") == line else 0   # эпоха линии только растёт
        if str(cur.get("line") or "") != line or marks.get("reset"):
            st.wipe()
            cur = {}
        dropped = st.drop_events_upto(i)
        st.drop_counts_upto("a", cb)
        last_e, last_i = st.last()
        known = max(e, last_e, prev_e)
        self.rsv_put("rsv_jrn_in", {"line": line, "e": known, "snap_i": i, "cb": cb, "last": max(i, last_i),
                                    "snap_at": timeutil.now_iso()})
        self._keep_meta_write(line, known)
        self.__dict__["_keep_synced"] = True
        if dropped:
            log.info("резерв: снимок «%s» по %d — снято событий журнала: %d", self.reserve_peer_name(), i, dropped)

    def _keep_meta_write(self, line: str, e: int) -> None:
        """Эпоха линии, известная резерву, — процессу открытия (он не видит базу резерва):
        гость откроется с эпохой больше неё."""
        import json
        from awgbot.infra import rsvzone
        try:
            rsvzone.write_private(self.reserve_keep_store().path / "meta.json", json.dumps({"line": line, "e": int(e)}))
        except OSError as ex:
            log.info("резерв: отметка эпохи журнала не записана: %s", ex)

    def reserve_keep_mine_in(self, mine: dict | None) -> None:
        """`st.jrn.mine` основы: её эпоха (смена роли без событий её поднимает) — в известную."""
        from .ship import epoch_sane
        cur = self._keep()
        try:
            line, e = str((mine or {}).get("line") or ""), int((mine or {}).get("e") or 0)
        except (TypeError, ValueError):
            return
        if not cur.get("line") or line != str(cur["line"]) or e <= int(cur.get("e") or 0):
            return
        if not epoch_sane(e, int(cur.get("e") or 0)):
            log.warning("резерв: «%s» называет эпоху %s своей линии — вне разумного, пропущено", self.reserve_peer_name(), e)
            return
        self.rsv_update("rsv_jrn_in", e=e)
        self._keep_meta_write(line, e)

    def reserve_keep_drop(self) -> None:
        """Пара снята или возврат: журнал второй стороны — прочь (его K я знал)."""
        try:
            self.reserve_keep_store().wipe()
        except OSError as e:
            log.info("резерв: журнал второй стороны не снят: %s", e)
        self.rsv_put("rsv_jrn_in", None)

    def reserve_keep_view(self) -> dict:
        """Что храню о линии второй стороны — в `st.jrn.keep`."""
        cur = self._keep()
        if not cur.get("line"):
            return {}
        last = int(cur.get("last") or 0)
        size, cnt_last = self._keep_disk_view()
        return {"line": str(cur["line"]), "e": int(cur.get("e") or 0), "last": last, "snap_i": int(cur.get("snap_i") or 0),
                "size": size,
                "snap": bool(cur.get("snap_at")),             # снимок есть (и снятый до первого события, snap_i = 0)
                "cnt_bn": max(cnt_last, int(cur.get("cb") or 0)),
                **({"gap": int(self.rsv_get("rsv_in").get("jgap") or 0)} if self.reserve_hosting() and self.rsv_get("rsv_in").get("jgap") else {}),
                **self._keep_guest_disk(str(cur["line"]))}

    def _keep_disk_view(self) -> tuple[int, int]:
        """Размер хранилища событий и номер последней пачки — с диска, но перечитывается
        только при смене файлов (st собирается раз в 5 с; файлы пачек читать целиком каждый
        раз незачем)."""
        st = self.reserve_keep_store()
        try:
            stamp = tuple(sorted((n, os.stat(os.path.join(st.path, n)).st_size) for n in os.listdir(st.path)
                                 if n.endswith(".seg")))
        except OSError:
            stamp = ()
        cached = self.__dict__.get("_keep_disk_cache")
        if cached and cached[0] == stamp:
            return cached[1]
        view = (int(st.events_size()), int(st.counts_last("a")))
        self.__dict__["_keep_disk_cache"] = (stamp, view)
        return view

    def _keep_guest_disk(self, line: str) -> dict:
        """Гостя нет, а его журнал данных на диске есть (перезагрузка посреди обслуживания):
        основе — что он есть (она заберёт его возвратом)."""
        if self.reserve_hosting():
            return {}
        try:
            from awgbot.util.rsvjrn import event
            store = self.guest_jrn_store()
            try:
                stamp = max((os.stat(os.path.join(store.path, n)).st_mtime_ns for n in os.listdir(store.path)
                             if n.endswith(".seg")), default=0)
            except OSError:
                stamp = 0
            cached = self.__dict__.get("_keep_gj_cache")
            if cached and cached[0] == (line, stamp):
                return dict(cached[1])                        # st раз в 5 с — журнал гостя не перечитывать без смены
            recs = store.events()
        except Exception:                                     # noqa: BLE001
            return {}
        recs = [r for r in recs if event.head(r).line == line]
        out = {}
        if recs:
            h = event.head(recs[-1])
            out = {"gj_e": h.e, "gj_last": h.n}
        self.__dict__["_keep_gj_cache"] = ((line, stamp), dict(out))
        return out

    def reserve_guest_jrn_seen(self, jrn: dict) -> None:
        """Слово гостя о своём журнале (hello/online по локальному каналу): эпоха `e`, точка
        расхождения `fork`, последний номер `last` — числами, в памяти (гость переподключится
        и скажет снова); не числа — нули."""
        out = {}
        for key in ("e", "fork", "last"):
            try:
                out[key] = int((jrn or {}).get(key) or 0)
            except (TypeError, ValueError):
                out[key] = 0
        self.__dict__["_rsv_guest_jrn"] = out

    def _keep_guest_int(self, key: str) -> int:
        """Эпоха (`e`), точка (`fork`) или последний номер (`last`) моего гостя, если он
        поднят и сказал о себе; иначе 0."""
        if not self.reserve_hosting():
            return 0
        return int((self.__dict__.get("_rsv_guest_jrn") or {}).get(key) or 0)

    # ── приём от основы ─────────────────────────────────────────────────────
    def _keep_jev_in(self, body: dict) -> list[tuple]:
        from awgbot.util.rsvjrn import event
        cur = self._keep()
        line = str(body["line"])
        if not cur.get("line") or line != str(cur["line"]):
            return [("send", "jneed", {"line": line, "from": 0})]
        try:
            recs = event.split(bytes.fromhex(str(body["blob"])))
            heads = [event.head(r) for r in recs]
        except (ValueError, event.RecordError) as e:
            log.warning("резерв: события «%s» не разобрались: %s", self.reserve_peer_name(), e)
            return [("send", "jneed", {"line": line, "from": int(cur.get("last") or 0) + 1})]
        # известная эпоха линии: основы и, пока он поднят, моего гостя — события старше не храню
        e_body, known = int(body["e"]), max(int(cur.get("e") or 0), self._keep_guest_int("e"))
        if not heads or any(h.kind != "E" or h.line != line or h.e != e_body for h in heads) or \
                [h.n for h in heads] != list(range(int(body["from"]), int(body["from"]) + len(heads))):
            log.warning("резерв: события «%s» с заголовками не по порядку — отказ", self.reserve_peer_name())
            return [("send", "jneed", {"line": line, "from": int(cur.get("last") or 0) + 1})]
        if e_body < known:
            return [("send", "jstale", {"line": line, "e": known})]
        last = int(cur.get("last") or 0)
        if not self.__dict__.get("_keep_synced"):              # отметка могла отстать от сегмента (пропало питание)
            last = max(last, self.reserve_keep_store().last()[1])
            self.__dict__["_keep_synced"] = True
        new = [r for r, h in zip(recs, heads) if h.n > last]
        if new and event.head(new[0]).n != last + 1:
            return [("send", "jneed", {"line": line, "from": last + 1})]
        acts: list[tuple] = []
        if new:
            try:
                self.reserve_keep_store().append_events(new)   # fsync — до подтверждения
            except OSError as e:
                return self._keep_store_failed(e)             # без jack — основа повторит; сессию не рвать
            self.__dict__.pop("_keep_oserr_told", None)
            last = event.head(new[-1]).n
            self.rsv_update("rsv_jrn_in", e=e_body, last=last)
            if self.reserve_hosting():
                acts.append(("guest", "jev", {"line": line, "e": e_body, "from": event.head(new[0]).n,
                                              "n": len(new), "blob": b"".join(new).hex()}))
        return [("send", "jack", {"line": line, "i": last})] + acts

    def _keep_store_failed(self, e: OSError) -> list[tuple]:
        """Журнал второй стороны не дописан (диск кончился, ввод-вывод): строка — с демпфером,
        админу — один раз на эпизод; подтверждения нет, основа повторит с подтверждённого."""
        from awgbot.domain.notices import reserve_tail as rt
        now = time.monotonic()
        if now - float(self.__dict__.get("_keep_oserr_at") or 0) > 60:
            self.__dict__["_keep_oserr_at"] = now
            log.error("резерв: журнал «%s» не дописан: %s", self.reserve_peer_name(), e)
        if self.__dict__.get("_keep_oserr_told"):
            return []
        self.__dict__["_keep_oserr_told"] = True
        return [("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rt.keep_write_failed(self.reserve_peer_name(), str(e)))])]

    def _keep_jcnt_in(self, body: dict) -> list[tuple]:
        from awgbot.util.rsvjrn import event
        cur = self._keep()
        line, side = str(body["line"]), str(body["side"])
        if side != "a" or not cur.get("line") or line != str(cur["line"]):
            return []
        try:
            raw = bytes.fromhex(str(body["blob"]))
            h = event.head(raw)
        except (ValueError, event.RecordError):
            return []
        if h.kind != "a" or h.line != line or h.n != int(body["s"]) or h.size != len(raw):
            return []
        st = self.reserve_keep_store()
        acts: list[tuple] = []
        if h.n > st.counts_last("a") and h.n > int(cur.get("cb") or 0):
            st.append_counts("a", [raw])
            if self.reserve_hosting():
                acts.append(("guest", "jcnt", {"line": line, "side": "a", "s": h.n, "blob": raw.hex()}))
        return [("send", "jcack", {"line": line, "side": "a", "s": h.n})] + acts
