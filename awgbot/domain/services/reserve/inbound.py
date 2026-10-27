"""
inbound.py — часть пакета reserve (см. __init__): «пользователи основы у меня» —
сторона резерва: процесс открытия, зона, обслуживание, возврат (handback).

Автомат в `rsv_in` (`state`: idle | taking | serving | releasing | relay).
Такт (`reserve_in_tick`, 3 с) читает ход процесса открытия
(`/run/awg-bot/rsv/open.status`), состояние зоны и локального канала с
ботом основы на резерве и отдаёт действия runtime: `("send", вид, тело)`,
`("guest", команда, тело)` — боту основы по локальному каналу, `("notify",
[...])`, `("fw",)` — перерисовать файервол хоста.

Переезд кнопкой (`go_req{gen, kind}`; предусловия — `reserve_open_block`
без доли: зона свободна, мои пользователи здесь, готовность, направление, пакет):
пересылка хвоста прошлого возврата уступает (`_relay_yield`),
`open.status = launching`, хранилище гостя монтирует бот (у хоста), процесс
открытия временным юнитом с песочницей (openproc, `rsvopen_sandbox_props`;
stdin — JSON входа первой строкой, труба открыта; запись DNS меняет основа;
env резерва закрыт, каталог данных подменён пустым — виден только guest.pkg,
`AWG_BOT_ENV=/dev/null`) → процесс выпускает разовую пару X25519, шаг `eph`
→ `open_eph{gen, eph, oid}` основе (повтор раз в 10 с) → `go{sealed, oid}`
— конверт с `K` второй строкой в stdin, труба закрыта: бот резерва `K` не
видит ни в канале, ни в памяти (`k` открытым в `go` принимается только от
основы без `seal1`) → `clone_up` →
`up` основе (повтор раз в 30 с, пока идёт подъём) → `released{реплика}` →
гость стоп, `rep.bin`, гость старт (он подхватит реплику; не вышло — срыв),
отметка `tg_take` → `tg_taken` от гостя → `taken` основе, serving, Т-РУ4.
Такт проверяет код завершения процесса: кончился без `done`/`fail` — срыв
сразу, хвост stderr (rsvopen.err) — в причину. После `done` у taking есть
срок: аварийный путь ждёт `tg_taken` TAKEN_WAIT_S, кнопка — `released`
DONE_BUTTON_WAIT_S; основа с `released` опоздала и не на связи — `tg_take`
гостю сам (`tg_take_forced`), ещё TAKEN_WAIT_S, иначе срыв. Отказ процесса —
один раз под замком: `down{причина}`, юнит открытия стоп, зона снята, шлюзам
— `state{idle, reason}` (шлюз, отдавший долю, говорит своему админу «переезд
не состоялся», Т-РА5), отметка `fail_at` (демпфер матрицы, vote
FAIL_DAMPER_S), не `ended_auto`. Зона снята мимо бота (фаза зоны idle
при моём serving) — idle без отметки автоматического переезда: паузы
`min_interval_minutes` нет; пакет и доля остаются — матрица вправе открыть
снова сразу (процесс возьмёт `rep.bin`, если он новее пакета). Пока taking,
serving или releasing — новый пакет не принимается (package: «зона занята»).

Возврат: `back_req` → гостю `stop` (открытие ещё шло — юнит стоп) →
`replica` (гость молчит 90 с — `rep.bin`) → `released` основе (releasing;
повторный `back_req` — та же реплика из `rep.bin` снова; зоны нет — `rep.bin`
с диска или `released` без реплики) → `back` (повтор в relay — снова `relay`
и `wiped`; зоны нет — сразу `wiped`) → пересылка хвоста (`--relay`,
файервол с DNAT, затем `relay_flush` — сброс conntrack; адрес основы — IP, имя резолвится сейчас; срыв открытия до смены
записи — без хвоста, сразу idle), учёт, `relay{until}`, `--wipe`, прежний
пакет и доля — прочь, `wiped`, Т-РУ5 → relay до `relay_max_days` или тишины `relay_idle_hours` (по счётчику правила
DNAT — nftguard.relay_packets, отметка `relay_last_traffic`) →
idle. `evict` — просьба вернуть пользователей по кнопке у резерва.
"""
from __future__ import annotations

import subprocess
import time

from awgbot.core import config, settings
from awgbot.util import livestep, timeutil

from ..types import Notification, ServiceError
from ..gateway_link.common import log
from .state import _LOCK

