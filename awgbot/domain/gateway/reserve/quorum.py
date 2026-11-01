"""
quorum.py — часть пакета reserve агента (см. __init__): кворум резерва на
шлюзе — раздел `quorum` документа конфигурации (файл
`/var/lib/awg-gw/quorum.json`, 0600: линия, поколение, состав, порог, конфиги
зондов к основе и к клону, точка, эпоха), интерфейсы зондов в пространстве
`awg-quo` (скрипт install/quorum-probe-setup.sh через infra/quoprobe), пробы
и взгляд голосующего (правило `util/quorum/voter`), `qview` резерву по
гостевому каналу и своему серверу по каналу конфигурации, строка панели
(Т-К17) и поле `quo` снимка.

Зонд к основе (`awgq<l>h`) стоит всегда, пока есть раздел; зонд к клону
(`awgq<l>r`) — пока пользователи основы у резерва (слово `away` основы или
`serving` резерва в reserve.json). Свои отметки хода (`_KEEP_KEYS`) новый
документ не стирает; точка прежнего поколения (`point_prev`) живёт до
следующей смены — её просит второй сервер, пока ключи нового до него не дошли.
Поколение точки — номер `q_gen` и отпечаток многочлена `qf` (rsvkeys.same_q):
после восстановления основы из копии номера идут заново; сервер прежней версии
отпечатка не шлёт — тогда по номеру.

Части класса: раздел документа, интерфейсы зондов и цифры лидера — quorum_doc;
эпоха, обязательства и отданная точка на диске — quorum_disk; свой взгляд,
панель и снимок — quorum_view; здесь — подъём зондов, такт голосующего, ответы,
взгляды и слова кандидата.
"""
from __future__ import annotations

import json
import time

from awgbot.util import livestep, timeutil
from awgbot.util.quorum import timing as qt
from awgbot.util.quorum.probe import Probe, Reply
from awgbot.util.quorum.voter import Voter

from ..base import log
# части класса и прежние имена модуля (STORE_PATH, _read, _write, _point_prev, …) — отсюда же
from .quorum_disk import EPOCH_STEP_MAX, GatewayQuorumDiskMixin
from .quorum_doc import _KEEP_KEYS, STORE_PATH, GatewayQuorumDocMixin, _point_of_gen, _point_prev, _read, _write
from .quorum_view import GatewayQuorumViewMixin
from .store import STORE_LOCK                                 # один замок на reserve.json и quorum.json

APPLY_EVERY_S = 300

__all__ = ["APPLY_EVERY_S", "EPOCH_STEP_MAX", "STORE_PATH", "GatewayQuorumMixin", "_KEEP_KEYS", "_point_of_gen",
           "_point_prev", "_read", "_write"]


