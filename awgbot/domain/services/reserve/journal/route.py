"""
route.py — часть пакета journal (см. __init__): разбор сообщений журнала по
линии данных.

Каждый сервер пары — основа своей линии и резерв линии второй стороны, и
сообщения журнала обеих линий идут одним каналом. Кто я для сообщения,
решает его линия:

| сообщение | моя линия (я основа) | линия второй стороны (я резерв) |
|---|---|---|
| `jev` | события гостя → применить (следование) | события основы → хранить |
| `jcnt` | пачка клона (b) → сложить | пачка основы (a) → хранить |
| `jack`, `jneed`, `jstale`, `jcack` | ответ резерва на мои записи | ответ основы на записи её бота у меня → ему |
| `jtail`, `jtail_ok` | хвост гостя (С9) и ответ на мой хвост (tail) | хвост основы после аварии и ответ её бота у меня → ему |
| `jc9none` | резерв: журнала моего бота у него после `last` нет — снять отметку С9 (c9) | не мне — пропустить |

Записи гостя приходят от него по локальному каналу
и уходят основе как есть — бот резерва видит только заголовки; гостя нет, а
основа ждёт его журнал по отметке С9, — его журнал с диска отдаёт бот резерва
сам (c9deliver), её `jack` и `jneed` тогда — ему. Набор видов —
pairproto.JRN_KINDS.
"""
from __future__ import annotations

from ...gateway_link.common import log


def _schema(kind: str, body: dict) -> dict:
    """Транзитом — только поля вида по словарю канала пары (лишнее не пересылается)."""
    from awgbot.util import pairproto
    fields = pairproto.PAIR_KINDS.get(kind, ("", {}))[1]
    return {k: body[k] for k in fields if k in body}