OPEN_WAIT_S = 35 * 60            # процесс открытия: факт записи ≤ 30 мин
EPH_RESEND_S = 10                # `open_eph` основе — повтор, пока процесс ждёт конверт
TAKEN_WAIT_S = 10 * 60          # после done: гость должен поднять бота и поставить вебхук
DONE_BUTTON_WAIT_S = 1800 + 300 + 120   # кнопка: основе на запись (30 мин) и передачу бота (5 мин) с запасом
REPLICA_WAIT_S = 90              # реплика от гостя после stop

class ReserveInboundMixin:
    def _inn(self) -> dict:
        return self.rsv_get("rsv_in")

    def _in_step(self, step: str, **fields) -> dict:
        return self.rsv_update("rsv_in", step=step, step_at=timeutil.now_iso(), **fields)

    # ── процесс открытия ────────────────────────────────────────────────────
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
        why = self.reserve_open_block(need_share=False)       # по K основы доля не нужна
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
            self.reserve_set_in("taking", kind=str(kind or "button"), gen=int(gen), why=str(kind or "button"), by="k",
                                open_started=timeutil.now_iso(), deadline=time.monotonic() + OPEN_WAIT_S,
                                up_sent=False, tg_taken=False, sealed=not k_hex, k_passed=False, oid="", eph_sent=0)
            self._in_step("open")
        try:
            self._rsvopen_launch(payload, keep_stdin=not k_hex)
        except ServiceError as e:
            self.reserve_set_in("idle", step="", reason=str(e))
            return str(e)
        log.info("резерв: процесс открытия запущен (%s, пакет %s%s)", kind, gen, ", ключ — конвертом" if not k_hex else "")
        return ""

    def _relay_yield(self) -> None:
        """Фаза relay (пересылка хвоста) при новом открытии: правило DNAT снимается —
        иначе оно увело бы пользователей нового клона на основу; состояние — idle."""
        from awgbot.infra import rsvzone
        if self.reserve_in() != "relay":
            return
        try:
            rsvzone.teardown()
        except rsvzone.ZoneError as e:
            log.info("резерв: пересылка хвоста при новом открытии снята с ошибкой: %s", e)
        self.reserve_set_in("idle", step="", relay_to="", relay_until="")
        log.info("резерв: пересылка хвоста снята — зона нужна новому переезду")

    def reserve_in_tick(self) -> list[tuple]:
        """Такт автомата «пользователи основы у меня»."""
        if not self.reserve_paired():
            return []
        inn = self._inn()
        state = str(inn.get("state") or "idle")
        if state == "taking":
            return self._tick_taking(inn)
        if state == "serving":
            return self._tick_serving(inn)
        if state == "releasing":
            return self._tick_releasing(inn)
        if state == "relay":
            return self._tick_relay(inn)
        return []

    def _tick_taking(self, inn: dict) -> list[tuple]:
        from awgbot.infra import rsvzone
        st = rsvzone.open_status()
        step = str(st.get("step") or "")
        actions: list[tuple] = []
        if step == "fail":
            return self._take_fail(str(st.get("reason") or "процесс открытия отказал"))
        if step != "done":
            rc, tail = self._rsvopen_exit()                   # юнит не стартовал (226) или процесс убит (OOM): сразу, не через 35 мин
            if rc is not None:
                return self._take_fail(f"процесс открытия завершился без итога (код {rc})" + (f": {tail}" if tail else ""))
        now = time.monotonic()
        if step == "eph" and inn.get("sealed") and not inn.get("k_passed") and livestep.mono_due(inn.get("eph_sent"), now, EPH_RESEND_S):
            # процесс ждёт конверт: разовый ключ — основе (повтор, пока ждёт: сессия могла оборваться)
            self.rsv_update("rsv_in", eph_sent=now)
            actions.append(("send", "open_eph", {"gen": int(inn.get("gen") or 0), "eph": str(st.get("eph") or ""),
                                                 "oid": str(st.get("oid") or "")}))
        if step in ("clone_up", "record_set", "record_fact", "done") and not inn.get("up_sent"):
            self.rsv_update("rsv_in", up_sent=True, up_resent=now, clone_at=timeutil.now_iso())
            zst = rsvzone.state()
            if zst.get("ifaces") and not zst.get("netmap"):
                # вывод --take процесс открытия не пересказывает — факт берём из состояния зоны
                log.warning("резерв: NETMAP подсетей клонов не встал (ядро не приняло или пул reserve.zone_pool "
                            "занят) — устройства основы выходят одним адресом зоны, CAKE делит загрузку по назначению")
            actions += [("send", "up", {}), ("fw",)]
        elif step in ("clone_up", "record_set", "record_fact", "done") and livestep.mono_due(inn.get("up_resent"), now, 30):
            self.rsv_update("rsv_in", up_resent=now)          # сессия могла оборваться в секунду clone_up — повтор, основа дубли не берёт
            actions.append(("send", "up", {}))
        if step == "done" and not inn.get("served"):
            # факт записи (аварийный путь) или открытие без записи (кнопка: запись меняет основа)
            try:
                rsvzone.serve(self._webhook_port())
            except rsvzone.ZoneError as e:
                return self._take_fail(f"зона не приняла режим serve: {e}")
            self.rsv_update("rsv_in", served=True, done_mono=now)
            actions.append(("fw",))
            if hasattr(self, "reserve_tg_pool_refresh_soon"):
                self.reserve_tg_pool_refresh_soon(5)         # свежие анонсы — до первого вебхука
        if step == "done":
            return actions + self._tick_done_wait(inn, now)
        if livestep.mono_expired(inn.get("deadline"), now):
            return self._take_fail("процесс открытия не завершился за 35 минут")
        return actions

    def _tick_done_wait(self, inn: dict, now: float) -> list[tuple]:
        """После done у taking есть срок: аварийный путь ждёт `tg_taken` гостя
        (бот основы поднялся и поставил вебхук), кнопка — `released` основы.
        Гость не стартовал или не взял Telegram — отказ с причиной; основа с
        `released` опоздала и мертва для меня — Telegram берёт гость (клон уже
        несёт пользователей), жива — отказ."""
        from awgbot.infra import rsvzone
        done_at = inn.get("done_mono")
        if not done_at:
            self.rsv_update("rsv_in", done_mono=now)
            return []
        waited = livestep.mono_since(done_at, now)
        if str(inn.get("kind") or "") != "button":
            if waited < TAKEN_WAIT_S:
                return []
            if not rsvzone.guest_unit_active():
                return self._take_fail("бот основы на резерве не поднялся (юнит гостя не активен)")
            return self._take_fail("бот основы на резерве не взял право отвечать (вебхук не поставлен)")
        if waited < DONE_BUTTON_WAIT_S:
            return []
        if not inn.get("replica_at") and self.reserve_peer_seen() == "down" and not inn.get("tg_take_forced"):
            # основа умерла между up и released: клон несёт пользователей — пусть гость заберёт и Telegram
            log.warning("резерв: основа не отдала released и не на связи — гость берёт Telegram сам")
            self.rsv_update("rsv_in", tg_take_forced=True)
            try:
                rsvzone.write_private(rsvzone.TG_TAKE_FILE, "1")
            except OSError as e:
                return self._take_fail(f"отметка tg_take не записана: {e}")
            return []
        if inn.get("tg_take_forced") and waited < DONE_BUTTON_WAIT_S + TAKEN_WAIT_S:
            return []
        return self._take_fail("основа не передала бота (released) в срок")

    def _webhook_port(self) -> int:
        from awgbot.infra import rsvzone
        return int(rsvzone.read_json(rsvzone.WEBHOOK_FILE).get("port") or 0)

    def _take_fail(self, reason: str) -> list[tuple]:
        from awgbot.infra import rsvzone
        from awgbot.domain.notices import reserve as rn
        with _LOCK:                                            # обработчик кода и такт видят один и тот же fail: срыв — один раз
            inn = self._inn()
            if str(inn.get("state") or "") != "taking" or inn.get("failing"):
                return []
            self.rsv_update("rsv_in", failing=True)
        name = self.reserve_peer_name()
        extra: list[tuple] = []
        if str(self._inn().get("kind") or "") == "code" and hasattr(self, "reserve_code_failed"):
            extra = self.reserve_code_failed(reason)             # Н4: не подошёл — окно снова открыто
        try:
            self._rsvopen_stop()
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: юнит процесса открытия не остановлен: %s", e)
        log.warning("резерв: пользователей «%s» не принял: %s", name, reason)
        for fn in (rsvzone.teardown, rsvzone.wipe):
            try:
                fn()
            except rsvzone.ZoneError as e:
                log.warning("резерв: зона после отказа: %s", e)
        # ended_auto — только после состоявшегося переезда; после срыва — короткий демпфер (fail_at)
        self.reserve_set_in("idle", step="", reason=reason, ended_auto=False, fail_at=timeutil.now_iso(), failing=False)
        gw_word = self.reserve_gw_state_actions(force=True, reason=reason) if hasattr(self, "reserve_gw_state_actions") else []
        return [("send", "down", {"reason": reason[:120]}), ("fw",),
                ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.hosting_failed(name, reason))])] + extra + gw_word

    # ── реплика и право отвечать (переезд кнопкой) ──────────────────────────
    def reserve_released_store(self, blob: bytes) -> list[tuple]:
        """`released` от основы (кнопка): реплика → rep.bin, перезапуск бота
        основы (подхватит её на старте), отметка «ставь вебхук»."""
        from awgbot.infra import rsvzone
        inn = self._inn()
        if str(inn.get("state") or "") != "taking":
            log.info("резерв: released вне приёма — пропущен")
            return []
        try:
            # гость — стоп ДО записи реплики: его снимок раз в 30 с иначе мог бы перезаписать финальную
            subprocess.run(["systemctl", "stop", rsvzone.GUEST_UNIT], capture_output=True, timeout=60)
            rsvzone.write_private(rsvzone.REP_PATH, blob)
            rsvzone.write_private(rsvzone.TG_TAKE_FILE, "1")
            subprocess.run(["systemctl", "start", rsvzone.GUEST_UNIT], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("резерв: реплика не положена гостю: %s", e)
            return self._take_fail(f"реплика не положена боту основы: {e}")   # гость остановлен — ждать нечего
        self.rsv_update("rsv_in", replica_at=timeutil.now_iso())
        return []

    def reserve_guest_tg_taken(self) -> list[tuple]:
        """Гость поставил вебхук: `taken` основе, serving, Т-РУ4."""
        from awgbot.domain.notices import reserve as rn
        inn = self._inn()
        if str(inn.get("state") or "") not in ("taking", "serving") or inn.get("tg_taken"):
            return []
        with _LOCK:
            self.reserve_set_in("serving", kind=str(inn.get("kind") or "button"), gen=int(inn.get("gen") or 0),
                                tg_taken=True, served=True, taken_at=timeutil.now_iso(), step="")
        name = self.reserve_peer_name()
        log.info("резерв: пользователи и бот «%s» у меня", name)
        return [("send", "taken", {}), ("fw",),
                ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.hosting_started(
                    name, str(inn.get("kind") or "button"), str(inn.get("dead_since") or inn.get("open_started") or ""),
                    str(inn.get("extra") or "")))])]

    def _tick_serving(self, inn: dict) -> list[tuple]:
        """Зона снята мимо бота (`awg-bot reserve teardown`, перезагрузка хоста): состояние
        idle без паузы матрице, шлюзам основы — idle, админу — итог; пакет и доля остаются."""
        from awgbot.infra import rsvzone
        from awgbot.domain.notices import reserve as rn
        phase = str(rsvzone.state().get("phase") or "")
        if phase in ("serving", "taking"):
            return []
        name = self.reserve_peer_name()
        log.warning("резерв: зона в состоянии %s, а я считаю serving — пользователей «%s» больше не несу", phase or "idle", name)
        # пакет и долю не трогаем: основа может быть всё ещё мертва — матрица откроет снова
        # (С11), процесс возьмёт `rep.bin`, если он новее пакета
        with _LOCK:
            self.reserve_set_in("idle", step="", reason="зона снята", ended_auto=False)   # матрица откроет снова без паузы
        actions: list[tuple] = [("fw",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.hosting_failed(name, "зона снята"))])]
        if hasattr(self, "reserve_gw_state_actions"):
            actions += self.reserve_gw_state_actions(force=True)
        return actions

    # ── возврат ─────────────────────────────────────────────────────────────
    def reserve_back_req_in(self) -> list[tuple]:
        """`back_req` основы: гостю `stop`, ждать реплику."""
        from awgbot.infra import rsvzone
        inn = self._inn()
        if str(inn.get("state") or "") == "releasing" and inn.get("released_at"):
            # повторная просьба (обрыв канала на частях released): отдать последнюю реплику снова
            try:
                blob = rsvzone.REP_PATH.read_bytes()
            except OSError:
                blob = b""
            return [("send_released", {"gen": int(inn.get("gen") or 0), "blob": blob.hex()})]
        if str(inn.get("state") or "") == "releasing":
            return []                                         # реплика ещё в пути (гость останавливается) — ждать
        if str(inn.get("state") or "") not in ("serving", "taking"):
            # зоны нет (перезагрузка резерва посреди обслуживания, teardown мимо бота): что есть на
            # диске — rep.bin под K основы — отдать, иначе released без реплики; основа вернёт
            # пользователей без слияния, а не будет просить вечно
            try:
                blob = rsvzone.REP_PATH.read_bytes()
            except OSError:
                blob = b""
            gen = int(self.rsv_get("rsv_pkg_in").get("gen") or 0)
            log.warning("резерв: back_req при зоне в состоянии %s — отдаю %s", inn.get("state") or "idle",
                        "rep.bin" if blob else "released без реплики")
            return [("send_released", {"gen": gen, "blob": blob.hex()})]
        with _LOCK:
            self.reserve_set_in("releasing", kind=str(inn.get("kind") or ""), gen=int(inn.get("gen") or 0),
                                deadline=time.monotonic() + REPLICA_WAIT_S, taken_at=str(inn.get("taken_at") or ""))
        if str(inn.get("state") or "") == "taking":
            try:
                self._rsvopen_stop()                          # открытие ещё шло — юнит прочь
            except Exception as e:                            # noqa: BLE001
                log.info("резерв: юнит процесса открытия не остановлен: %s", e)
        return [("guest", "stop", {})]

    def reserve_guest_replica_in(self, blob: bytes) -> list[tuple]:
        """Реплика от гостя (или его `rep.bin`): `released` основе частями."""
        inn = self._inn()
        if str(inn.get("state") or "") != "releasing":
            return []
        self.rsv_update("rsv_in", released_at=timeutil.now_iso())
        return [("send_released", {"gen": int(inn.get("gen") or 0), "blob": blob.hex()})]

    @staticmethod
    def _relay_host_ip(host: str) -> str:
        """Адрес основы для DNAT хвоста: IPv4-литерал как есть; имя — резолвится
        сейчас (первый A); не резолвится — пусто (хвоста не будет, зона снимается)."""
        import ipaddress
        import socket
        if not host:
            return ""
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as e:
            log.warning("резерв: адрес основы %s для пересылки хвоста не резолвится: %s", host, e)
            return ""
        return str(infos[0][4][0]) if infos else ""

    def _tick_releasing(self, inn: dict) -> list[tuple]:
        from awgbot.infra import rsvzone
        if inn.get("released_at"):
            return []
        if livestep.mono_expired(inn.get("deadline"), time.monotonic()):
            # гость не ответил — отдать последний снимок с диска
            try:
                blob = rsvzone.REP_PATH.read_bytes()
            except OSError:
                blob = b""
            if blob:
                return self.reserve_guest_replica_in(blob)
            log.warning("резерв: реплики нет ни от гостя, ни на диске — released без реплики")
            self.rsv_update("rsv_in", released_at=timeutil.now_iso())
            return [("send_released", {"gen": int(inn.get("gen") or 0), "blob": ""})]
        return []

    def reserve_back_in(self) -> list[tuple]:
        """`back` основы: пересылка хвоста, учёт, `relay{until}`, снятие, `wiped`, Т-РУ5."""
        from awgbot.infra import rsvzone
        from awgbot.domain.notices import reserve as rn
        inn = self._inn()
        if str(inn.get("state") or "") == "relay":
            return [("send", "relay", {"until": str(inn.get("relay_until") or "")}), ("send", "wiped", {})]   # повтор
        if str(inn.get("state") or "") not in ("releasing", "serving", "taking"):
            return [("send", "wiped", {})]                    # зоны нет — основе нечего ждать
        name = self.reserve_peer_name()
        # пересылка хвоста нужна, только если пользователи могли прийти на клон: зона обслуживала
        # или процесс открытия дошёл до смены записи; срыв раньше — сразу idle
        step = str(rsvzone.open_status().get("step") or "")
        had_clients = str(inn.get("state") or "") != "taking" or step in ("record_set", "record_fact", "done")
        if str(inn.get("state") or "") == "taking":
            try:
                self._rsvopen_stop()                          # переезд сорвался на открытии — юнит прочь
            except Exception as e:                            # noqa: BLE001
                log.info("резерв: юнит процесса открытия не остановлен: %s", e)
        peer = self.reserve_peer_state()
        host = str(peer.get("self_host") or (self.reserve_peer() or {}).get("host") or "")
        host = self._relay_host_ip(host)                      # в правило nft — адрес: имя резолвится при загрузке таблицы
        ports = [int(p) for p in (peer.get("ports") or []) if isinstance(p, int)]
        to = f"{host}:{ports[0]}" if host and ports and had_clients else ""
        until = timeutil.to_iso(timeutil.shift(timeutil.now(), settings.get_int("app.reserve.relay_max_days", 7) * 86400))
        try:
            self.reserve_traffic_in_close()
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: учёт при возврате: %s", e)
        try:
            if to:
                rsvzone.relay(to)
            else:
                rsvzone.teardown()
        except rsvzone.ZoneError as e:
            log.warning("резерв: зона при возврате: %s", e)
        actions: list[tuple] = [("fw",)] + ([("relay_flush",)] if to else []) + [("send", "relay", {"until": until})]
        try:
            rsvzone.wipe()
        except rsvzone.ZoneError as e:
            log.warning("резерв: хранилище гостя не снято: %s", e)
        self.reserve_pkg_in_drop()
        if str(inn.get("kind") or "") in ("dead", "rf", "nogw", "code"):
            self.rsv_put("rsv_shares", None)              # прямая скомпрометирована — доля резерва прочь
        with _LOCK:
            self.reserve_set_in("relay", kind=str(inn.get("kind") or ""), relay_to=to, relay_until=until,
                                relay_at=timeutil.now_iso(), step="")
        actions.append(("send", "wiped", {}))
        tr = self._reserve_traffic_view().get("in") or ""
        actions.append(("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.hosting_ended(name, tr))]))
        log.info("резерв: пользователи и бот «%s» вернулись к нему; пересылка хвоста до %s", name, until)
        return actions

    def _tick_relay(self, inn: dict) -> list[tuple]:
        from awgbot.infra import rsvzone
        until = str(inn.get("relay_until") or "")
        done = False
        try:
            done = bool(until) and timeutil.parse_iso(until) <= timeutil.now()
        except ValueError:
            done = True
        idle_h = settings.get_int("app.reserve.relay_idle_hours", 2)
        if not done and idle_h:
            last = self._relay_last_traffic(inn)
            try:
                done = bool(last) and timeutil.elapsed(timeutil.parse_iso(last)) >= idle_h * 3600
            except ValueError:
                done = True
        if not done:
            return []
        try:
            rsvzone.teardown()
        except rsvzone.ZoneError as e:
            log.info("резерв: пересылка хвоста снята с ошибкой: %s", e)
        self.reserve_set_in("idle", step="", relay_to="", relay_until="")
        return [("fw",), ("send", "st", self.reserve_st_body())]

    def _relay_last_traffic(self, inn: dict) -> str:
        """Момент последнего пакета через правило пересылки (по счётчику правила DNAT
        в таблице хоста): счётчик вырос с прошлого такта — «сейчас»; счётчика нет —
        начало пересылки. Тишина считается от него."""
        from awgbot.infra import nftguard
        last = str(inn.get("relay_last_traffic") or inn.get("relay_at") or "")
        try:
            pkts = nftguard.relay_packets()
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: счётчик пересылки хвоста не прочитан: %s", e)
            pkts = None
        if pkts is None:
            return last
        seen = inn.get("relay_pkts")
        if seen is not None and int(pkts) > int(seen):       # меньше — перерисовка таблицы обнулила счётчик
            last = timeutil.now_iso()
        if seen is None or int(pkts) != int(seen) or not inn.get("relay_last_traffic"):
            self.rsv_update("rsv_in", relay_pkts=int(pkts), relay_last_traffic=last)
        return last

    # ── возврат по кнопке у резерва ─────────────────────────────────────────
    def reserve_evict_check(self) -> str:
        if not self.reserve_hosting():
            return f"пользователей «{self.reserve_peer_name()}» здесь нет"
        if not self.reserve_online():
            return f"«{self.reserve_peer_name()}» не на связи"
        if self.reserve_in() == "releasing":
            return "возврат уже идёт"
        return ""

    def reserve_evict_dialog_data(self) -> dict:
        return {"name": self.reserve_peer_name()}

    def reserve_guest_back_req(self) -> list[tuple]:
        """Кнопка «↩️ Вернуть пользователей» в боте основы на резерве → `evict` основе."""
        if self.reserve_evict_check():
            return []
        return [("send", "evict", {})]
