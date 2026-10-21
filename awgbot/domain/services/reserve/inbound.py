"""
inbound.py — часть пакета reserve (см. __init__): «клиенты основы у меня» —
сторона резерва: процесс открытия, зона, обслуживание, возврат (handback).

Автомат в `rsv_in` (`state`: idle | taking | serving | releasing | relay).
Такт (`reserve_in_tick`, 3 с) читает ход процесса открытия
(`/run/awg-bot/rsv/open.status`), состояние зоны и локального канала с
ботом основы на резерве и отдаёт действия runtime: `("send", вид, тело)`,
`("guest", команда, тело)` — боту основы по локальному каналу, `("notify",
[...])`, `("fw",)` — перерисовать файервол хоста.

Переезд кнопкой (`go{K, gen, kind}`; отказ, если зона занята или мои
клиенты не здесь): `open.status = launching`, процесс открытия временным
юнитом с песочницей (stdin — JSON с `K`; запись DNS меняет основа; env и
bot.db резерва юниту закрыты, `AWG_BOT_ENV=/dev/null`) → `clone_up` →
`up` основе → `released{реплика}` → гость стоп, `rep.bin`, гость старт (он
подхватит реплику), отметка `tg_take` → `tg_taken` от гостя → `taken` основе,
serving, Т-РУ4. Отказ процесса — `down{причина}`, юнит открытия стоп, зона
снята.

Возврат: `back_req` → гостю `stop` (открытие ещё шло — юнит стоп) →
`replica` (гость молчит 90 с — `rep.bin`) → `released` основе (releasing;
повторный `back_req` — та же реплика из `rep.bin` снова) → `back` (повтор в
relay — снова `relay` и `wiped`) → пересылка хвоста (`--relay`, файервол), учёт,
`relay{until}`, `--wipe`, прежний пакет и доля — прочь, `wiped`, Т-РУ5 →
relay до `relay_max_days`/тишины → idle. `evict` — просьба вернуть клиентов
по кнопке у резерва.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from awgbot.core import config, settings
from awgbot.util import livestep, timeutil

from ..types import Notification, ServiceError
from ..gateway_link.common import log
from .state import _LOCK

OPEN_UNIT = "awg-bot-rsvopen"
OPEN_WAIT_S = 35 * 60            # процесс открытия: факт записи ≤ 30 мин
REPLICA_WAIT_S = 90              # реплика от гостя после stop


class ReserveInboundMixin:
    def _inn(self) -> dict:
        return self.rsv_get("rsv_in")

    def _in_step(self, step: str, **fields) -> dict:
        return self.rsv_update("rsv_in", step=step, step_at=timeutil.now_iso(), **fields)

    # ── процесс открытия ────────────────────────────────────────────────────
    def reserve_go_in(self, k_hex: str, gen: int, kind: str) -> str:
        """`go` от основы: запустить процесс открытия. Пусто — запущен; иначе
        причина отказа (уйдёт в `down`)."""
        pkg_in = self.rsv_get("rsv_pkg_in")
        if int(pkg_in.get("gen") or 0) != int(gen):
            return f"у меня пакет {pkg_in.get('gen') or 'нет'}, а не {gen}"
        if self.reserve_in() != "idle":
            return "зона занята"
        if self.reserve_out() != "home":
            return "мои клиенты не на этом сервере"
        if not settings.get_bool("app.reserve.ready", False):
            return "готовность выключена"
        ok, why = self.reserve_directions()["in"]
        if not ok:
            return why
        try:
            bytes.fromhex(str(k_hex))
        except ValueError:
            return "ключ не hex"
        if hasattr(self, "reserve_zone_shell_ensure"):
            self.reserve_zone_shell_ensure()                  # после перезагрузки хоста зоны нет — --take отказал бы
        payload = self._open_payload(kind)
        payload["k"] = str(k_hex)
        with _LOCK:
            if self.reserve_in() != "idle":                   # проверка и запись — под одним замком
                return "зона занята"
            from awgbot.infra import rsvzone
            rsvzone.open_status_write("launching")           # прежний open.status не должен сойти за ход этого открытия
            self.reserve_set_in("taking", kind=str(kind or "button"), gen=int(gen), why=str(kind or "button"), by="k",
                                open_started=timeutil.now_iso(), deadline=time.monotonic() + OPEN_WAIT_S,
                                up_sent=False, tg_taken=False)
            self._in_step("open")
        try:
            self._rsvopen_launch(payload)
        except ServiceError as e:
            self.reserve_set_in("idle", step="", reason=str(e))
            return str(e)
        log.info("резерв: процесс открытия запущен (%s, пакет %s)", kind, gen)
        return ""

    def _open_payload(self, kind: str) -> dict:
        from awgbot.infra import nftguard, rsvzone
        from ..selfhost import own_host
        plan = rsvzone.plan()
        try:
            port = int(plan.get("webhook_port") or 0)
        except ValueError:
            port = 0
        return {"set_record": kind != "button", "tg_take": False, "webhook_port": port, "self_host": own_host(),
                "target_ip": self.reserve_own_ip(), "tg_pool": list(nftguard.tg_pool()),
                "channel_port": int(settings.get_int("app.routing.link_channel_port", 8787) or 8787)}

    def _rsvopen_launch(self, payload: dict) -> None:
        """Временный юнит с песочницей (systemd-run); без systemd — просто процесс.
        Секреты — только в stdin."""
        from awgbot.infra import rsvzone
        base = str(config.BASE_DIR)
        argv = [os.path.join(base, "venv", "bin", "python"), "-m", "tools.rsvopen"]
        if not os.path.exists(argv[0]):
            argv[0] = "python3"
        # env резерва (его токен) процессу не нужен: AWG_BOT_ENV — пустой файл, а
        # /etc/awg-bot/env закрыт юниту; BOT_TOKEN/ADMIN_ID из окружения бота — вон
        env = {k: v for k, v in os.environ.items() if k not in ("BOT_TOKEN", "ADMIN_ID")}
        env.update({"AWG_BOT_CONF_DIR": str(config.CONF_DIR), "AWG_BOT_DATA_DIR": str(config.DATA_DIR),
                    "AWG_BOT_ENV": "/dev/null"})
        if shutil.which("systemd-run"):
            props = ["-p", "MemoryMax=96M", "-p", "MemorySwapMax=0", "-p", "LimitCORE=0", "-p", "Nice=5",
                     "-p", "PrivateTmp=yes", "-p", "ProtectHome=yes", "-p", "ProtectSystem=full",
                     "-p", "NoNewPrivileges=yes",
                     "-p", f"InaccessiblePaths=-{config.ENV_PATH or '/etc/awg-bot/env'}",
                     "-p", f"WorkingDirectory={base}",
                     # скрипт зоны пишет юниты и настройки журнала гостя в /etc/systemd
                     "-p", f"ReadWritePaths={rsvzone.RUN_DIR} {rsvzone.VAR_DIR} {rsvzone.STATE_DIR} /etc/systemd",
                     # база резерва процессу не нужна: пакет лежит в reserve/, а клиенты основы — в части бота
                     "-p", f"InaccessiblePaths=-{config.DATA_DIR / 'bot.db'}"]
            setenv = [f"--setenv={k}={env[k]}" for k in ("AWG_BOT_CONF_DIR", "AWG_BOT_DATA_DIR", "AWG_BOT_ENV") if k in env]
            cmd = ["systemd-run", "--collect", "--quiet", "--pipe", f"--unit={OPEN_UNIT}", *props, *setenv, *argv]
        else:
            cmd = argv
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    cwd=base, env=env, close_fds=True)
            proc.stdin.write(json.dumps(payload).encode())
            proc.stdin.close()
        except OSError as e:
            raise ServiceError(f"процесс открытия не запустился: {e}")
        self.__dict__["_rsvopen_proc"] = proc
        rsvzone.open_status_write("launched")

    @staticmethod
    def _rsvopen_stop() -> None:
        """Остановить временный юнит процесса открытия (срыв, `back` на шаге up)."""
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "stop", f"{OPEN_UNIT}.service"], capture_output=True, timeout=30)

    def reserve_in_tick(self) -> list[tuple]:
        """Такт автомата «клиенты основы у меня»."""
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
        if step in ("clone_up", "record_set", "record_fact", "done") and not inn.get("up_sent"):
            self.rsv_update("rsv_in", up_sent=True, clone_at=timeutil.now_iso())
            actions += [("send", "up", {}), ("fw",)]
        if step == "done" and not inn.get("served"):
            # факт записи (аварийный путь) или открытие без записи (кнопка: запись меняет основа)
            try:
                rsvzone.serve(self._webhook_port())
            except rsvzone.ZoneError as e:
                return self._take_fail(f"зона не приняла режим serve: {e}")
            self.rsv_update("rsv_in", served=True)
            actions.append(("fw",))
            if str(inn.get("kind") or "") != "button":
                # аварийный переезд: право отвечать берёт гость сам по отметке tg_take
                pass
        if livestep.mono_expired(inn.get("deadline"), time.monotonic()) and step != "done":
            return self._take_fail("процесс открытия не завершился за 35 минут")
        return actions

    def _webhook_port(self) -> int:
        from awgbot.infra import rsvzone
        return int(rsvzone.read_json(rsvzone.WEBHOOK_FILE).get("port") or 0)

    def _take_fail(self, reason: str) -> list[tuple]:
        from awgbot.infra import rsvzone
        from awgbot.domain.notices import reserve as rn
        name = self.reserve_peer_name()
        extra: list[tuple] = []
        if str(self._inn().get("kind") or "") == "code" and hasattr(self, "reserve_code_failed"):
            extra = self.reserve_code_failed(reason)             # Н4: не подошёл — окно снова открыто
        try:
            self._rsvopen_stop()
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: юнит процесса открытия не остановлен: %s", e)
        log.warning("резерв: клиентов «%s» не принял: %s", name, reason)
        for fn in (rsvzone.teardown, rsvzone.wipe):
            try:
                fn()
            except rsvzone.ZoneError as e:
                log.warning("резерв: зона после отказа: %s", e)
        self.reserve_set_in("idle", step="", reason=reason)
        return [("send", "down", {"reason": reason[:120]}), ("fw",),
                ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.hosting_failed(name, reason))])] + extra

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
            return []
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
        log.info("резерв: клиенты и бот «%s» у меня", name)
        return [("send", "taken", {}), ("fw",),
                ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.hosting_started(
                    name, str(inn.get("kind") or "button"), str(inn.get("dead_since") or inn.get("open_started") or ""),
                    str(inn.get("extra") or "")))])]

    def _tick_serving(self, inn: dict) -> list[tuple]:
        """Зона снята мимо бота (`awg-bot reserve teardown`, перезагрузка хоста): состояние
        idle, шлюзам основы — idle, пакет и доля — прочь, админу — итог."""
        from awgbot.infra import rsvzone
        from awgbot.domain.notices import reserve as rn
        phase = str(rsvzone.state().get("phase") or "")
        if phase in ("serving", "taking"):
            return []
        name = self.reserve_peer_name()
        log.warning("резерв: зона в состоянии %s, а я считаю serving — клиентов «%s» больше не несу", phase or "idle", name)
        self.reserve_pkg_in_drop()
        with _LOCK:
            self.reserve_set_in("idle", step="", reason="зона снята", ended_auto=True)
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
        if str(inn.get("state") or "") not in ("serving", "taking"):
            return []
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
            return []
        name = self.reserve_peer_name()
        if str(inn.get("state") or "") == "taking":
            try:
                self._rsvopen_stop()                          # переезд сорвался на открытии — юнит прочь
            except Exception as e:                            # noqa: BLE001
                log.info("резерв: юнит процесса открытия не остановлен: %s", e)
        peer = self.reserve_peer_state()
        host = str(peer.get("self_host") or (self.reserve_peer() or {}).get("host") or "")
        ports = [int(p) for p in (peer.get("ports") or []) if isinstance(p, int)]
        to = f"{host}:{ports[0]}" if host and ports else ""
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
        actions: list[tuple] = [("fw",), ("send", "relay", {"until": until})]
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
        log.info("резерв: клиенты и бот «%s» вернулись к нему; пересылка хвоста до %s", name, until)
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
        if not done and idle_h and str(inn.get("relay_at") or ""):
            try:
                done = timeutil.elapsed(timeutil.parse_iso(str(inn["relay_at"]))) >= idle_h * 3600 and not self._relay_busy()
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

    def _relay_busy(self) -> bool:
        """Есть ли ещё трафик на правиле пересылки — пока не считаем: по сроку тишины."""
        return False

    # ── возврат по кнопке у резерва ─────────────────────────────────────────
    def reserve_evict_check(self) -> str:
        if not self.reserve_hosting():
            return f"клиентов «{self.reserve_peer_name()}» здесь нет"
        if not self.reserve_online():
            return f"«{self.reserve_peer_name()}» не на связи"
        if self.reserve_in() == "releasing":
            return "возврат уже идёт"
        return ""

    def reserve_evict_dialog_data(self) -> dict:
        return {"name": self.reserve_peer_name()}

    def reserve_guest_back_req(self) -> list[tuple]:
        """Кнопка «↩️ Вернуть клиентов» в боте основы на резерве → `evict` основе."""
        if self.reserve_evict_check():
            return []
        return [("send", "evict", {})]