class ReserveJournalRouteMixin:

    def reserve_jrn_in(self, kind: str, body: dict) -> list[tuple]:
        """Сообщение журнала из канала пары → действия (ответы, гостю)."""
        line = str(body.get("line") or "")
        mine = self.db.jrn_line()
        if line and line == mine:
            return self._jrn_mine_in(kind, body)
        keep = self.reserve_keep_line()
        if kind in ("jev", "jcnt"):
            return self._keep_jev_in(body) if kind == "jev" else self._keep_jcnt_in(body)
        if line and line == keep:
            if kind == "jc9none":
                log.info("резерв: «jc9none» по линии, которую храню, — не мне, пропущено")
                return []
            if kind == "jneed":
                # основе не хватает её же событий (база потеряла хвост при пропаже питания) или
                # событий гостя, которого уже нет: журнал гостя, который она ждёт по отметке С9, —
                # с диска (c9deliver); что есть в хранилище — отдаю сам, остальное — гостю
                c9 = self.reserve_c9_deliver_need(int(body.get("from") or 0))
                if c9:
                    return c9
                acts = self._keep_serve_base(int(body.get("from") or 0))
                return acts or [("guest", kind, _schema(kind, body))]
            if kind == "jack" and not self.reserve_hosting():
                self.reserve_c9_deliver_acked(int(body["i"]))   # гостя нет — подтверждение моей доставки
            return [("guest", kind, _schema(kind, body))]      # ответ основы её боту у меня
        log.info("резерв: «%s» линии %s — не моя и не храню — пропущено", kind, line[:8] or "—")
        return []

    def _keep_serve_base(self, frm: int) -> list[tuple]:
        """`jneed` основы по её линии: подряд с `frm` — из хранилища её событий (она их
        потеряла: пропажа питания после отправки), иначе из журнала данных гостя на диске
        (гость ушёл, не досказав). Только записи эпохи, которая держит номер `frm`
        (`_keep_need_epoch`): тот же номер прежней эпохи после точки расхождения новой —
        запись, которую новая эпоха заменила, основе она чужая. Нет ни там, ни там —
        пусто (спросит гостя)."""
        from awgbot.util.rsvjrn import event
        line = self.reserve_keep_line()
        keep = self.reserve_keep_store()
        try:
            guest = self.guest_jrn_store()
            guest_starts = guest.epoch_starts()
        except Exception as e:                                # noqa: BLE001 — журнала гостя нет
            log.debug("резерв: журнал гостя на диске не прочитан: %s", e)
            guest, guest_starts = None, {}
        want = self._keep_need_epoch(frm, keep.epoch_starts(), guest_starts)
        if want is None:
            return []
        recs = self._store_run(keep, line, frm, want)
        if not recs and guest is not None:
            try:
                recs = self._store_run(guest, line, frm, want)
            except Exception as e:                            # noqa: BLE001 — журнала гостя нет
                log.debug("резерв: журнал гостя на диске не прочитан: %s", e)
                recs = []
        if not recs:
            return []
        h = event.head(recs[0])
        log.info("резерв: «%s» просит события с %s — отдаю из хранилища %d (эпоха %s)", self.reserve_peer_name(),
                 frm, len(recs), h.e)
        return [("send", "jev", {"line": line, "e": h.e, "from": h.n, "n": len(recs), "blob": b"".join(recs).hex()})]

    def _keep_need_epoch(self, frm: int, *starts: dict[int, int]) -> int | None:
        """Эпоха, которая держит номер `frm`: старшая из начавшихся не позже него. Начала —
        первые номера эпох в хранилищах и, пока гость поднят и сказал о себе, его эпоха с
        номера после его точки. Ни одна не начиналась — None."""
        first: dict[int, int] = {}
        for got in starts:
            for e, n in got.items():
                first[e] = min(first.get(e, n), n)
        ge, gf = self._keep_guest_int("e"), self._keep_guest_int("fork")
        if ge and gf:
            first[ge] = min(first.get(ge, gf + 1), gf + 1)
        return max((e for e, n in first.items() if n <= frm), default=None)

    @staticmethod
    def _store_run(store, line: str, frm: int, epoch: int | None = None) -> list[bytes]:
        """Записи линии подряд с номера `frm` (только эпохи `epoch`, если задана), одной
        эпохи, не больше EV_BYTES; первая не `frm` — пусто (разрыв не прикрывать)."""
        from awgbot.util.rsvjrn import event
        from .ship import EV_BYTES
        recs = [r for r in store.events(after=max(0, frm - 1), epoch=epoch) if event.head(r).line == line]
        if not recs or event.head(recs[0]).n != frm:
            return []
        return event.runs(recs, EV_BYTES)[0]

    def _jrn_mine_in(self, kind: str, body: dict) -> list[tuple]:
        if kind == "jev":
            return self._follow_jev_in(body)
        if kind == "jcnt":
            return self._follow_jcnt_in(body)
        if kind == "jack":
            self._jrn_ack_in(int(body["i"]))
        elif kind == "jneed":
            self._jrn_need_in(int(body["from"]))
        elif kind == "jstale":
            self._jrn_stale_in(int(body["e"]))
        elif kind == "jcack" and str(body.get("side") or "") == "a":
            self._jrn_cack_in(int(body["s"]))
        elif kind == "jtail":
            return self.reserve_tail_in(body)                 # хвост бота на резерве (С9) — держатель здесь
        elif kind == "jtail_ok":
            return self.reserve_tail_ok_in(body)              # мой хвост перебазирован ботом на резерве
        elif kind == "jc9none":
            return self.reserve_c9_none_in(body)              # журнала бота на резерве (С9) у резерва нет
        return []

    def _jrn_stale_in(self, e: int) -> None:
        """Резерв знает эпоху моей линии больше моей: держатель — мой бот у него. Принять
        её (следование), только если резерв и правда несёт моих (иначе — строка)."""
        from .ship import epoch_sane
        peer = self.reserve_peer_state()
        meta_now = self.db.jrn_meta()
        if not epoch_sane(e, int(meta_now["epoch"])):
            log.warning("резерв: «%s» называет эпоху %s моей линии — вне разумного, пропущено", self.reserve_peer_name(), e)
            return
        if str(peer.get("inn") or "") == "serving":
            fork = int((peer.get("jrn_keep") or {}).get("guest_fork") or 0)
            if fork and not int(meta_now["acked"]) <= fork <= max(self.db.jrn_last_i(), int(meta_now["acked"])) + 1:
                log.warning("резерв: точка гостя %s из st вне моих номеров — беру подтверждённое", fork)
                fork = 0
            fork = fork or int(meta_now["acked"])
            meta = self.db.jrn_adopt(int(e), why="эпоха больше моей у резерва", fork_i=fork)
            if meta is not None and (peer.get("jrn_keep") or {}).get("guest_fork"):
                self.reserve_tail_finalize(fork)
                log.warning("резерв: эпоха %s у бота на «%s» — здесь следование", meta["epoch"], self.reserve_peer_name())
        elif meta_now["role"] == "holder":
            # он моих не несёт, а эпоху линии помнит выше моей: база восстановлена из копии (jrn_meta в
            # копию не идёт) — поднять свою, иначе каждое событие получало бы jstale без конца
            meta = self.db.jrn_raise(int(e), why="эпоха линии у резерва выше — восстановление из копии")
            if meta is not None:
                log.warning("резерв: эпоха линии поднята до %s по слову «%s» (моих он не несёт)", meta["epoch"],
                            self.reserve_peer_name())
        else:
            log.warning("резерв: «%s» знает эпоху %s моей линии, но моих не несёт", self.reserve_peer_name(), e)

    # ── гость → основа (транзит у резерва) ──────────────────────────────────
    def reserve_guest_jrn_word(self, kind: str, body: dict) -> list[tuple]:
        """Запись моего гостя по локальному каналу — основе как
        есть; его `jneed` — о событиях основы, которых он не получил (следует до передачи
        бота): досылаю из своего хранилища. Линия — та, что храню."""
        keep = self.reserve_keep_line()
        if not keep or str(body.get("line") or "") != keep:
            log.info("резерв: «%s» от гостя не той линии — пропущено", kind)
            return []
        if kind == "jneed":
            return self._keep_serve_guest(int(body.get("from") or 0))
        if kind not in ("jev", "jcnt", "jtail", "jtail_ok"):
            return []
        return [("send", kind, _schema(kind, body))]

    def _keep_serve_guest(self, frm: int) -> list[tuple]:
        """События основы гостю с номера `frm` (его `jneed`): из хранилища, подряд, не больше
        EV_BYTES. С `frm` в хранилище не начинается — ничего: основа подтверждённые события у
        себя не держит, досылать их некому — гость в следовании остаётся на своей точке, а
        основа скажет о недошедших решениях после возврата (jrn_mine_lost); дыру — в журнал,
        один раз на номер. Только записи эпохи, которая держит номер `frm` (`_keep_need_epoch`,
        как у `jneed` основы): тот же номер прежней эпохи, заменённый новой с точки
        расхождения, гостю чужой — его база разошлась бы с основой молча."""
        from awgbot.util.rsvjrn import event
        line = self.reserve_keep_line()
        keep = self.reserve_keep_store()
        want = self._keep_need_epoch(frm, keep.epoch_starts())
        recs = self._store_run(keep, line, frm, want) if want is not None else []
        if not recs:
            if self.__dict__.get("_keep_hole_told") != frm:
                self.__dict__["_keep_hole_told"] = frm
                log.warning("резерв: в хранилище журнала «%s» нет записи %s — гостю досылать нечего, его база "
                            "отстанет от основы с этого номера", self.reserve_peer_name(), frm)
            return []
        h = event.head(recs[0])
        return [("guest", "jev", {"line": line, "e": h.e, "from": h.n, "n": len(recs), "blob": b"".join(recs).hex()})]
