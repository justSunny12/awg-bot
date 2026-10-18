"""outside.py — часть пакета fleet (см. __init__): шлюзы вне хода — hello,
удержание прошлого хода, догон и следование, повторы.

Вне хода набор ведётся по выпуску набора: доступный шлюз не на версии
агента в выпуске получает команду — догон (пропустил ход, выпуск которого
стоит) или следование (сервер сменил версию сам: восстановление, ручной
`awg-bot update`, Р17). По одному, резервные первыми: пока шлюз на связи
посреди своей команды, новых не начинаем. Удержание прошлого хода доводится
тем же путём: фиксация — `upd_commit`, откат — `upd_revert`; удержание хода,
который сменился другим, — `upd_commit` по id той команды (агент снимет
прежнее). Неудачный догон набор не откатывает (§12.1, п. 18): повторы
30 мин / 2 ч / 6 ч ±40 %, после третьего повтора — строка ⚠️ и «🔁 Повторить».

Ответ шлюза, вернувшегося посреди своей команды, — только его `upd_ack`:
сервер не выводит «done» из версии в hello; команду, которую шлюз мог не
получить («sent»), — повторяет тем же id (агент ответит своим фактом).
"""
from __future__ import annotations

import logging
import random

from awgbot.core import config
from awgbot.domain.notices import fleet as notes_
from awgbot.infra import updates
from awgbot.util import timeutil

from .order import GW_STEP_MAX_S, FleetOut, _LOCK, _now_iso, _since, _upd_body
from .state import ACTIVE_RUN, DONE, IN_FLIGHT, ROLLBACK_FAILED, ROLLED_BACK, RELEASE_KEY

log = logging.getLogger("awgbot.fleet")

RETRY_S = (30 * 60, 2 * 3600, 6 * 3600)
RETRY_JITTER = 0.4
RETRY_MAX = len(RETRY_S)          # повторов; неудача после последнего — строка ⚠️ и «🔁 Повторить»


def _due(iso: str) -> bool:
    if not iso:
        return True
    try:
        return timeutil.elapsed(timeutil.parse_iso(iso)) >= 0
    except ValueError:
        return True


