"""order.py — часть пакета fleet (см. __init__): ход набора вперёд — старт,
шаг сервера, очередь шлюзов, ответы шлюзов, фиксация.

Решает такт (`fleet_tick`): его зовут слушатель канала (hello шлюза, ответ
`upd_ack`, такт живости) и старт бота. Такт не ходит в сеть и не шлёт сам:
отдаёт, что отправить шлюзам (`FleetOut.send`), что сделать серверу
(`server`: «apply» со ступенью, «commit», «revert»), что сказать админу
(`notes`) и надо ли перерисовать живое сообщение (`live`). Состояние
записано ДО того, как действие ушло: перезапуск посреди шага продолжает с
записанного, а не повторяет.

Порядок: сначала сервер, затем шлюзы по одному — резервные, последним
активный; выпуску сервера нужны новые шлюзы (`needs_gw`) — сначала шлюзы.
Следующий шаг — только по факту предыдущего: шлюз ответил `done` или стал
недоступен (без сессии 15 минут), сервер — новый процесс вышел на связь
(сторож обновления снят, отметка жизни есть; server.py). Шлюз на связи без
ответа полчаса — неудача шага. Команда вне хода (догон, следование), ещё в
пути у шлюза на связи, ход держит: два обновления разом агент не примет.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from awgbot.core import config
from awgbot.infra import updates
from awgbot.util import timeutil

from awgbot.domain.notices import fleet as notes_

from .state import (ACTIVE_RUN, DONE, FAILURES, IN_FLIGHT, ROLLING_BACK, RUNNING, new_id)

log = logging.getLogger("awgbot.fleet")

LOST_AFTER_S = 15 * 60          # шаг шлюза без сессии столько — шлюз недоступен, ход идёт дальше
GW_STEP_MAX_S = 30 * 60         # шлюз на связи без ответа на шаг столько — шаг не удался

_LOCK = threading.RLock()       # один такт за раз: слушатель, кнопка и старт решают в разных потоках


@dataclass
class FleetOut:
    """Что сделать после такта: send — [(слот, вид, тело)], server — действие
    сервера над собой, step — ступень для «apply», notes — уведомления,
    live — живое сообщение изменилось, final — ход закончился."""
    send: list = field(default_factory=list)
    server: str = ""
    step: object = None
    notes: list = field(default_factory=list)
    live: bool = False
    final: bool = False

    def merge(self, other: "FleetOut") -> "FleetOut":
        self.send += other.send
        self.notes += other.notes
        self.server = self.server or other.server
        self.step = self.step or other.step
        self.live = self.live or other.live
        self.final = self.final or other.final
        return self


def _now_iso() -> str:
    return timeutil.to_iso(timeutil.now())


def _since(iso: str) -> float:
    try:
        return timeutil.elapsed(timeutil.parse_iso(iso))
    except ValueError:
        return 0.0


class FleetOrderMixin:
    """Ход набора вперёд."""

    # ── старт ────────────────────────────────────────────────────────────────

    def fleet_start(self, tag: str) -> str:
        """«⬆️ Обновить»: начать ход к tag. Пусто — начат; иначе причина
        (всплывашкой): идёт ход, цель сменилась, сервер не на сборке из выпуска."""
        with _LOCK:
            run = self.fleet_run()
            if run.get("state") in ACTIVE_RUN:
                return "running"
            target = self.fleet_target()
            if target is None:
                return "nothing"
            if target.tag != tag:
                return "changed"
            if self._fleet_links_busy():
                return "links"                            # смена ключей линка или сопряжение: ход — после их конца
            self.fleet_run_set({
                "id": new_id(), "tag": target.tag, "prev": self.fleet_release(),
                "main_prev": _v(config.INSTALLED_VERSION),
                "digest": f"sha256:{target.sha256}" if target.sha256 else "",
                "why": "button", "at": _now_iso(), "state": RUNNING, "steps": [], "undo": 0,
                "failed_on": "", "reason": "", "revert_failed": [], "msg": []})
            log.info("набор: ход до %s начат", target.tag)
            return ""

    def _fleet_links_busy(self) -> bool:
        """Идёт смена ключей линка (ручная или ради I1) или сопряжение: ход
        набора их ждёт — как и они ждут ход (перезапуск агента посреди
        переключения ключей сжёг бы попытку)."""
        if any(self.gateway_rekey_active(gw.id) for gw in self.db.gateways()):
            return True
        return bool(getattr(self, "gwjoin_active", lambda: False)())

    def update_applied_note(self, pending: str, ok: bool):
        """Итог своего обновления после перезапуска (selfupdate/core): в ходе
        набора — без отдельного сообщения, о нём говорит живое сообщение
        набора; вне хода — обычный итог роли (DEFAULT_NOTE)."""
        from awgbot.domain.selfupdate.core import DEFAULT_NOTE
        return None if self.fleet_run().get("state") in ACTIVE_RUN else DEFAULT_NOTE

    def fleet_set_message(self, chat_id: int, message_id: int) -> None:
        with _LOCK:
            run = self.fleet_run()
            if run:
                run["msg"] = [int(chat_id), int(message_id)]
                self.fleet_run_set(run)

    # ── такт ─────────────────────────────────────────────────────────────────

    def fleet_tick(self) -> FleetOut:
        """Следующий шаг набора по фактам. Вне хода — догон и следование
        шлюзов за выпуском набора (rollback.py)."""
        with _LOCK:
            out = FleetOut()
            run = self.fleet_run()
            if run.get("state") == RUNNING:
                return self._fleet_forward(run, out)
            if run.get("state") == ROLLING_BACK:
                return self._fleet_backward(run, out)
            return self._fleet_outside(run, out)

    def _fleet_forward(self, run: dict, out: FleetOut) -> FleetOut:
        if self._fleet_server_wait(run, out):
            return out                                    # сервер посреди своего шага — ждём факта
        target = self.fleet_release_of(str(run.get("tag") or ""))
        if target is None:
            return out                                    # выпуски ещё не перечитаны после перезапуска
        gws = self.fleet_gateways()
        # шаг шлюза в пути: ждём его ответа или признаём недоступным
        for g in gws:
            st = self.fleet_slot(g["slot"])
            if st.get("state") not in IN_FLIGHT:
                continue
            if st.get("run") != run["id"]:
                # команда вне хода у шлюза на связи — ждём её итога (второй агент не примет)
                if g["online"] and _since(str(st.get("at") or "")) < GW_STEP_MAX_S:
                    return out
                continue
            if g["online"]:
                if st.get("off_since"):
                    st["off_since"] = ""
                    self.fleet_slot_set(g["slot"], st)
                if _since(str(st.get("at") or "")) >= GW_STEP_MAX_S:
                    st["state"], st["error"] = "failed", notes_.GW_NO_ANSWER
                    self.fleet_slot_set(g["slot"], st)
                    return self._fleet_rollback_begin(run, out, int(g["slot"]), notes_.GW_NO_ANSWER)
                return out
            if not st.get("off_since"):
                st["off_since"] = _now_iso()
                self.fleet_slot_set(g["slot"], st)
                return out
            if _since(st["off_since"]) < LOST_AFTER_S:
                return out
            st["state"], st["lost_from"] = "lost", st.get("state") or ""
            self.fleet_slot_set(g["slot"], st)
            out.live = True
            log.info("набор: «%s» пропал посреди обновления — ход идёт дальше", g["name"])
        if self._fleet_expecting(run, gws):
            return out                                    # шлюзы прежнего процесса ещё переподключаются
        step, needs_gw = self.fleet_server_step(target)
        if step is not None:
            run["steps"].append(["main", step.tag])
            run["undo"] = len(run["steps"])
            self._fleet_expect(run, gws)
            self._fleet_server_mark(run, "apply", step.tag)   # до апдейтера: второй такт его не повторит
            self.fleet_run_set(run)
            out.server, out.step, out.live = "apply", step, True
            return out
        gw_target = self.fleet_gw_target(target.tag)
        queue = []
        if gw_target is not None:
            for g in gws:                                 # по порядку хода: резервные, активный последним
                if not g["participant"]:
                    continue
                st = self.fleet_slot(g["slot"])
                if st.get("run") == run["id"] and st.get("state") in ("done", "lost") + FAILURES:
                    continue
                if updates.parse_version(g["version"]) == gw_target.version:
                    continue
                queue.append(g)
        if queue:
            g = queue[0]
            out.send.append(self._fleet_command(run, g, gw_target, why="fleet", hold=True))
            out.live = True
            return out
        if needs_gw:
            # шлюзы на своей версии, а ступени сервера всё нет — выпуск ему недоступен
            return self._fleet_rollback_begin(run, out, "main",
                                              "серверу AWG нужны агенты шлюзов новее, чем в выпуске")
        return self._fleet_commit(run, out)

    def _fleet_expect(self, run: dict, gws: list) -> None:
        """Сервер перезапускается своим шагом: доступные сейчас участники
        переподключатся к новому процессу — их ждём (факт — их hello), не
        дольше LOST_AFTER_S; без этого новый процесс счёл бы их недоступными
        и зафиксировал набор без них."""
        run["expect"] = [g["slot"] for g in gws if g["participant"]]
        run["expect_at"] = _now_iso()

    def _fleet_expecting(self, run: dict, gws: list) -> bool:
        """Ждём ли ещё переподключения шлюзов после перезапуска сервера."""
        expect = [int(x) for x in run.get("expect") or []]
        if not expect:
            return False
        online = {g["slot"] for g in gws if g["online"]}
        left = [x for x in expect if x not in online and any(g["slot"] == x for g in gws)]
        if left and _since(str(run.get("expect_at") or "")) < LOST_AFTER_S:
            return True
        run["expect"], run["expect_at"] = [], ""
        self.fleet_run_set(run)
        return False

    def _fleet_command(self, run: dict, g: dict, target, *, why: str, hold: bool, tries: int = 0) -> tuple:
        """Команда `upd` шлюзу и его состояние «sent»."""
        st = {"id": new_id(), "run": run.get("id", "") if run else "", "target": target.tag,
              "prev": _v(g["version"]) if g["version"] else "", "state": "sent",
              "step": "", "error": "", "why": why, "hold": hold, "at": _now_iso(),
              "digest": f"sha256:{target.sha256}" if target.sha256 else "", "tries": tries}
        self.fleet_slot_set(g["slot"], st)
        return (g["slot"], "upd", _upd_body(st))

    def _fleet_commit(self, run: dict, out: FleetOut) -> FleetOut:
        """Все доступные на выпуске хода — фиксация: прежнее у хостов прочь;
        выпуск набора — тег хода (ключ пишет только фиксация)."""
        run["state"] = DONE
        run["at"] = _now_iso()
        self.fleet_run_set(run)
        self.fleet_release_set(str(run.get("tag") or ""))
        for g in self.fleet_gateways():
            st = self.fleet_slot(g["slot"])
            if st.get("run") == run["id"] and st.get("hold") and st.get("state") == "done" and g["online"]:
                out.send.append((g["slot"], "upd_commit", {"id": st["id"]}))
                st["pending"] = "upd_commit"              # удержание снимет только сданная в сокет фиксация
                self.fleet_slot_set(g["slot"], st)
        if any(s[0] == "main" for s in run.get("steps") or []):
            out.server = "commit"
        out.live = out.final = True
        log.info("набор: ход до %s зафиксирован", run.get("tag"))
        return out

    # ── ответы шлюзов ────────────────────────────────────────────────────────

    def fleet_ack(self, slot_id: int, msg: dict) -> FleetOut:
        """`upd_ack` шлюза: записать итог; неудача доступного шлюза в ходе —
        откат набора; вне хода — повторы (rollback.py)."""
        with _LOCK:
            out = FleetOut()
            st = self.fleet_slot(slot_id)
            if not st or str(msg.get("id") or "") != st.get("id"):
                return out                                # чужая или устаревшая команда
            state = str(msg.get("state") or "")
            if state not in ("accepted", "done", "reverted", "revert_failed") + FAILURES:
                return out
            st["state"] = state
            st["step"] = str(msg.get("step") or st.get("step") or "")[:32]
            st["error"] = " ".join(str(msg.get("error") or "").split())[:300]
            st["at"] = _now_iso()
            st["off_since"] = ""
            self.fleet_slot_set(slot_id, st)
            out.live = True
            run = self.fleet_run()
            mine = bool(st.get("run")) and st["run"] == run.get("id")
            if mine and run.get("state") == RUNNING:
                if state == "done":
                    run["steps"].append(["gw", int(slot_id), st["target"]])
                    run["undo"] = len(run["steps"])
                    self.fleet_run_set(run)
                elif state in FAILURES:
                    return self._fleet_rollback_begin(run, out, int(slot_id), st["error"] or state)
            elif mine and run.get("state") == ROLLING_BACK:
                if state in ("reverted", "revert_failed"):
                    self._fleet_revert_answered(run, int(slot_id), state)
            elif not st.get("run") or state in ("reverted", "revert_failed"):
                # догон или следование вне хода; удержание прошлого хода доведено
                out.merge(self._fleet_outside_ack(slot_id, st, state))
            return out.merge(self.fleet_tick())


def _spawn(argv: list, unit: str) -> None:
    """Команда awg-bot.sh вне cgroup бота (она перезапускает сервис)."""
    from awgbot.infra.detach import spawn_detached
    spawn_detached(argv, unit_prefix=unit)


def _revert_check() -> int:
    """`awg-bot __update_revert --check`: 0 — есть на что откатиться, 2 — нет."""
    import subprocess
    try:
        return subprocess.run([updates._AWG_BOT_BIN, "__update_revert", "--check"],
                              capture_output=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return 2


def _v(ver: str) -> str:
    return ver if ver.startswith("v") else f"v{ver}"


def _upd_body(st: dict) -> dict:
    return {"id": st["id"], "target": st["target"], "digest": st.get("digest") or "",
            "why": st.get("why") or "fleet", "hold": bool(st.get("hold"))}
