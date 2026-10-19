"""upd.py — команда набора обновлений на агенте шлюза: проверки, ступень,
ход и ответ серверу (`upd_ack`).

Сервер называет только тег; всё остальное агент проверяет сам:
сборка из выпуска, не ниже COMPAT_FLOOR, выпуск есть на GitHub и не за
барьером пропущенных, digest ассета совпал с названным сервером, то же
поколение AmneziaWG; ступень вверх — тем же выбором цели роли с условием
`needs_main` по версии сервера из `role`, вниз — сразу на цель.
Неудача проверки — refused, сети или скачивания — failed, обновлённый агент
не вышел на связь (метка отката сторожа) — rolled_back.

Команда и её ход лежат в базе агента (`gw_upd`) и переживают перезапуск:
новый процесс по первому слову сервера доводит её — `done`, следующая
ступень той же команды или `rolled_back`. Тот же процесс (сессия
переподключилась, пока апдейтер скачивает и собирает) ничего не решает,
пока апдейтер жив; умер, не доведя, или 20 минут — `failed`. Ответ, не ушедший по каналу, ждёт
следующей сессии (`gw_upd_ack`). Прежние код и окружение при `hold`
держатся до `upd_commit` / `upd_revert` (awg-bot.sh).
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess

from awgbot.core import config
from awgbot.domain.selfupdate.marks import install_busy
from awgbot.infra import updates
from awgbot.util import timeutil

log = logging.getLogger("awgbot.gateway")

CMD_KEY = "gw_upd"
ACK_KEY = "gw_upd_ack"
BUSY_WAIT_S = 600             # занят (смена ключей в переходе) — ждать факта не дольше
ROLLED_MARK = "update-rolled-back"
GUARD_FILE = "update-guard"
ACTIVE = ("checking", "accepted", "applying", "reverting")
SPAWN_GRACE_S = 90            # апдейтер успевает взять замок установки и записать pid
APPLY_MAX_S = 20 * 60         # ступень (с ядром) или понижение дольше — не удались
FAILED = ("failed", "rolled_back", "refused")

# ключи прежних «⬆️ Обновлений» агента: с 3.4.0 агент сам не обновляется
LEGACY_KEYS = ("update_available_tag", "update_notified_tag", "update_waiting", "update_releases",
               "update_meta", "updates_muted")

_ID_RE = re.compile(r"[0-9a-f]{8}\Z")
_TAG_RE = re.compile(r"v?\d+\.\d+\.\d+(\.\d+)?\Z")


def _v(ver: str) -> str:
    ver = str(ver or "")
    return ver if not ver or ver.startswith("v") else f"v{ver}"


def _data(name: str) -> str:
    return os.path.join(os.path.dirname(str(config.DB_PATH)), name)


class GatewayUpdMixin:
    """Команда обновления с сервера AWG."""

    # ── состояние ────────────────────────────────────────────────────────────

    def upd_cmd(self) -> dict:
        data = self.db.get_state_json(CMD_KEY, {})
        return data if isinstance(data, dict) else {}

    def _upd_save(self, cmd: dict) -> None:
        self.db.set_state(CMD_KEY, json.dumps(cmd, ensure_ascii=False) if cmd else "")

    def upd_ack_pending(self) -> dict:
        data = self.db.get_state_json(ACK_KEY, {})
        return data if isinstance(data, dict) else {}

    def upd_ack_clear(self) -> None:
        self.db.set_state(ACK_KEY, "")

    def _upd_state(self, state: str, *, error: str = "", **extra) -> dict:
        """Записать итог шага и ответ серверу (уйдёт первой же сессией)."""
        cmd = self.upd_cmd()
        cmd.update(extra, state=state, error=" ".join(str(error or "").split())[:300],
                   at=timeutil.to_iso(timeutil.now()))
        self._upd_save(cmd)
        ack = {"id": cmd.get("id", ""), "state": state, "step": cmd.get("step") or "",
               "version": config.INSTALLED_VERSION, "error": cmd["error"]}
        self.db.set_state(ACK_KEY, json.dumps(ack, ensure_ascii=False))
        return ack

    def upd_legacy_drop(self) -> None:
        """Первый старт 3.4.0: ключи прежних «⬆️ Обновлений» агента прочь."""
        for key in LEGACY_KEYS:
            if self.db.get_state(key):
                self.db.set_state(key, "")

    # ── приём команды ───────────────────────────────────────────────────────

    def upd_in(self, msg: dict) -> str:
        """`upd` с сервера: «new» — принята к проверке; «same» — та же команда
        (сервер повторил после переподключения: ответ — текущий); «busy» —
        идёт другая команда (ответ refused записан); «bad» — мусор."""
        rid = str(msg.get("id") or "")
        target = str(msg.get("target") or "")
        if not _ID_RE.match(rid) or not _TAG_RE.match(target):
            return "bad"
        cur = self.upd_cmd()
        if cur.get("id") == rid:
            return "same"
        if cur.get("state") in ACTIVE:
            # своя команда идёт дальше; новой — отказ
            ack = {"id": rid, "state": "refused", "step": "", "version": config.INSTALLED_VERSION,
                   "error": f"занят: идёт обновление до {_v(cur.get('target'))}"}
            self.db.set_state(ACK_KEY, json.dumps(ack, ensure_ascii=False))
            return "busy"
        self._upd_save({"id": rid, "target": _v(target), "digest": str(msg.get("digest") or "")[:80],
                        "why": str(msg.get("why") or "fleet")[:16], "hold": bool(msg.get("hold")),
                        "prev": _v(config.INSTALLED_VERSION), "step": "", "state": "checking",
                        "error": "", "at": timeutil.to_iso(timeutil.now())})
        self.upd_ack_clear()
        return "new"

    def upd_current_ack(self) -> dict:
        """Ответ на повтор той же команды: отложенный или по текущему состоянию."""
        pending = self.upd_ack_pending()
        if pending:
            return pending
        cmd = self.upd_cmd()
        state = cmd.get("state") or ""
        state = "accepted" if state in ("checking", "applying") else state
        return {"id": cmd.get("id", ""), "state": state, "step": cmd.get("step") or "",
                "version": config.INSTALLED_VERSION, "error": cmd.get("error") or ""}

    def upd_busy(self) -> str:
        """Чем агент занят так, что обновляться сейчас нельзя, или пусто."""
        try:
            st = self.rekey_state() if callable(getattr(self, "rekey_state", None)) else {}
        except Exception:                                 # noqa: BLE001
            st = {}
        if st.get("state") in ("ready", "switched", "back"):
            return "смена ключей линка в переходе"
        return ""

    # ── проверки и ступень ──────────────────────────────────────────────────

    def upd_plan(self, target: str = "", digest=None):
        """(ступень, исход, причина): исход «step» — ставить ступень; «done» —
        уже на цели; «refused» / «failed» — с причиной. Ходит на GitHub.
        target/digest — не из команды (понижение при откате набора)."""
        from awgbot.core.compat import COMPAT_FLOOR, below_floor
        from awgbot.infra import awglock
        cmd = self.upd_cmd()
        target = target or str(cmd.get("target") or "")
        inst = updates.parse_version(config.INSTALLED_VERSION)
        want = updates.parse_version(target)
        if inst is None or want is None:
            return None, "refused", "версия не читается"
        if below_floor(target):
            return None, "refused", f"{_v(target)} ниже версии совместимости {_v(COMPAT_FLOOR)}"
        if want == inst:
            return None, "done", ""
        try:
            releases = updates.list_releases()
        except updates.UpdateError as e:
            return None, "failed", f"GitHub не ответил: {e}"
        if not updates._is_release_build(inst, releases):
            return None, "refused", f"агент — сборка не из выпуска ({_v(config.INSTALLED_VERSION)})"
        rel = next((r for r in releases if r.version == want), None)
        if rel is None:
            return None, "refused", f"выпуска {_v(target)} нет на GitHub"
        if not rel.usable():
            return None, "refused", f"метаданные {rel.tag} не прочитаны"
        barrier = next((r for r in releases if inst < r.version < want and not r.usable()), None)
        if barrier is not None and want > inst:
            return None, "refused", f"{rel.tag} за пропущенным выпуском {barrier.tag}"
        sent = str(cmd.get("digest") or "") if digest is None else digest
        if sent and (not rel.sha256 or sent != f"sha256:{rel.sha256}"):
            return None, "refused", f"digest {rel.tag} у GitHub не совпал с названным сервером AWG"
        gen, have = rel.awg_generation(), awglock.generation()
        if gen and have and gen != have:
            return None, "refused", f"{rel.tag} несёт AmneziaWG поколения {gen}, у шлюза — {have}"
        if want < inst:
            return rel, "step", ""                        # понижение — сразу на цель
        from awgbot.domain.selfupdate import UpdatePeer
        # цель снята (сервер на ней) — шлюзу она годится; прочие снятые ступенями не станут
        import dataclasses
        capped = [dataclasses.replace(r, withdrawn="", prerelease=False) if r.version == want else r
                  for r in releases if r.version <= want]
        peer = self._update_gate([UpdatePeer(slot=0, name="", version=self.server_version(), fresh=True)])
        step = updates.pick_target(inst, capped, "gateway", None, peer)
        if step is None:
            if updates.pick_target(inst, capped, "gateway") is not None:
                return None, "refused", f"{rel.tag} нужен сервер AWG новее (сейчас {_v(self.server_version())})"
            return None, "refused", f"ступени до {rel.tag} для шлюза нет"
        return step, "step", ""

    def upd_accept(self, step) -> dict:
        return self._upd_state("accepted", step=step.tag)

    def upd_apply(self, step) -> None:
        """Скачать и поставить ступень (апдейтер вне cgroup); отказ — UpdateError.
        Понижение при откате набора — без удержания, состояние «reverting»."""
        cmd = self.upd_cmd()
        reverting = cmd.get("state") == "reverting"
        if not reverting:
            cmd.update(state="applying", step=step.tag)
        cmd.update(proc=_PROC, applied_at=timeutil.to_iso(timeutil.now()))
        self._upd_save(cmd)
        hold = str(cmd.get("id") or "1") if cmd.get("hold") and not reverting else ""
        self.apply_update(step, hold=hold)

    # ── новый процесс: довести команду ──────────────────────────────────────

    def upd_settled(self) -> bool:
        """Сторож обновления снят (новый процесс отметился): ответ «done»
        только после этого — иначе фиксация набора опередила бы удержание."""
        return not os.path.exists(_data(GUARD_FILE))

    def upd_resume(self) -> str:
        """Первое слово сервера в новом процессе: что с командой. «done» /
        «rolled_back» / «reverted» / «revert_failed» — ответ записан; «next» —
        следующая ступень той же команды (проверки заново); «check» — проверки
        не закончены; пусто — делать нечего."""
        cmd = self.upd_cmd()
        state = cmd.get("state") or ""
        if state in ("applying", "reverting") and cmd.get("proc") == _PROC:
            # тот же процесс — апдейтер ещё не перезапустил агента
            age = _age(str(cmd.get("applied_at") or ""))
            if age < APPLY_MAX_S and (install_busy() or age < SPAWN_GRACE_S):
                return ""
            if state == "applying":
                self._upd_state("failed", error="обновление не применилось — агент на прежней версии")
                return "failed"
            if cmd.get("revert_local"):
                self._upd_state("revert_failed", error="прежняя версия не вернулась")
                return "revert_failed"
        rolled = self._upd_rolled_mark()
        inst = updates.parse_version(config.INSTALLED_VERSION)
        if state == "applying":
            step = updates.parse_version(str(cmd.get("step") or ""))
            if inst is not None and inst == step and not rolled:
                if step == updates.parse_version(str(cmd.get("target") or "")):
                    self._upd_state("done")
                    return "done"
                cmd["state"] = "checking"
                self._upd_save(cmd)
                return "next"
            from awgbot.domain.notices.fleet import AGENT_SILENT
            self._upd_state("rolled_back", error=AGENT_SILENT if rolled else "обновление не применилось")
            return "rolled_back"
        if state == "reverting":
            to = updates.parse_version(str(cmd.get("revert_to") or ""))
            if inst is not None and inst == to:
                self._upd_state("reverted")
                return "reverted"
            if cmd.get("revert_local"):
                self._upd_state("revert_failed", error="прежняя версия не вернулась")
                return "revert_failed"
            return "check"                                # понижение с GitHub не закончено
        if state in ("checking", "accepted"):
            return "check"
        return ""

    def _upd_rolled_mark(self) -> dict:
        path = _data(ROLLED_MARK)
        try:
            with open(path, encoding="utf-8") as f:
                mark = json.loads(f.read() or "{}")
        except (OSError, ValueError):
            return {}
        try:
            os.remove(path)
        except OSError:
            pass
        return mark if isinstance(mark, dict) else {}

    # ── фиксация и откат набора ─────────────────────────────────────────────

    def upd_commit(self, msg: dict) -> bool:
        """`upd_commit`: набор зафиксирован — удержанное прежнее прочь."""
        cmd = self.upd_cmd()
        if str(msg.get("id") or "") != cmd.get("id"):
            return False
        cmd["committed"] = True
        self._upd_save(cmd)
        from awgbot.infra.detach import spawn_detached
        spawn_detached([updates._AWG_BOT_BIN, "__update_commit"], unit_prefix="awg-bot-update-commit")
        return True

    def upd_revert(self, msg: dict) -> str:
        """`upd_revert {to}`: прежнее в удержании — «local» (откат на месте, без
        сети); нет — «remote» (понижение с GitHub на to, проверки заново);
        «bad» — чужая команда или цель не читается."""
        cmd = self.upd_cmd()
        to = str(msg.get("to") or "")
        if str(msg.get("id") or "") != cmd.get("id") or not _TAG_RE.match(to):
            return "bad"
        if cmd.get("state") in ("reverting", "reverted", "revert_failed") and cmd.get("revert_to") == _v(to):
            return "same"                                 # повтор той же команды отката — второго не запускать
        if updates.parse_version(to) == updates.parse_version(config.INSTALLED_VERSION):
            self._upd_save({**cmd, "revert_to": _v(to)})
            self._upd_state("reverted")
            return "done"
        local = _revert_check() == 0
        self._upd_save({**cmd, "state": "reverting", "revert_to": _v(to), "revert_local": local,
                        "target": cmd.get("target"), "step": ""})
        if local:
            from awgbot.infra.detach import spawn_detached
            spawn_detached([updates._AWG_BOT_BIN, "__update_revert"], unit_prefix="awg-bot-update-revert")
            return "local"
        return "remote"

    def upd_revert_plan(self):
        """Понижение с GitHub на revert_to: проверки как у команды, цель — прежняя."""
        return self.upd_plan(target=str(self.upd_cmd().get("revert_to") or ""), digest="")

    # ── строки панели и итог после перезапуска ─────────────────────────────

    def upd_panel(self) -> dict:
        """Для панели: {"state": "updating"|"failed", "target", "prev", "error"} или пусто."""
        cmd = self.upd_cmd()
        state = cmd.get("state") or ""
        if state in ("checking", "accepted", "applying"):
            return {"state": "updating", "target": cmd.get("target") or ""}
        if state in FAILED:
            return {"state": "failed", "target": cmd.get("target") or "", "prev": cmd.get("prev") or "",
                    "error": cmd.get("error") or "", "how": state}
        return {}

    def update_applied_note(self, pending: str, ok: bool):
        """Итог своего обновления после перезапуска (selfupdate/core): по команде
        сервера — одна строка без звука на цели, промежуточная ступень и
        неудача — молча (неудачу скажет upd_resume); иначе — обычный итог
        (DEFAULT_NOTE)."""
        from awgbot.domain.selfupdate.core import DEFAULT_NOTE
        cmd = self.upd_cmd()
        if cmd.get("state") not in ("applying", "reverting"):
            return DEFAULT_NOTE
        if ok and cmd.get("state") == "applying" and _v(pending) == _v(cmd.get("target")):
            from awgbot.domain.notices.fleet import agent_updated
            from awgbot.domain.selfupdate import _notification
            return _notification(config.ADMIN_ID, agent_updated(pending), silent=True)
        return None


_PROC = secrets.token_hex(4)     # этот процесс агента: его апдейтер ещё не мог перезапустить


def _age(iso: str) -> float:
    try:
        return timeutil.elapsed(timeutil.parse_iso(iso))
    except ValueError:
        return float(APPLY_MAX_S)


def _revert_check() -> int:
    """`awg-bot __update_revert --check`: 0 — прежнее на месте, 2 — нет."""
    try:
        return subprocess.run([updates._AWG_BOT_BIN, "__update_revert", "--check"],
                              capture_output=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return 2