class FleetOutsideMixin:
    """Шлюзы вне хода."""

    def fleet_hello(self, slot_id: int) -> FleetOut:
        """Шлюз вышел на связь: команда, которую он мог не получить, — снова
        тем же id; вне своего хода посреди чужого — не повторяется (ход сам
        даст ему команду); пропавший посреди шага ждёт своего ответа."""
        with _LOCK:
            out = FleetOut()
            st = self.fleet_slot(slot_id)
            run = self.fleet_run()
            active = run.get("state") in ACTIVE_RUN
            mine = active and bool(st.get("run")) and st.get("run") == run.get("id")
            changed = False
            if st.get("state") == "lost":
                # вернулся: его ответ — только upd_ack (отложенный уйдёт первой же сессией)
                st["state"] = st.pop("lost_from", "") or "accepted"
                st["at"], changed = _now_iso(), True
                out.live = out.live or mine
            if st.get("state") == "sent":
                if active and not mine:
                    st["state"], changed = "", True       # догон, не начатый до хода, — ход сделает сам
                else:
                    out.send.append((slot_id, "upd", _upd_body(st)))
            if st.get("state") == "done" and not st.get("run"):
                g = next((x for x in self.fleet_gateways() if x["slot"] == slot_id), None)
                if g is not None and updates.parse_version(g["version"]) != updates.parse_version(st.get("target") or ""):
                    st["state"], changed = "", True       # вышел на связь не той версией — решит ведение вне хода
            if st.get("off_since"):
                st["off_since"], changed = "", True
            if changed:
                self.fleet_slot_set(slot_id, st)
            return out.merge(self.fleet_tick())

    def fleet_needs_tick(self) -> bool:
        """Нужен ли такт по расписанию: идёт ход, или у какого-то шлюза
        команда в пути, удержание, отложенная отправка, срок повтора. Иначе
        вне хода всё решают hello и ответы шлюзов — такт без дела не нужен."""
        run = self.fleet_run()
        if run.get("state") in ACTIVE_RUN:
            return True
        for gw in self.db.gateways():
            st = self.fleet_slot(gw.id)
            if not st:
                continue
            if st.get("hold") or st.get("pending") or st.get("state") in IN_FLIGHT + ("reverting", "lost"):
                return True
            if st.get("next_at") and not st.get("final") and _due(str(st["next_at"])):
                return True
        return False

    def _fleet_outside(self, run: dict, out: FleetOut) -> FleetOut:
        if run.get("state") == ROLLBACK_FAILED:
            return out                                    # до «🔁 Повторить» команд нет
        release = self.fleet_release()
        want = self.fleet_gw_target(release) if release else None
        # догнать пропущенный ход — «catchup»; сервер сменил версию сам — «follow»
        missed = run.get("state") == DONE and str(run.get("tag") or "") == release
        gws = self.fleet_gateways()
        busy = False
        for g in gws:
            st = self.fleet_slot(g["slot"])
            if g["participant"] and g["online"] and st.get("state") in IN_FLIGHT:
                if _since(str(st.get("at") or "")) >= GW_STEP_MAX_S:
                    # ответа нет полчаса — команда не дошла или агент её потерял
                    st["error"] = notes_.GW_NO_ANSWER
                    out.merge(self._fleet_outside_ack(g["slot"], st, "failed"))
                    continue
                busy = True
        for g in gws:
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
                if st["run"] != run.get("id"):
                    # ход сменился другим — прежнее у шлюза держать незачем: его
                    # команде — фиксация (агент снимет удержание по её id)
                    out.send.append((slot, "upd_commit", {"id": st["id"]}))
                    st["pending"] = "upd_commit"
                    self.fleet_slot_set(slot, st)
                    continue
                if st.get("state") in IN_FLIGHT + ("reverting", "lost"):
                    continue                              # ход его ещё ждёт
                st["hold"] = False                        # откат этого хода сюда не дошёл — держать нечего
                self.fleet_slot_set(slot, st)
            if st.get("state") in IN_FLIGHT + ("reverting", "lost"):
                continue                                  # ждём ответа
            if want is None or ver == want.version:
                continue
            if st.get("state") == "done" and st.get("target") == want.tag:
                continue                                  # ответил «done» — версию скажет его hello
            if st.get("final") or not _due(str(st.get("next_at") or "")) or busy:
                continue
            same = not st.get("run") and st.get("target") == want.tag
            why = "catchup" if missed and ver is not None and ver < want.version else "follow"
            out.send.append(self._fleet_command({}, g, want, why=why, hold=False,
                                                tries=int(st.get("tries") or 0) if same else 0))
            busy = True
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
            if st.get("why") == "catchup":
                text = notes_.gw_caught_up(name, label, st["target"], slot=slot_id, bot_username=user)
            else:
                up = (updates.parse_version(st.get("target") or "") or ()) > (updates.parse_version(st.get("prev") or "") or ())
                text = (notes_.gw_followed_up if up else notes_.gw_followed_down)(
                    name, label, st["target"], slot=slot_id, bot_username=user)
            out.notes.append(Notification(config.ADMIN_ID, text, silent=True))
        elif state in ("failed", "rolled_back", "refused"):
            tries = int(st.get("tries") or 0) + 1
            st["tries"], st["state"] = tries, state
            reason = st.get("error") or state
            if tries > RETRY_MAX:
                st["final"], st["next_at"] = True, ""
                text = notes_.gw_catchup_given_up(name, label, st["target"], reason, slot=slot_id, bot_username=user)
            else:
                delay = RETRY_S[tries - 1] * random.uniform(1 - RETRY_JITTER, 1 + RETRY_JITTER)
                st["next_at"] = timeutil.to_iso(timeutil.shift(timeutil.now(), int(delay)))
                text = notes_.gw_catchup_failed(name, label, st["target"], reason, tries, slot=slot_id,
                                                bot_username=user)
            out.notes.append(Notification(config.ADMIN_ID, text, silent=True))
            self.fleet_slot_set(slot_id, st)
        elif state in ("reverted", "revert_failed"):
            st["hold"] = False
            self.fleet_slot_set(slot_id, st)
        return out

    def fleet_release_set(self, tag: str) -> None:
        """Выпуск набора поверх версии сервера (выпуск только для шлюзов) —
        пишет только фиксация хода; восстановление снимает."""
        self.db.set_state(RELEASE_KEY, tag or "")
