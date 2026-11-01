"""
quorum_disk.py — часть пакета reserve агента (см. __init__): кворум резерва на
шлюзе — что голосующий держит на диске (quorum.json) между перезапусками
агента (вынесено из quorum): известная эпоха линии (`e_known` — факт) и
поднятая по `qtake` (`e_take` — отдельно, её снимают `qrel` и `state{idle}`),
скачок эпохи далеко вперёд не принимается (EPOCH_STEP_MAX); обязательства
«нет» (`pledges`) со сроком по отметке; отданная кандидату точка ключа
кворума (`qneed` → `qshare`, отметка `point_given` и `share_given_at` в
reserve.json — Т-РА5); слова кандидата до создания голосующего.
"""
from __future__ import annotations

from awgbot.util import rsvkeys, timeutil
from awgbot.util.quorum.voter import Voter

from ..base import log
from .quorum_doc import _point_of_gen, _write
from .store import STORE_LOCK                                 # один замок на reserve.json и quorum.json

EPOCH_STEP_MAX = 1000          # эпоха из слова или ответа — не дальше известной на столько (мусор, поломка)


class GatewayQuorumDiskMixin:
    # ── эпоха линии ─────────────────────────────────────────────────────────
    def _quorum_epoch_known(self) -> int:
        sec = self.quorum_section()
        return max([int(sec.get("e") or 0), int(sec.get("e_known") or 0)] + [int(v.epoch) for v in self._gw_qvoters.values()])

    def _quorum_epoch_sane(self, e: int) -> bool:
        """Эпоха годится: известной нет (первый документ) или она недалеко впереди — скачок на тысячи
        принятым завёл бы линию под эпоху, которой никто не держит (пробы несут её, лидер следует)."""
        known = self._quorum_epoch_known()
        return 0 <= int(e) < 2 ** 62 and (known <= 0 or int(e) <= known + EPOCH_STEP_MAX)

    def _quorum_epoch_in(self, e: int) -> None:
        if not self._quorum_epoch_sane(int(e)):
            log.warning("quorum: эпоха %s далеко впереди известной (%s) — не принята", e, self._quorum_epoch_known())
            return
        for v in self._gw_qvoters.values():
            v.epoch_in(int(e))
        self._quorum_epoch_persist(int(e))

    def _quorum_epoch_settle(self) -> None:
        """Резерв сказал `state{idle}`: пользователей основы у него нет — эпоха, поднятая у
        голосующих по `qtake` без факта, назад к факту (сорванное «забираю» иначе держало бы
        шлюзы под e + 1 бессрочно, а живая основа принимала бы эту эпоху по первой пробе)."""
        top = 0
        for v in self._gw_qvoters.values():
            if v.rollback():
                log.warning("quorum: пользователей основы у резерва нет — эпоха снова %s (поднятую по qtake снял)", v.epoch)
            top = max(top, int(v.epoch))
        if self._gw_qvoters:
            self._quorum_take_unpersist()

    def _quorum_take_persist(self, e: int) -> None:
        """Эпоха, поднятая по `qtake` (e + 1), — на диск отдельно от факта (`e_take`): после
        перезапуска агента голосующий снова под ней, но `qrel{e}` и `state{idle}` её снимают;
        лежала бы она в `e_known` — стала бы фактом, и откат после перезапуска был бы невозможен."""
        with STORE_LOCK:
            sec = self.quorum_section()
            if sec.get("l") and int(sec.get("e_take") or 0) < int(e):
                sec["e_take"] = int(e)
                _write(sec, self._QUO_STORE)

    def _quorum_take_unpersist(self) -> None:
        """«Забираю» отменено (qrel, state{idle}) — поднятая по qtake эпоха с диска прочь."""
        with STORE_LOCK:
            sec = self.quorum_section()
            if sec.get("l") and sec.get("e_take"):
                sec["e_take"] = 0
                _write(sec, self._QUO_STORE)

    def _quorum_epoch_persist(self, e: int) -> None:
        """Известная эпоха — факт (ответ ответчика, `qstate`, `state{e}`, документ) — на диск
        (`e_known`): после перезапуска агента пробы идут с ней, а не с эпохой документа. Факт не
        ниже поднятой по qtake — поднятая больше не нужна."""
        with STORE_LOCK:
            sec = self.quorum_section()
            if not sec.get("l"):
                return
            changed = False
            if int(sec.get("e_known") or 0) < int(e):
                sec["e_known"] = int(e)
                changed = True
            if sec.get("e_take") and int(sec["e_take"]) <= int(e):
                sec["e_take"] = 0
                changed = True
            if changed:
                _write(sec, self._QUO_STORE)

    # ── слова кандидата без голосующего, точка ключа, обязательства ─────────
    def _quorum_word_without_voter(self, name: str, kind: str, body: dict) -> list[tuple]:
        """Слово кандидата до создания голосующего (первый такт после перезапуска агента):
        `qtake{e}` — эпоха e + 1 на диск (e_take, отдельно от факта), обязательство на диске снято;
        `qrel{e}` — обязательство под той эпохой снято, поднятая эпоха прочь. Голосующий при
        создании берёт всё с диска."""
        e = int(body.get("e") or 0)
        if kind == "qtake" and self._quorum_epoch_sane(e + 1):
            self._quorum_take_persist(e + 1)
            self._pledge_store(name, None)
            log.warning("quorum: кандидат забирает основу под эпоху %s (голосующего ещё нет) — записано, обязательство снято", e + 1)
        elif kind == "qrel":
            sec = self.quorum_section()
            pledge = (sec.get("pledges") or {}).get(name)
            if isinstance(pledge, dict) and int(pledge.get("e") or -1) == e:
                self._pledge_store(name, None)
                log.info("quorum: обязательство «нет» под эпохой %s снято (qrel, голосующего ещё нет)", e)
            if int(sec.get("e_take") or 0) == e + 1:
                self._quorum_take_unpersist()                 # «забираю» под e отменено — поднятая по qtake эпоха прочь
                log.warning("quorum: «забираю» под эпоху %s отменено (qrel, голосующего ещё нет)", e + 1)
            if hasattr(self, "reserve_share_released"):
                self.reserve_share_released()
        return []

    def _quorum_need_in(self, sec: dict, v: Voter, want: dict, since: str) -> list[tuple]:
        point = _point_of_gen(sec, want)
        q_gen = int(want.get("q_gen") or 0)
        if v.view != "uns":
            log.info("quorum: кандидат просит точку (с %s), а сервер AWG виден — отказ", since)
            return []
        if not point.get("y") or not rsvkeys.same_q(point, want):
            log.warning("quorum: кандидат просит точку поколения %s, у меня %s (или многочлен другой — отпечаток) — отказ",
                        q_gen, point.get("q_gen"))
            return []
        qf = rsvkeys.m_print_of(point.get("qf"))
        log.warning("quorum: отдаю точку ключа кворума кандидату (сервер AWG не отвечает с %s)", since)
        from .store import _write as _write_rsv
        with STORE_LOCK:
            rs = self.reserve_section()                       # отметку читает слово state{idle, reason} — Т-РА5
            rs["share_given_at"], rs["share_move"] = timeutil.now_iso(), None   # попытку свяжет первый serving
            _write_rsv(rs, self._RSV_STORE)
            cur = self.quorum_section()                       # факт для основы — снимком (`quo.pg`): точка какого
            cur["point_given"] = {"q": int(q_gen), "at": timeutil.now_iso(),   # многочлена ушла; qrel и новый
                                  **({"qf": qf} if qf else {})}          # документ не стирают
            _write(cur, self._QUO_STORE)
        return [("guest", "qshare", {"l": str(sec.get("l") or ""), "q_gen": int(q_gen), "x": int(point.get("x") or 0),
                                     "y": str(point.get("y")), **({"qf": qf} if qf else {})})]

    def _pledge_store(self, name: str, pledge: dict | None) -> None:
        with STORE_LOCK:
            cur = self.quorum_section()
            pledges = dict(cur.get("pledges") or {})
            if pledge is None:
                pledges.pop(name, None)
            else:
                pledges[name] = pledge
            cur["pledges"] = pledges
            _write(cur, self._QUO_STORE)

    def _pledge_restore(self, name: str, v: Voter, now: float) -> None:
        """Обязательство «нет» переживает перезапуск агента (срок — по отметке)."""
        pledge = (self.quorum_section().get("pledges") or {}).get(name)
        if not isinstance(pledge, dict):
            return
        try:
            age = timeutil.elapsed(timeutil.parse_iso(str(pledge.get("at") or "")))
        except (ValueError, TypeError):
            age = v.timing.pledge_s + 1
        # стенные часы после перезагрузки могут стоять позади отметки (часы без батарейки до NTP):
        # отрицательный возраст продлил бы обязательство без предела — не дольше pledge_s от старта
        age = max(0.0, float(age))
        if age < v.timing.pledge_s:
            v.pledge = {"e": int(pledge.get("e") or 0), "at": now - age}
            if int(pledge.get("e") or 0) > v.epoch:
                v.epoch = int(pledge.get("e") or 0)
        else:
            self._pledge_store(name, None)
