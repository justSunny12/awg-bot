"""rollback.py — часть пакета fleet (см. __init__): откат набора, откат
сервера сторожем, догон и следование вне хода, повторы.

Откат (Р21) — шагами хода в обратном порядке, по одному: шлюз —
`upd_revert` (прежнее у него в удержании, без сети), сервер — `awg-bot
__update_revert` и перезапуск, откат продолжает новый (прежний) процесс.
Каждое состояние отката уже встречалось в ходе вперёд (§12.1, п. 17). Хост,
не вернувшийся, — в `revert_failed`, остальные откатываются дальше; итог —
`rollback_failed`, и автоматических команд шлюзам нет до «🔁 Повторить».

Вне хода набор ведётся по выпуску набора: доступный шлюз не на версии
агента в выпуске получает команду сразу по `hello` — догон (ниже) или
следование (новее, Р17). Удержание прошлого хода доводится тем же путём:
фиксация — `upd_commit`, откат — `upd_revert`. Неудачный догон набор не
откатывает (§12.1, п. 18): повторы 30 мин / 2 ч / 6 ч ±40 %, затем строка ⚠️
и «🔁 Повторить».
"""
from __future__ import annotations

import json
import logging
import os
import random

from awgbot.core import config
from awgbot.domain.notices import fleet as notes_
from awgbot.infra import updates
from awgbot.util import timeutil

from .order import LOST_AFTER_S, FleetOut, _LOCK, _now_iso, _since, _upd_body
from .state import DONE, IN_FLIGHT, ROLLBACK_FAILED, ROLLED_BACK, ROLLING_BACK, RUNNING

log = logging.getLogger("awgbot.fleet")

RETRY_S = (30 * 60, 2 * 3600, 6 * 3600)
RETRY_JITTER = 0.4
RETRY_MAX = len(RETRY_S)
ROLLED_MARK = "update-rolled-back"      # метка отката сторожем (awg-bot.sh) — для прежнего процесса


def _due(iso: str) -> bool:
    if not iso:
        return True
    try:
        return timeutil.elapsed(timeutil.parse_iso(iso)) >= 0
    except ValueError:
        return True


