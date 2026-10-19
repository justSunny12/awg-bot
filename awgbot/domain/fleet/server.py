"""server.py — часть пакета fleet (см. __init__): шаг сервера AWG над собой —
ступень вперёд (`awg-bot update` с удержанием), фиксация и откат набора, откат
сторожем.

Шаг сервера отмечается в ходе ДО того, как ушёл апдейтер (`run["server"]`:
вид, ожидаемая версия, процесс, время): иначе такт, пришедший, пока апдейтер
ещё собирает ядро (минуты, сторожа ещё нет), запустил бы второй. Ждём факта:
тот же процесс — пока апдейтер жив (его pid в `update-running`) или идут
первые 90 секунд после запуска; новый процесс — сторож обновления снят. Факт —
версия установки: ожидаемая — шаг сделан; другая или 20 минут без конца —
шаг не удался (ступень — откат набора, откат — `revert_failed`).
"""
from __future__ import annotations

from awgbot.core import config
from awgbot.domain.notices import fleet as notes_
from awgbot.domain.selfupdate import marks
from awgbot.infra import updates

from . import order as _order
from .order import FleetOut, _LOCK, _now_iso, _since
from .state import ROLLING_BACK, RUNNING, new_id

PROC = new_id()                 # этот процесс бота: шаг, начатый им, ещё не мог дойти до конца
SPAWN_GRACE_S = 90              # апдейтер успевает взять замок и записать pid
STEP_MAX_S = 20 * 60            # ступень (с ядром) или откат дольше — не удались
def _mark(kind: str, want: str) -> dict:
    return {"kind": kind, "want": str(want or ""), "proc": PROC, "at": _now_iso()}


class FleetServerMixin:
    """Сервер AWG — шаг набора над собой."""

    def fleet_server_settled(self) -> bool:
        """Сервер не посреди своего шага: сторож не взведён и отметки шага нет."""
        return not marks.guard_armed() and not self.fleet_run().get("server")

    def _fleet_server_mark(self, run: dict, kind: str, want: str) -> None:
        run["server"] = _mark(kind, want)

    def _fleet_server_wait(self, run: dict, out: FleetOut) -> bool:
        """True — сервер посреди шага (ждём факта) или шаг только что признан
        неудавшимся (out уже несёт откат); False — можно идти дальше."""
        if marks.guard_armed():
            return True                                   # новый процесс ещё не отметился
        mark = run.get("server") or {}
        if not mark:
            return False
        age = _since(str(mark.get("at") or ""))
        same = mark.get("proc") == PROC
        if age < STEP_MAX_S and (marks.install_busy() or (same and age < SPAWN_GRACE_S)):
            return True
        want = updates.parse_version(str(mark.get("want") or ""))
        ok = want is not None and updates.parse_version(config.INSTALLED_VERSION) == want
        run.pop("server", None)
        self.fleet_run_set(run)
        if ok:
            return False
        if mark.get("kind") == "apply":
            steps = run.get("steps") or []
            if steps and steps[-1][0] == "main":
                steps.pop()
            run["undo"] = len(steps)
            reason = notes_.SERVER_STEP_LATE if age >= STEP_MAX_S else notes_.SERVER_STEP_NOT_APPLIED
            out.merge(self._fleet_rollback_begin(run, FleetOut(), "main", reason))
        else:
            out.merge(self._fleet_server_revert_failed_run(run, notes_.SERVER_NOT_BACK))
        return True

    def fleet_server_act(self, out: FleetOut) -> FleetOut:
        """Исполнить out.server (после того, как живое сообщение показало шаг):
        apply — ступень сервера с удержанием прежнего (id хода); commit —
        прежнее прочь; revert — прежнее на место. Отказ — следующий такт."""
        if out.server == "apply":
            run_id = str(self.fleet_run().get("id") or "") or "1"
            try:
                self.apply_update(out.step, hold=run_id)
            except Exception as e:                        # noqa: BLE001 — сеть, sha256, переезд
                import logging
                logging.getLogger("awgbot.fleet").warning(
                    "набор: ступень сервера %s не запустилась: %s", getattr(out.step, "tag", "?"), e)
                return self.fleet_server_failed(str(e))
            return FleetOut()
        if out.server == "commit":
            _order._spawn([updates._AWG_BOT_BIN, "__update_commit"], "awg-bot-update-commit")
            return FleetOut()
        if out.server == "revert":
            if marks.revert_check() != 0:
                return self.fleet_server_revert_failed("прежней версии сервера AWG нет — откатывать некуда")
            _order._spawn([updates._AWG_BOT_BIN, "__update_revert"], "awg-bot-update-revert")
        return FleetOut()

    def fleet_server_failed(self, reason: str) -> FleetOut:
        """Ступень сервера не запустилась (сеть GitHub, sha256, место) — сервер
        на прежнем: шаг снимается, откат того, что успели."""
        with _LOCK:
            run = self.fleet_run()
            if run.get("state") != RUNNING:
                return FleetOut()
            run.pop("server", None)
            steps = run.get("steps") or []
            if steps and steps[-1][0] == "main":
                steps.pop()
            run["undo"] = len(steps)
            return self._fleet_rollback_begin(run, FleetOut(), "main", reason)

    def fleet_server_revert_failed(self, reason: str) -> FleetOut:
        """Откатить сервер нечем (прежнего кода нет) — остальные откатываются дальше."""
        with _LOCK:
            run = self.fleet_run()
            if run.get("state") != ROLLING_BACK:
                return FleetOut()
            run.pop("server", None)
            return self._fleet_server_revert_failed_run(run, reason)

    def _fleet_server_revert_failed_run(self, run: dict, reason: str) -> FleetOut:
        if "main" not in run.setdefault("revert_failed", []):
            run["revert_failed"].append("main")
        run["reason_main"] = " ".join(str(reason or "").split())[:300]
        self.fleet_run_set(run)
        return self._fleet_backward(run, FleetOut(live=True))

    # ── старт процесса: откат сервера сторожем ──────────────────────────────

    def fleet_on_start(self) -> FleetOut:
        """Сторож вернул прежнюю версию сервера (метка awg-bot.sh): уведомление; в ходе
        — снимается только последний шаг сервера (сторож вернул одну ступень),
        остальное откатывается обратным ходом."""
        with _LOCK:
            out = FleetOut()
            mark = marks.pop_rolled_mark()
            if not mark:
                return out
            ver, prev = str(mark.get("version") or ""), str(mark.get("prev") or config.INSTALLED_VERSION)
            from awgbot.domain.services import Notification
            run = self.fleet_run()
            tag = run.get("tag") if run.get("state") == RUNNING else ver
            out.notes.append(Notification(config.ADMIN_ID, notes_.server_rolled_back(tag or ver, prev),
                                          silent=True))
            if run.get("state") == RUNNING:
                steps = run.get("steps") or []
                if steps and steps[-1][0] == "main":
                    steps.pop()                           # ступень, которую вернул сторож; прежние — в удержании
                run.pop("server", None)
                run["undo"] = len(steps)
                out.merge(self._fleet_rollback_begin(run, FleetOut(), "main", notes_.SERVER_SILENT))
            return out
