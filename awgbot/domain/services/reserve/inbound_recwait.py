"""
inbound_recwait.py — часть пакета reserve (см. __init__): «пользователи основы у
меня» — исход смены записи DNS процессом открытия неизвестен.

Аварийный путь (по точкам кворума или по коду): процесс открытия перед запросом
к провайдеру пишет шаг-намерение `record_try{name, ip, limit}`. Запрос кончился
исключением (таймаут, сетевой сбой, 5xx, ответ не разобран) — это не «запись не
сменена»: провайдер мог принять правку. Явный отказ провайдера (`dnsapi.Refused`:
4xx с разобранным телом, «KO», badauth…, запись не найдена) и «вручную» — исход
известен: процесс отказывает сразу итогом без поля `record`, и такт это знает
(`_rec_observe`: итог после намерения без поля — запись не сменена, прежний срыв). Процесс сам читает авторитетный ответ:
адрес мой — `record_set`, как обычно; нет — шаг `record_wait{name, ip, limit,
why}`, клоны стоят, чтение раз в минуту ± 40 % до `limit` (dnsapi `apply_wait_s`:
5 минут у API-провайдеров, «вручную» — 30) от `record_try`; не дождался —
прежний срыв со снятием зоны («DNS-запись не подтвердилась за N мин: …»).

Процесс погиб или отказал посреди неизвестного (такт видит `record_try` /
`record_wait` без итога, процесс кончился, итог `fail` с полем `record=try`, срок
35 минут, или итога нет дольше предела и REC_GRACE_S) — `_take_fail` не снимает
зону, ожидание ведёт бот резерва той же логикой (`_take_rec_wait`): первое
чтение сразу, дальше раз в KEEP_RETRY_S ± 40 % до `rec_until` (предел от
`record_try`), последнее — на самом пределе. Подтвердилась — как после
`record_fact` процесса: запись моя (`rec_moved`, дальше любой срыв зону не
снимает — inbound_keep), отметка `tg_take` (гость ставит вебхук), открытие
кончено (`rec_done`: такт идёт как по `done` — serve и ожидание `tg_taken`); не
подтвердилась за предел — прежний срыв (`_take_fail_do`: зона снята, `down`
основе, шлюзам `state{idle}`, админу — «⚠️ Пользователей … принять не удалось:
DNS-запись не подтвердилась за N мин: …»).

Админу резерва — одно уведомление на вход в ожидание (шаг `record_wait` процесса
или ожидание бота; подтверждение первым же чтением — без него) и одно на исход
(«подтвердилась» или срыв). Путь с K основы запись не меняет — правило не
действует.
"""
from __future__ import annotations

import time

from awgbot.core import config
from awgbot.util import backoff, livestep

from ..types import Notification
from ..gateway_link.common import log
from .inbound_keep import KEEP_RETRY_S

REC_GRACE_S = 60                  # итога процесса нет дольше предела и этого — ожидание ведёт бот
REC_STEPS = ("record_try", "record_wait")


def _limit_of(v) -> int:
    from awgbot.util import dnsapi
    try:
        n = int(float(v or 0))
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else dnsapi.APPLY_DEFAULT_S