class FleetRollbackMixin:
    """Откат набора и ведение вне хода."""

    # ── откат хода ───────────────────────────────────────────────────────────

    def _fleet_rollback_begin(self, run: dict, out: FleetOut, failed_on, reason: str) -> FleetOut:
        run["state"] = ROLLING_BACK
        run["failed_on"] = failed_on
        run["reason"] = " ".join(str(reason or "").split())[:300]
        self.fleet_run_set(run)
        out.live = True
        log.info("набор: ход до %s — откат (%s: %s)", run.get("tag"), failed_on, run["reason"])
        return self._fleet_backward(run, out)

    def _fleet_backward(self, run: dict, out: FleetOut) -> FleetOut:
        steps = run.get("steps") or []
        gws = self.fleet_gateways()
        online = {g["slot"]: g for g in gws}
        if self._fleet_expecting(run, gws):
            return out                                    # шлюзы прежнего процесса ещё переподключаются
        while int(run.get("undo") or 0) > 0:
            undo = int(run["undo"])
            step = steps[undo - 1]
            if step[0] == "gw":
                slot = int(step[1])
                st = self.fleet_slot(slot)
                g = online.get(slot)
                if st.get("state") == "reverting":
                    if g is not None and g["online"] and st.get("pending") == "upd_revert":
                        # команда не ушла (сессия упала на отправке) — снова новой сессией
                        out.send.append((slot, "upd_revert", {"id": st["id"], "to": st.get("prev") or run.get("prev")}))
                        return out
                    if g is not None and (g["online"] or _since(str(st.get("at") or "")) < LOST_AFTER_S):
                        return out                        # ждём его ответа (или переподключения — не дольше 15 минут)
                    st["state"] = "done"                  # пропал посреди отката: вернувшись, получит upd_revert
                    self.fleet_slot_set(slot, st)
                    run["undo"] = undo - 1
                    continue
                if g is None or not g["online"] or not st.get("hold"):
                    run["undo"] = undo - 1                # недоступный: доведётся по hello (вне хода)
                    continue
                st["state"], st["pending"] = "reverting", "upd_revert"
                st["at"] = _now_iso()
                self.fleet_slot_set(slot, st)
                self.fleet_run_set(run)
                out.send.append((slot, "upd_revert", {"id": st["id"], "to": st.get("prev") or run.get("prev")}))
                out.live = True
                return out
            # шаги сервера подряд — один откат: прежнее в удержании — самое первое
            j = undo
            while j > 0 and steps[j - 1][0] == "main":
                j -= 1
            run["undo"] = j
            self._fleet_expect(run, gws)
            self.fleet_run_set(run)
            out.server, out.live = "revert", True
            return out
        run["state"] = ROLLBACK_FAILED if run.get("revert_failed") else ROLLED_BACK
        run["at"] = _now_iso()
        self.fleet_run_set(run)
        out.live = out.final = True
        log.info("набор: откат хода до %s — %s", run.get("tag"), run["state"])
        return out

    def _fleet_revert_answered(self, run: dict, slot: int, state: str) -> None:
        steps = run.get("steps") or []
        undo = int(run.get("undo") or 0)
        if undo and steps[undo - 1][0] == "gw" and int(steps[undo - 1][1]) == slot:
            run["undo"] = undo - 1
        st = self.fleet_slot(slot)
        st["hold"] = False
        self.fleet_slot_set(slot, st)
        if state == "revert_failed" and slot not in run.setdefault("revert_failed", []):
            run["revert_failed"].append(slot)
        self.fleet_run_set(run)

    def fleet_server_revert_failed(self, reason: str) -> FleetOut:
        """Откатить сервер нечем (прежнего кода нет) — остальные откатываются дальше."""
        with _LOCK:
            run = self.fleet_run()
            if run.get("state") != ROLLING_BACK:
                return FleetOut()
            if "main" not in run.setdefault("revert_failed", []):
                run["revert_failed"].append("main")
            run["reason_main"] = " ".join(str(reason or "").split())[:300]
            self.fleet_run_set(run)
            return self._fleet_backward(run, FleetOut(live=True))

    def fleet_server_failed(self, reason: str) -> FleetOut:
        """Ступень сервера не запустилась (сеть GitHub, sha256, место) — сервер
        на прежнем: шаг снимается, откат того, что успели."""
        with _LOCK:
            run = self.fleet_run()
            if run.get("state") != RUNNING:
                return FleetOut()
            steps = run.get("steps") or []
            if steps and steps[-1][0] == "main":
                steps.pop()
            run["undo"] = len(steps)
            return self._fleet_rollback_begin(run, FleetOut(), "main", reason)

    # ── старт процесса: откат сервера сторожем ──────────────────────────────

    def fleet_on_start(self) -> FleetOut:
        """Сторож вернул прежнюю версию сервера (метка awg-bot.sh): Т-У8; в ходе
        — шаги сервера сняты, остальное откатывается."""
        with _LOCK:
            out = FleetOut()
            path = os.path.join(os.path.dirname(str(config.DB_PATH)), ROLLED_MARK)
            try:
                with open(path, encoding="utf-8") as f:
                    mark = json.loads(f.read() or "{}")
            except (OSError, ValueError):
                return out
            try:
                os.remove(path)
            except OSError:
                pass
            if not isinstance(mark, dict):
                return out
            ver, prev = str(mark.get("version") or ""), str(mark.get("prev") or config.INSTALLED_VERSION)
            from awgbot.domain.services import Notification
            run = self.fleet_run()
            tag = run.get("tag") if run.get("state") == RUNNING else ver
            out.notes.append(Notification(config.ADMIN_ID, notes_.server_rolled_back(tag or ver, prev),
                                          silent=True))
            if run.get("state") == RUNNING:
                steps = run.get("steps") or []
                while steps and steps[-1][0] == "main":
                    steps.pop()
                run["undo"] = len(steps)
                out.merge(self._fleet_rollback_begin(run, FleetOut(), "main", notes_.SERVER_SILENT))
            return out

    # ── «🔁 Повторить» ───────────────────────────────────────────────────────

    def fleet_retry(self) -> str:
        """Набор откатился или откат не прошёл — ход заново с того, что стоит;
        догоны, не прошедшие трижды, — заново. Пусто — начато; иначе причина."""
        with _LOCK:
            for gw in self.db.gateways():
                st = self.fleet_slot(gw.id)
                if not st.get("run") and st.get("state") in ("failed", "rolled_back", "refused"):
                    st["tries"], st["next_at"], st["final"] = 0, "", False
                    self.fleet_slot_set(gw.id, st)
            run = self.fleet_run()
            if run.get("state") not in (ROLLED_BACK, ROLLBACK_FAILED):
                return ""
            target = self.fleet_target()
            if target is None:
                return "nothing"
            run["state"] = ""                             # ход закрыт — новый с того, что стоит
            self.fleet_run_set(run)
            return self.fleet_start(target.tag)

    # ── вне хода: удержание прошлого хода, догон, следование ────────────────

    def fleet_hello(self, slot_id: int) -> FleetOut:
        """Шлюз вышел на связь: команда в пути, которую он не получил, — снова
        (тот же id, агент её узнает); дальше — такт."""
        with _LOCK:
            out = FleetOut()
            st = self.fleet_slot(slot_id)
            if st.get("state") == "sent":
                out.send.append((slot_id, "upd", _upd_body(st)))
            if st.get("state") == "done" and not st.get("run"):
                g = next((x for x in self.fleet_gateways() if x["slot"] == slot_id), None)
                if g is not None and updates.parse_version(g["version"]) != updates.parse_version(st.get("target") or ""):
                    st["state"] = ""                      # вышел на связь не той версией — решит ведение вне хода
                    self.fleet_slot_set(slot_id, st)
            if st.get("off_since"):
                st["off_since"] = ""
                self.fleet_slot_set(slot_id, st)
            run = self.fleet_run()
            if st.get("state") == "lost" and st.get("run") == run.get("id") and run.get("state") == RUNNING:
                g = next((x for x in self.fleet_gateways() if x["slot"] == slot_id), None)
                want = self.fleet_gw_target(run["tag"])
                if g is not None and want is not None and updates.parse_version(g["version"]) == want.version:
                    st["state"] = "done"                  # обновился и пропал — участник на выпуске хода
                    self.fleet_slot_set(slot_id, st)
                    run["steps"].append(["gw", int(slot_id), want.tag])
                    run["undo"] = len(run["steps"])
                    self.fleet_run_set(run)
                out.live = True
            return out.merge(self.fleet_tick())

    def _fleet_outside(self, run: dict, out: FleetOut) -> FleetOut:
        if run.get("state") == ROLLBACK_FAILED:
            return out                                    # до «🔁 Повторить» команд нет
        release = self.fleet_release()
        want = self.fleet_gw_target(release) if release else None
        for g in self.fleet_gateways():
            if not g["participant"]:
                continue
            slot = g["slot"]
            st = self.fleet_slot(slot)
            ver = updates.parse_version(g["version"])
            if st.get("hold") and st.get("run"):
                if st["run"] == run.get("id") and run.get("state") == DONE:
                    # фиксация — до сдачи в сокет; не ушла — снова на следующей сессии
                    out.send.append((slot, "upd_commit", {"id": st["id"]}))
                    st["pending"] = "upd_commit"
                    self.fleet_slot_set(slot, st)
                    continue
                prev = updates.parse_version(str(st.get("prev") or ""))
                if st["run"] == run.get("id") and run.get("state") == ROLLED_BACK and prev is not None \
                        and ver != prev and (st.get("state") != "reverting" or st.get("pending") == "upd_revert"):
                    st["state"], st["at"], st["pending"] = "reverting", _now_iso(), "upd_revert"
                    self.fleet_slot_set(slot, st)
                    out.send.append((slot, "upd_revert", {"id": st["id"], "to": st["prev"]}))
                    out.live = True
                    continue
                st["hold"] = False                        # прошлый ход ушёл — держать нечего
                self.fleet_slot_set(slot, st)
            if st.get("state") in IN_FLIGHT + ("reverting",):
                continue                                  # ждём ответа
            if want is None or ver == want.version:
                continue
            if st.get("state") == "done" and st.get("target") == want.tag:
                continue                                  # ответил «done» — версию скажет его hello
            if st.get("final") or not _due(str(st.get("next_at") or "")):
                continue
            same = not st.get("run") and st.get("target") == want.tag
            why = "follow" if ver is not None and ver > want.version else "catchup"
            out.send.append(self._fleet_command({}, g, want, why=why, hold=False,
                                                tries=int(st.get("tries") or 0) if same else 0))
            out.live = True
        return out

    def fleet_delivered(self, slot_id: int, what: str, body: dict) -> None:
        """Команда сдана в сокет (а не только собрана тактом): фиксация снимает
        удержание, откат больше не ждёт повтора. Не сдана — пометка остаётся,
        следующая сессия получит команду снова."""
        with _LOCK:
            st = self.fleet_slot(slot_id)
            if not st or str(body.get("id") or "") != st.get("id") or st.get("pending") != what:
                return
            st["pending"] = ""
            if what == "upd_commit":
                st["hold"] = False
            self.fleet_slot_set(slot_id, st)

    def _fleet_outside_ack(self, slot_id: int, st: dict, state: str) -> FleetOut:
        out = FleetOut()
        gw = self.db.gateway(slot_id)
        if gw is None:
            return out
        from awgbot.domain.services import Notification
        name, label = self._gw_name(gw), gw.label or ""
        user = getattr(self, "bot_username", "") or ""
        if state == "done":
            st.update(tries=0, next_at="", final=False)
            self.fleet_slot_set(slot_id, st)
            text = (notes_.gw_followed_down if st.get("why") == "follow" else notes_.gw_caught_up)(
                name, label, st["target"], slot=slot_id, bot_username=user)
            out.notes.append(Notification(config.ADMIN_ID, text, silent=True))
        elif state in ("failed", "rolled_back", "refused"):
            tries = int(st.get("tries") or 0) + 1
            st["tries"] = tries
            if tries >= RETRY_MAX:
                st["final"], st["next_at"] = True, ""
            else:
                delay = RETRY_S[tries - 1] * random.uniform(1 - RETRY_JITTER, 1 + RETRY_JITTER)
                st["next_at"] = timeutil.to_iso(timeutil.shift(timeutil.now(), int(delay)))
                out.notes.append(Notification(config.ADMIN_ID, notes_.gw_catchup_failed(
                    name, label, st["target"], st.get("error") or state, tries,
                    slot=slot_id, bot_username=user), silent=True))
            self.fleet_slot_set(slot_id, st)
        elif state in ("reverted", "revert_failed"):
            st["hold"] = False
            self.fleet_slot_set(slot_id, st)
        return out
