"""
route.py — часть пакета journal (см. __init__): разбор сообщений журнала по
линии данных.

Каждый сервер пары — основа своей линии и резерв линии второй стороны, и
сообщения журнала обеих линий идут одним каналом. Кто я для сообщения,
решает его линия:

| сообщение | моя линия (я основа) | линия второй стороны (я резерв) |
|---|---|---|
| `jev` | события бота основы на резерве → применить (следование) | события основы → хранить |
| `jcnt` | пачка клона (b) → сложить | пачка основы (a) → хранить |
| `jack`, `jneed`, `jstale`, `jcack` | ответ резерва на мои записи | ответ основы на записи её бота у меня → ему |

Записи бота основы на резерве (гостя) приходят от него по локальному каналу
и уходят основе как есть — бот резерва видит только заголовки.
"""
from __future__ import annotations

from ...gateway_link.common import log

_ACKS = ("jack", "jneed", "jstale", "jcack", "jtail", "jtail_ok")


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
            return [("guest", kind, _schema(kind, body))]      # ответ основы её боту у меня
        log.info("резерв: «%s» линии %s — не моя и не храню — пропущено", kind, line[:8] or "—")
        return []

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
            fork = int((peer.get("jrn_keep") or {}).get("guest_fork") or 0) or int(meta_now["acked"])
            meta = self.db.jrn_adopt(int(e), why="эпоха больше моей у резерва", fork_i=fork)
            if meta is not None and (peer.get("jrn_keep") or {}).get("guest_fork"):
                self.reserve_tail_finalize(fork)
                log.warning("резерв: эпоха %s у бота на «%s» — здесь следование", meta["epoch"], self.reserve_peer_name())
        else:
            log.warning("резерв: «%s» знает эпоху %s моей линии, но моих не несёт", self.reserve_peer_name(), e)

    # ── гость → основа (транзит у резерва) ──────────────────────────────────
    def reserve_guest_jrn_word(self, kind: str, body: dict) -> list[tuple]:
        """Запись моего гостя (бота основы на резерве) по локальному каналу — основе как
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
        from awgbot.util.rsvjrn import event
        from .ship import EV_BYTES
        line = self.reserve_keep_line()
        recs, size = [], 0
        for r in self.reserve_keep_store().events(after=max(0, frm - 1)):
            if recs and (size + len(r) > EV_BYTES or event.head(r).e != event.head(recs[0]).e):
                break
            recs.append(r)
            size += len(r)
        if not recs:
            return []
        h = event.head(recs[0])
        return [("guest", "jev", {"line": line, "e": h.e, "from": h.n, "n": len(recs), "blob": b"".join(recs).hex()})]