class ReserveInboundRecWaitMixin:

    def _rec_try_observe(self, inn: dict, st: dict) -> dict:
        """Шаг `record_try` / `record_wait` или итог `fail{record=try}`: исход смены записи
        неизвестен — имя, адрес, предел и срок (от первого взгляда на намерение)."""
        if inn.get("rec_moved"):
            return inn                                        # запись уже моя — перезапущенному процессу не верю на слово
        upd: dict = {}
        if not inn.get("rec_try"):
            limit = _limit_of(st.get("limit"))
            upd.update(rec_try=True, rec_limit=limit, rec_until=time.monotonic() + limit)
            log.info("резерв: процесс открытия меняет запись DNS %s → %s — исход до факта неизвестен (предел %d с)",
                     st.get("name") or "?", st.get("ip") or "?", limit)
        for k, v in (("rec_name", str(st.get("name") or "")), ("rec_ip", str(st.get("ip") or ""))):
            if v and v != inn.get(k):
                upd[k] = v
        why = str(st.get("why") or "")[:200]
        if why and why != inn.get("rec_why"):
            upd["rec_why"] = why
        return self.rsv_update("rsv_in", **upd) if upd else inn

    def _rec_unknown(self, inn: dict) -> bool:
        """Процесс начал смену записи, а её исход не известен (ни `record_set`, ни итога)."""
        return bool(inn.get("rec_try")) and not inn.get("rec_moved") and not self._in_by_k(inn)

    def _rec_wait_notes(self, inn: dict, step: str = "") -> list[tuple]:
        """Админу резерва: вход в ожидание факта — один раз; «подтвердилась» — один раз после входа."""
        from awgbot.domain.notices import reserve_keep as rk
        told = str(inn.get("rec_told") or "")
        name, ip = str(inn.get("rec_name") or ""), str(inn.get("rec_ip") or "")
        if not told and not inn.get("rec_moved") and (step == "record_wait" or inn.get("rec_wait")):
            self.rsv_update("rsv_in", rec_told="wait")
            text = rk.record_unknown(self.reserve_peer_name(), name, ip, max(1, int(inn.get("rec_limit") or 0) // 60),
                                     str(inn.get("rec_why") or ""))
        elif told == "wait" and inn.get("rec_moved"):
            self.rsv_update("rsv_in", rec_told="ok")
            text = rk.record_confirmed(self.reserve_peer_name(), name, ip)
        else:
            return []
        return [("notify", [Notification(tg_id=int(config.ADMIN_ID), text=text)])]

    # ── процесса нет: ожидание факта веду я ─────────────────────────────────
    def _take_rec_wait(self, reason: str) -> list[tuple]:
        """Вместо срыва (под отметкой `_take_fail`): процесс открытия погиб или отказал посреди
        неизвестного исхода смены записи — зона и клоны стоят, факт читаю сам, первый раз — сразу."""
        try:
            self._rsvopen_stop()                              # завис (срок) — дальше жду я
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: юнит процесса открытия не остановлен: %s", e)
        inn = self._inn()
        inn = self.rsv_update("rsv_in", rec_wait=True, rec_poll_mono=0,
                              rec_why=str(inn.get("rec_why") or reason)[:200])
        log.warning("резерв: процесс открытия не подвёл итог смены записи %s → %s (%s) — исход неизвестен, факт у "
                    "авторитетного сервера проверяю сам до %d с от намерения, зону не снимаю",
                    inn.get("rec_name") or "?", inn.get("rec_ip") or "?", reason, int(inn.get("rec_limit") or 0))
        return self._tick_rec_unknown(inn, first=True)

    def _tick_rec_unknown(self, inn: dict, first: bool = False) -> list[tuple]:
        """Такт ожидания факта (taking, `rec_wait`): чтение раз в KEEP_RETRY_S ± 40 %, последнее — на
        пределе; `first` — изнутри `_take_fail` (срыв по пределу — сразу `_take_fail_do`)."""
        now = time.monotonic()
        expired = livestep.mono_expired(inn.get("rec_until"), now) or not inn.get("rec_until")
        if not (first or expired or livestep.mono_due(inn.get("rec_poll_mono"), now, backoff.jitter(KEEP_RETRY_S))):
            return []
        self.rsv_update("rsv_in", rec_poll_mono=now)
        name, ip = str(inn.get("rec_name") or ""), str(inn.get("rec_ip") or "")
        if self._rec_auth(name, ip):
            return self._rec_confirm()
        if not expired:
            return self._rec_wait_notes(self._inn())
        why = str(inn.get("rec_why") or "")
        reason = f"DNS-запись не подтвердилась за {max(1, int(inn.get('rec_limit') or 0) // 60)} мин" + (f": {why}" if why else "")
        log.warning("резерв: факт записи %s → %s за предел не наступил — прежний срыв", name or "?", ip or "?")
        if first:
            return self._take_fail_do(reason)
        return self._take_fail(reason, keep=False)

    @staticmethod
    def _rec_auth(name: str, ip: str) -> bool:
        from awgbot.util import dnsapi
        if not name or not ip:
            log.warning("резерв: имя или адрес записи из намерения процесса открытия не известны — факт не проверить")
            return False
        try:
            return ip in dnsapi.read_authoritative(name)
        except dnsapi.DnsError as e:
            log.info("резерв: факт записи %s не прочитан: %s", name, e)
            return False

    def _rec_confirm(self) -> list[tuple]:
        """Факт есть, а процесса нет: его оставшиеся шаги — мои. Запись моя (`rec_moved`: дальше
        любой срыв зону не снимает), отметка `tg_take` — гость ставит вебхук (не легла — её вернёт
        повтор захвата inbound_tgwait после срока TAKEN_WAIT_S), открытие кончено (`rec_done`)."""
        from awgbot.infra import rsvzone
        try:
            rsvzone.write_private(rsvzone.TG_TAKE_FILE, "1")
        except OSError as e:
            log.warning("резерв: отметка tg_take для бота основы не записана: %s", e)
        inn = self.rsv_update("rsv_in", rec_moved=True, rec_fact=True, rec_try=False, rec_wait=False, rec_done=True)
        log.info("резерв: факт записи %s → %s есть у авторитетного сервера — открытие доделываю сам, гость ставит вебхук",
                 inn.get("rec_name") or "?", inn.get("rec_ip") or "?")
        return self._rec_wait_notes(inn)