class GatewayQuorumMixin(GatewayQuorumDocMixin, GatewayQuorumDiskMixin, GatewayQuorumViewMixin):
    """Кворум на шлюзе: раздел документа и зонды (quorum_doc), эпоха, обязательства и точка на
    диске (quorum_disk), взгляд, панель и снимок (quorum_view); здесь — пробы, ответы, взгляды
    голосующего и слова кандидата (их часы — `time` этого модуля)."""
    _QUO_STORE = STORE_PATH

    # ── зонды: подъём по разделу ────────────────────────────────────────────
    def quorum_probes_apply(self, force: bool = False) -> dict:
        from awgbot.infra import quoprobe
        want = self.quorum_probe_ifaces()
        digest = json.dumps(sorted((k, v["conf"]) for k, v in want.items()))
        now = time.monotonic()
        if not force and self.__dict__.get("_quo_apply_digest") == digest and \
                not livestep.mono_due(self.__dict__.get("_quo_apply_mono"), now, APPLY_EVERY_S):
            return want
        self.__dict__["_quo_apply_digest"], self.__dict__["_quo_apply_mono"] = digest, now
        for name in sorted(set(quoprobe.standing()) - set(want)):
            try:
                quoprobe.down(name)
                log.info("quorum: зонд %s снят", name)
            except quoprobe.QuoError as e:
                log.warning("quorum: зонд %s не снят: %s", name, e)
        for name, rec in want.items():
            try:
                out = quoprobe.up(name, rec["conf"], int(rec.get("port") or 0))
                if "same" not in out:
                    log.info("quorum: зонд %s поднят", name)
            except quoprobe.QuoError as e:
                log.warning("quorum: зонд %s не поднялся: %s", name, e)
        return want

    # ── правило голосующего ─────────────────────────────────────────────────
    @property
    def _gw_qvoters(self) -> dict:
        return self.__dict__.setdefault("_gw_qvoters", {})

    def quorum_tick(self, now: float | None = None) -> list[tuple]:
        """Такт голосующего (5 с): интерфейсы, пробы, взгляды → qview."""
        now = time.monotonic() if now is None else now
        if not self.quorum_present():
            self._gw_qvoters.clear()
            return []
        want = self.quorum_probes_apply()
        me = self.quorum_my_node()
        if not me:
            return []
        for name in list(self._gw_qvoters):
            if name not in want:
                self._gw_qvoters.pop(name, None)
        sec = self.quorum_section()
        timing = self.quorum_timing()
        actions: list[tuple] = []
        for name, rec in want.items():
            v = self._gw_qvoters.get(name)
            if v is None:
                v = Voter(str(rec["l"]), me, timing=timing)
                v.start(now)
                v.epoch_in(max(int(sec.get("e") or 0), int(sec.get("e_known") or 0)))   # факт (документ, ответы, qstate)
                if int(sec.get("e_take") or 0) > int(v.epoch):
                    v.raise_to(int(sec["e_take"]))         # поднятая по qtake — отдельно: qrel/state{idle} вернут к факту
                self._gw_qvoters[name] = v
                self._pledge_restore(name, v, now)
            elif v.timing != timing:
                v.timing = timing                             # лидер сменил цифры — голосующий считает ими же
            for act in v.tick(now):
                if act == "probe":
                    p = Probe(str(rec["l"]), int(sec.get("g") or 1), me, int(v.epoch), v.w_for_probe(),
                              v.probe_out(now, qt.jitter(timing.probe_s)))
                    actions.append(("probe", name, p.encode(), (str(rec["resp"]), int(rec["port"]))))
                elif act in ("see", "uns", "sus"):
                    actions += self._quorum_view_changed(name, rec, v, act)
                elif act == "unpledge":
                    self._pledge_store(name, None)
                    log.info("quorum: обязательство «нет» снято по сроку")
            due = (self.__dict__.get("_quo_ask_due") or {}).get(name)
            if due is not None and now >= due:
                self.__dict__["_quo_ask_due"].pop(name, None)
                actions += self._quorum_qview_now(name, rec, v)      # ответ на qask после свежих проб
        online = bool(self.__dict__.get("_quo_reserve_online"))
        sess = self.__dict__.get("_quo_guest_session")             # номер сессии от runtime (обрыв между тактами)
        if online and (not self.__dict__.get("_quo_online_prev") or sess != self.__dict__.get("_quo_session_prev")):
            # новая сессия гостевого канала: текущий взгляд о сервере — первым словом (у резерва взгляды в памяти)
            for name, rec in want.items():
                v = self._gw_qvoters.get(name)
                if v is not None and rec.get("target") == "h" and v.view != "new":
                    actions += self._quorum_qview_now(name, rec, v)
        self.__dict__["_quo_online_prev"] = online
        if online:
            self.__dict__["_quo_session_prev"] = sess
        return actions

    def quorum_reply_in(self, name: str, data: bytes) -> list[tuple]:
        v = self._gw_qvoters.get(name)
        if v is None:
            return []
        r = Reply.parse(data)
        if r is None:
            return []
        if int(r.e) > int(v.epoch) and not self._quorum_epoch_sane(int(r.e)):
            import dataclasses
            log.warning("quorum: ответ %s несёт эпоху %s далеко впереди известной — эпоха не принята", name, r.e)
            r = dataclasses.replace(r, e=int(v.epoch))       # ответ — жизнь лидера, эпоха — нет
        now = time.monotonic()
        actions: list[tuple] = []
        fact_before = int(v.fact)
        for act in v.reply_in(r, now):
            if act == "see":
                rec = self.quorum_probe_ifaces().get(name) or {"l": v.l, "target": name[-1:]}
                actions += self._quorum_view_changed(name, rec, v, "see")
            elif act.startswith("adopt:"):
                # эпоха из ответа (клона у резерва — E после взятия) — факт линии: всем голосующим
                # и на диск; иначе пробы к основе дома шли бы с прежней и она набрала бы «да»
                self._quorum_epoch_in(int(v.epoch))
                log.info("quorum: ответ %s несёт эпоху %s — принята", "клона" if name.endswith("r") else "сервера AWG", v.epoch)
        if int(v.fact) > fact_before:
            self._quorum_epoch_persist(int(v.fact))           # факт догнал поднятую по qtake — на диск (e_take прочь)
        return actions

    def quorum_probe_unsent(self, name: str, data: bytes) -> None:
        """Проба не ушла из сокета зонда (его ещё нет, ошибка отправки): ответа не ждать,
        время до неё — не молчание лидера (иначе qview{uns} о живом сервере после старта)."""
        v = self._gw_qvoters.get(name)
        p = Probe.parse(data)
        if v is not None and p is not None:
            v.unsent(int(p.s), time.monotonic())

    def _quorum_view_changed(self, name: str, rec: dict, v: Voter, w: str) -> list[tuple]:
        target = "клон" if rec.get("target") == "r" else "сервер AWG"
        log.info("quorum: %s — %s", target, {"see": "вижу", "sus": "ответ пропущен", "uns": "не вижу"}[w])
        with STORE_LOCK:
            cur = self.quorum_section()
            views = dict(cur.get("views") or {})
            views[name] = {"w": w, "since": timeutil.now_iso(), "target": str(rec.get("target") or "")}
            cur["views"] = views
            _write(cur, self._QUO_STORE)
        if w == "sus":
            return []
        body = {"l": str(rec.get("l") or ""), "g": int(cur.get("g") or 0), "w": w, "since": views[name]["since"], "e": int(v.epoch),
                "tgt": str(rec.get("target") or "h")}
        info = v.info()
        if info is not None and w == "see":
            body["i"] = info.to_dict()
        if w == "uns":
            v.said_no(time.monotonic())                       # «нет» кандидату (о сервере или о клоне) — обязательство под этой эпохой
            self._pledge_store(name, {"e": int(v.epoch), "at": timeutil.now_iso()})
        # взгляд о клоне — только своему серверу: резерв цель не различает, «не вижу клон» лёг бы взглядом на основу
        return ([("guest", "qview", body)] if rec.get("target") != "r" else []) + [("link", "qview", body), ("push",)]

    def quorum_guest_in(self, kind: str, body: dict) -> list[tuple]:
        """Слово кандидата (резерв по гостевому каналу или свой сервер по каналу
        конфигурации в отъезде, `tgt = r` — о своём клоне): `qstate` — эпоха линии; `qask` — опрос (взгляд сразу
        или после трёх свежих проб); `qtake{e}` — забирает под e + 1; `qrel{e}` —
        обязательство снято; `qneed` — точка ключа кворума, если сам не вижу основу и
        поколение совпало; `qpause{until}` — «не вижу» кандидату не уходит до срока."""
        sec = self.quorum_section()
        l = str(body.get("l") or "")
        if not sec.get("l") or (l and l != str(sec.get("l"))):
            return []
        now = time.monotonic()
        name = f"awgq{sec['l']}{'r' if str(body.get('tgt') or 'h') == 'r' else 'h'}"   # tgt = r — свой сервер в отъезде о клоне
        v = self._gw_qvoters.get(name)
        if kind == "qstate":
            e = int(body.get("e") or 0)
            if e:
                self._quorum_epoch_in(e)
            return []
        if v is None:
            return self._quorum_word_without_voter(name, kind, body)
        if kind == "qask":
            return self._quorum_asked(name, v, now)
        if kind == "qtake":
            e = int(body.get("e") or 0)
            if not self._quorum_epoch_sane(e + 1):
                log.warning("quorum: qtake под эпоху %s далеко впереди известной — не принят", e + 1)
                return []
            v.take(e)
            self._quorum_take_persist(int(v.epoch))
            self._pledge_store(name, None)
            log.warning("quorum: кандидат забирает основу под эпоху %s — обязательство снято", v.epoch)
            return []
        if kind == "qrel":
            before = int(v.epoch)
            if v.release(int(body.get("e") or 0)):
                self._pledge_store(name, None)
                if int(v.epoch) < before:
                    # «забираю» отменено: эпоха, поднятая по qtake без факта, — назад, на диске тоже (иначе
                    # после перезапуска пробы снова шли бы с e + 1, и прежний лидер не набрал бы «да»)
                    self._quorum_take_unpersist()
                    log.warning("quorum: «забираю» под эпоху %s отменено (qrel) — эпоха снова %s", before, v.epoch)
                else:
                    log.info("quorum: обязательство «нет» под эпохой %s снято (qrel)", body.get("e"))
            if hasattr(self, "reserve_share_released"):
                self.reserve_share_released()                 # «не забираю» до начала попытки — точка не понадобится
            return []
        if kind == "qneed":
            return self._quorum_need_in(sec, v, {"q_gen": int(body.get("q_gen") or 0), "qf": body.get("qf")},
                                        str(body.get("since") or ""))
        if kind == "qpause":
            from awgbot.util.quorum.voter import pause_seconds
            v.pause(now, pause_seconds(body))                 # dur — длительность, until — по своим часам
            log.info("quorum: сервер AWG просит паузу до %s", body.get("until"))
            return []
        return []

    def _quorum_asked(self, name: str, v: Voter, now: float) -> list[tuple]:
        """`qask`: взгляд uns/sus — qview сразу; see и ответ свежее fresh_s — qview{see, i}
        сразу; see со старым ответом — три пробы за ≤ 15 с, qview после них."""
        rec = self.quorum_probe_ifaces().get(name) or {"l": v.l, "target": "h"}
        if v.view in ("uns", "sus") or (v.view == "see" and v.last_reply and now - v.last_reply <= v.timing.fresh_s):
            return self._quorum_qview_now(name, rec, v)
        v.next_probe = now
        v.resends = 0
        self.__dict__.setdefault("_quo_ask_due", {})[name] = now + 15.0
        log.info("quorum: опрос кандидата — три свежие пробы основы")
        return []

    def _quorum_qview_now(self, name: str, rec: dict, v: Voter) -> list[tuple]:
        w = v.view if v.view != "sus" else "uns"
        cur = self.quorum_section()
        body = {"l": str(rec.get("l") or ""), "g": int(cur.get("g") or 0), "w": w,
                "since": str((cur.get("views") or {}).get(name, {}).get("since") or timeutil.now_iso()), "e": int(v.epoch),
                "tgt": str(rec.get("target") or "h")}
        info = v.info()
        if info is not None and w == "see":
            body["i"] = info.to_dict()
        if w == "uns":
            v.said_no(time.monotonic())
            self._pledge_store(name, {"e": int(v.epoch), "at": timeutil.now_iso()})
        return ([("guest", "qview", body)] if rec.get("target") != "r" else []) + [("link", "qview", body)]
