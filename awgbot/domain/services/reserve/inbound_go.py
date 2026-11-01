"""
inbound_go.py — часть пакета reserve (см. __init__): «пользователи основы у
меня» — переезд кнопкой со стороны резерва (вынесено из inbound): `go_req`
основы — предусловия (`reserve_open_block` без ящика кода), зона, процесс
открытия в режиме ожидания конверта; `go{sealed, oid}` — конверт с `K`
второй строкой процессу, который ждёт его под тем же `oid` (шаг `eph`),
повтор после передачи — молча; `go{k}` открытым — только от основы без
`seal1`. Отказ конверту, пока процесс его ждёт, — срыв приёма целиком
(`_take_fail`). Автомат приёма и такт — в inbound.
"""
from __future__ import annotations

import time

from awgbot.util import timeutil

from ..types import ServiceError
from ..gateway_link.common import log
from .inbound import OPEN_WAIT_S
from .inbound_keep import KEEP_RESET
from .state import _LOCK


class ReserveInboundGoMixin:
    # ── процесс открытия: go_req / go основы ────────────────────────────────
    def reserve_go_req_in(self, gen: int, kind: str, ttl: int = 0) -> str:
        """`go_req` от основы (переезд кнопкой): запустить процесс открытия в
        режиме ожидания конверта с ключом пакета. Пусто — запущен; иначе
        причина отказа (уйдёт в `down`)."""
        return self._go_launch(gen, kind, ttl, k_hex="")

    def reserve_go_in(self, k_hex: str, gen: int, kind: str, ttl: int = 0, sealed: str = "", oid: str = "") -> str:
        """`go` от основы. С конвертом (`sealed`, `oid`) — ключ пакета процессу
        открытия, который его ждёт (шаг `eph` с тем же `oid`); повтор с тем же
        `oid` после передачи — молча. `k` открытым — только от основы прежнего
        выпуска (без `seal1`): процесс запускается с ключом во входе. Пусто —
        принято; иначе причина отказа (уйдёт в `down`)."""
        if sealed:
            return self._go_sealed(gen, str(sealed), str(oid))
        if "seal1" in (self.reserve_peer_state().get("caps") or []):
            return "ключ пакета не зашифрован"
        try:
            bytes.fromhex(str(k_hex))
        except ValueError:
            return "ключ не hex"
        return self._go_launch(gen, kind, ttl, k_hex=str(k_hex))

    def reserve_go_refused(self, why: str, sealed: bool) -> list[tuple]:
        """Отказ на `go`: `down` основе. Отказ конверту, пока процесс открытия
        его ждёт, — срыв приёма целиком (`_take_fail`: юнит стоп, зона снята,
        `down`): конверта к этому открытию уже не будет, а ждать его 2 минуты —
        держать зону занятой для нового переезда."""
        inn = self._inn()
        if sealed and str(inn.get("state") or "") == "taking" and inn.get("sealed") and not inn.get("k_passed"):
            return self._take_fail(why)
        return [("send", "down", {"reason": why[:120]})]

    def _go_sealed(self, gen: int, sealed: str, oid: str) -> str:
        from awgbot.infra import rsvzone
        try:
            bytes.fromhex(sealed)
        except ValueError:
            return "ключ пакета не hex"
        with _LOCK:
            inn = self._inn()
            if str(inn.get("state") or "") != "taking" or not inn.get("sealed"):
                return "процесс открытия не ждёт ключ"
            if int(inn.get("gen") or 0) != int(gen):
                older = int(inn.get("gen") or 0) < int(gen)
                return "открывается пакет " + ("прошлого" if older else "более нового") + " поколения"
            if inn.get("k_passed"):
                if str(inn.get("oid") or "") == oid:
                    log.info("резерв: ключ пакета уже передан процессу — повтор `go` пропущен")
                    return ""
                return "ключ не к этому процессу открытия"
            st = rsvzone.open_status()
            if str(st.get("step") or "") != "eph" or str(st.get("oid") or "") != oid:
                return "ключ не к этому процессу открытия"
            why = self._rsvopen_send({"sealed": sealed})
            if why:
                return why
            self.rsv_update("rsv_in", k_passed=True, oid=oid, k_passed_at=timeutil.now_iso())
        log.info("резерв: конверт с ключом пакета передан процессу открытия (пакет %s)", gen)
        return ""

    def _go_launch(self, gen: int, kind: str, ttl: int, k_hex: str) -> str:
        """Общее для `go_req` и `go{k}`: предусловия, зона, запуск процесса."""
        pkg_in = self.rsv_get("rsv_pkg_in")
        if int(pkg_in.get("gen") or 0) != int(gen):
            return f"у меня пакет {pkg_in.get('gen') or 'нет'}, а не {gen}"
        why = self.reserve_open_block(need_box=False)         # по K основы ящик кода не нужен
        if why:
            return why
        if hasattr(self, "reserve_zone_shell_ensure"):
            self.reserve_zone_shell_ensure()                  # после перезагрузки хоста зоны нет — --take отказал бы
        payload = self._open_payload(kind)
        if k_hex:
            payload["k"] = k_hex
        else:
            payload["await_k"] = True                         # конверт с K — второй строкой, когда процесс даст разовый ключ
        payload["dns"] = {"ttl": int(ttl or 0)}               # по K конверта DNS нет: TTL записи — от основы
        with _LOCK:
            if not self.reserve_zone_free():                  # проверка и запись — под одним замком
                return "зона занята"
            self._relay_yield()                               # хвост уступает новому клону
            from awgbot.infra import rsvzone
            rsvzone.open_status_write("launching")           # прежний open.status не должен сойти за ход этого открытия
            self._rsvopen_forget()                            # и код прошлого процесса — за его итог (запуск — после замка)
            self.reserve_set_in("taking", kind=str(kind or "button"), gen=int(gen), why=str(kind or "button"), by="k",
                                open_started=timeutil.now_iso(), deadline=time.monotonic() + OPEN_WAIT_S,
                                up_sent=False, tg_taken=False, tg_take_forced=False, tg_wait="", **KEEP_RESET,
                                webhook_foreign=False, webhook_stalled=False, peer_tg_taken="", jrn_released=None, pult_back="",
                                pult_told="",   # о прошлом эпизоде, не об этом
                                sealed=not k_hex, k_passed=False, oid="", eph_sent=0,
                                ended_auto=str(kind or "button") != "button")   # по просьбе кворума (dead | rf) — автоматический
            self._in_step("open")
        try:
            self._rsvopen_launch(payload, keep_stdin=not k_hex)
        except ServiceError as e:
            self.reserve_set_in("idle", step="", reason=str(e))
            self._relay_restore()                             # пересылка хвоста, уступившая открытию, — обратно
            return str(e)
        log.info("резерв: процесс открытия запущен (%s, пакет %s%s)", kind, gen, ", ключ — конвертом" if not k_hex else "")
        return ""
