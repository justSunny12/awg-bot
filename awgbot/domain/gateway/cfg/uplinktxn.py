"""uplinktxn.py — транзакция конфига аплинка: замок обвязки, журнал, ключи признания, --uplink, проба, итог; восстановление после перезапуска; аварийное правило на тике."""
from __future__ import annotations

import contextlib
import json
import os
import time

from awgbot.util import wgconf

from .. import base
from ..base import log

UPLINK_PROBE_SECONDS = 60       # после нового конфига — ждём свежий хендшейк аплинка столько
LOCK_WAIT_HUMAN = 20            # с — «Применить» ждёт замка обвязки не дольше, дальше «занято»
UPLINK_FRESH_SECONDS = 60
EMERGENCY_RETRY_SECONDS = 600   # аварийная попытка не удалась — следующая не раньше


FRESH_UPLINK_IF = "awg0"         # первый аплинк шлюза, подключённого командой (как UPLINK_IF скрипта обвязки)

class CfgUplinkTxnMixin:
    """Новый конфиг аплинка — одной транзакцией под замком обвязки (тот же
    flock, что у скрипта): прогон юнита на загрузке иначе прочитал бы юнит
    до неё и записал после со старыми ключами признания. Шаги: журнал
    (`/etc/awg-gw/uplink.txn` — прежний текст юнита), ключи признания в юнит,
    конфиг режимом --uplink (копия прежнего — <аплинк>.conf.before), проба —
    свежий хендшейк за 60 с; вышло — --uplink-commit, нет — --uplink-revert
    (прежний байт в байт) и прежний юнит. Журнал есть, а транзакции нет
    (агент перезапустился посреди) — тот же откат на ближайшем тике."""

    def _cfg_uplink_paths(self) -> tuple[str, str]:
        from awgbot.infra import gwguard
        return f"{gwguard.GW_ETC}/uplink.next", f"{gwguard.GW_ETC}/uplink.txn"

    def _cfg_uplink_tidy(self) -> None:
        """Файлы транзакции (в uplink.next и uplink.render — ключ устройства)
        после итога не нужны; uplink.render скрипт убирает сам, но не после
        SIGKILL по таймауту."""
        from awgbot.infra import gwguard
        for path in (*self._cfg_uplink_paths(), f"{gwguard.GW_ETC}/uplink.render"):
            try:
                os.unlink(path)
            except OSError:
                pass

    @contextlib.contextmanager
    def _cfg_uplink_locks(self, flock_wait: float):
        """Оба замка: агентский (_APPLY_LOCK) — без ожидания: его держит
        транзакция env+link до четверти часа, и человек не должен смотреть на
        «⏳ Применяю…» всё это время; замок обвязки — flock_wait секунд (0 —
        одна попытка). Не взяли — _Busy."""
        from awgbot.infra import gwguard
        if not base._APPLY_LOCK.acquire(blocking=False):
            raise _Busy("агент применяет другую конфигурацию шлюза")
        try:
            try:
                with gwguard.apply_lock(flock_wait):
                    yield
            except gwguard.GwGuardError as e:
                if "занята" in str(e):
                    raise _Busy(str(e)) from None
                raise
        finally:
            base._APPLY_LOCK.release()

    # ── ключи признания ──────────────────────────────────────────────────────

    def cfg_uplink_keys_only(self, value: dict) -> dict:
        """Только ключи признания — строки юнита; стоящий конфиг аплинка ими
        признаётся, переподнимать нечего. Под замком обвязки: прогон юнита не
        перепишет их старыми. Обвязка занята — busy (сервер повторит)."""
        from awgbot.infra import gwguard
        want = {"GATEWAY_PUBKEY": value.get("pubkey", ""), "GATEWAY_PREV_PUBKEY": value.get("prev_pubkey", "")}
        if self.cfg_uplink_keys() == (want["GATEWAY_PUBKEY"], want["GATEWAY_PREV_PUBKEY"]):
            return {"ok": True, "error": ""}
        try:
            with self._cfg_uplink_locks(LOCK_WAIT_HUMAN):
                self._cfg_uplink_recover_locked()
                gwguard.unit_set_env(want)
        except _Busy as e:
            return {"ok": False, "error": f"юнит не переписан ({e})", "busy": True}
        except (OSError, gwguard.GwGuardError) as e:
            return {"ok": False, "error": f"юнит не переписан ({e})"}
        return {"ok": True, "error": ""}

    # ── транзакция ───────────────────────────────────────────────────────────

    def cfg_uplink_apply(self, value: dict, flock_wait: float | None = None, fresh: bool = False) -> dict:
        """Новый конфиг аплинка одной транзакцией. Возвращает {"ok", "error",
        "state"}: applied | refused (скрипт не принял — ничего не тронуто) |
        reverted (не поднялся — прежний на месте) | revert_failed (не поднялся,
        и прежний не вернулся) | busy (агент или обвязка заняты — ничего не
        тронуто). flock_wait — сколько ждать замка обвязки (тик — 0). fresh —
        первый аплинк шлюза, подключённого командой: интерфейса ещё нет —
        имя по умолчанию скрипта обвязки (awg0)."""
        iface = self.cfg_uplink_if() or (FRESH_UPLINK_IF if fresh else "")
        if not iface:
            return {"ok": False, "error": "аплинк этого устройства не найден", "state": "refused"}
        nxt, txn = self._cfg_uplink_paths()
        try:
            with self._cfg_uplink_locks(LOCK_WAIT_HUMAN if flock_wait is None else flock_wait):
                self._cfg_uplink_recover_locked()
                return self._cfg_uplink_txn(value, iface, nxt, txn)
        except _Busy as e:
            return {"ok": False, "error": str(e), "state": "busy"}

    def _cfg_uplink_txn(self, value: dict, iface: str, nxt: str, txn: str) -> dict:
        from awgbot.infra import gwguard
        try:
            unit_before = gwguard.unit_path().read_text(encoding="utf-8")
            conf = wgconf.parse(value.get("conf", ""))
            # журнал — до первого файла и первого изменения: перезапуск агента
            # дальше этой строки откатит всё и уберёт файлы с ключом
            self._cfg_write_private(txn, json.dumps({"iface": iface, "unit": unit_before, "at": time.time()},
                                                    ensure_ascii=False))
            self._cfg_write_private(nxt, wgconf.render(conf, "uplink"))
            gwguard.unit_set_env({"GATEWAY_PUBKEY": value.get("pubkey", ""),
                                  "GATEWAY_PREV_PUBKEY": value.get("prev_pubkey", "")})
        except (OSError, wgconf.WgConfError, gwguard.GwGuardError) as e:
            self._cfg_uplink_back(iface, txn, finish=False)
            return {"ok": False, "error": f"конфиг аплинка не записан ({e})", "state": "refused"}
        ok, out = gwguard.uplink_apply(nxt, iface)
        if not ok:
            reason = _reason(out)
            # отказ до установки скрипт называет итогом «uplink: refused» — на диске ничего
            refused = gwguard.uplink_applied_how(out) == "refused"
            back_ok = self._cfg_uplink_back(iface, txn, finish=not refused)
            log.warning("gateway: конфиг аплинка с сервера не применился: %s", reason)
            return {"ok": False, "error": reason, "back_why": self.__dict__.get("_uplink_back_why", ""),
                    "state": "refused" if refused else ("reverted" if back_ok else "revert_failed")}
        if gwguard.uplink_applied_how(out) == "installed" and not self._cfg_uplink_probe(iface):
            back_ok = self._cfg_uplink_back(iface, txn, finish=True)
            return {"ok": False, "error": f"аплинк не поднялся за {UPLINK_PROBE_SECONDS} с",
                    "back_why": self.__dict__.get("_uplink_back_why", ""),
                    "state": "reverted" if back_ok else "revert_failed"}
        # сначала журнал прочь, потом копию: сбой между ними оставит лишь
        # .before (её уберёт следующий --uplink), а не откат юнита при новом конфиге
        self._cfg_uplink_tidy()
        done, out = gwguard.uplink_finish(iface, commit=True)
        if not done:
            log.warning("gateway: копия прежнего аплинка не убрана: %s", _reason(out))
        self.__dict__["_uplink_dead_since"] = None
        self.invalidate_static()
        return {"ok": True, "error": "", "state": "applied"}

    def _cfg_uplink_back(self, iface: str, txn: str, finish: bool) -> bool:
        """Откат под замками: прежний конфиг (если скрипт успел поставить
        новый), затем прежний юнит из журнала, файлы транзакции прочь. True —
        прежнее на месте."""
        from awgbot.infra import gwguard
        ok = True
        self.__dict__["_uplink_back_why"] = ""
        if finish and iface:
            back_ok, out = gwguard.uplink_finish(iface, commit=False)
            if not back_ok:
                ok = False
                # настоящая причина — в итог для человека, а не только в журнал
                self.__dict__["_uplink_back_why"] = _reason(out)
                log.warning("gateway: прежний конфиг аплинка не вернулся: %s", _reason(out))
        try:
            with open(txn, encoding="utf-8") as f:
                unit_before = json.load(f).get("unit")
        except (OSError, ValueError):
            unit_before = None
        if unit_before:
            try:
                gwguard.unit_restore(unit_before)
            except (OSError, gwguard.GwGuardError) as e:
                ok = False
                self.__dict__["_uplink_back_why"] = self.__dict__.get("_uplink_back_why") or f"обвязка не переписана ({e})"
                log.warning("gateway: обвязка после отказа аплинка не вернулась: %s", e)
        self._cfg_uplink_tidy()
        return ok

    def _cfg_uplink_probe(self, iface: str) -> bool:
        from awgbot.infra import gwguard
        deadline = time.monotonic() + UPLINK_PROBE_SECONDS
        while time.monotonic() < deadline:
            state, age = gwguard.handshake_state(iface)
            if state == "ok" and age is not None and age <= UPLINK_FRESH_SECONDS:
                return True
            time.sleep(3)
        return False

    # ── тик: восстановление и аварийное правило ──────────────────────────────

    def _cfg_uplink_recover_locked(self) -> str:
        """Под замками: журнал есть — транзакция прервана перезапуском; прежний
        конфиг и прежний юнит назад. Текст уведомления или пусто."""
        nxt, txn = self._cfg_uplink_paths()
        if not os.path.exists(txn):
            return ""
        try:
            with open(txn, encoding="utf-8") as f:
                iface = str(json.load(f).get("iface") or "")
        except (OSError, ValueError):
            iface = ""
        ok = self._cfg_uplink_back(iface or self.cfg_uplink_if(), txn, finish=True)
        log.warning("gateway: применение конфига аплинка прервано перезапуском — прежний %s",
                    "на месте" if ok else "не вернулся")
        return ("⚠️ Применение конфига аплинка прервал перезапуск агента — вернул прежний" if ok else
                "⚠️ Применение конфига аплинка прервал перезапуск агента, и прежний не вернулся")

    def cfg_uplink_recover(self) -> str:
        """Журнал транзакции есть, а замки свободны — агент перезапустился
        посреди: откат. Вопрос (если был) остаётся — человек ответит снова.
        Замки заняты — не сейчас (транзакция идёт или обвязка занята)."""
        from awgbot.infra import gwguard
        nxt, txn = self._cfg_uplink_paths()
        if not os.path.exists(txn):
            return ""
        try:
            with self._cfg_uplink_locks(0):
                return self._cfg_uplink_recover_locked()
        except (_Busy, gwguard.GwGuardError) as e:
            log.info("gateway: откат прерванного применения аплинка отложен: %s", e)
            return ""

    def _cfg_unit_mark(self) -> None:
        """Строка AWG_GW_UNIT_RUN=1 в юните обвязки — по ней прогон юнита после
        замка сверяет свои строки с диском. Юнит старого образца получает её
        сразу, не дожидаясь первого полного прогона после обновления агента."""
        from awgbot.infra import gwguard
        text = gwguard.unit_text()
        if not text or "AWG_GW_UNIT_RUN=" in text:
            return
        try:
            with self._cfg_uplink_locks(0):
                gwguard.unit_set_env({"AWG_GW_UNIT_RUN": "1"})
        except (_Busy, OSError, gwguard.GwGuardError) as e:
            log.info("gateway: метка прогона юнита не поставлена: %s", e)

    def cfg_uplink_tick(self) -> dict:
        """Такт агента: восстановление прерванной транзакции, замер аплинка и
        аварийное правило для висящего вопроса. {"answer": ответ серверу или
        None, "notes": [тексты в чат]}."""
        notes = []
        self._cfg_unit_mark()
        note = self.cfg_uplink_recover()
        if note:
            notes.append(note)
        dead = self.cfg_uplink_observe()
        wait = self.cfg_wait().get("uplink")
        value = self.cfg_pending().get("uplink")
        if not dead or not wait or not value or not value.get("conf"):
            return {"answer": None, "notes": notes}
        last = self.__dict__.get("_uplink_emergency_at")
        if last is not None and time.monotonic() - last < EMERGENCY_RETRY_SECONDS:
            return {"answer": None, "notes": notes}
        if self.cfg_keep().get("uplink") == wait.get("server"):
            return {"answer": None, "notes": notes}        # человек отказался — аварийно не ставим
        same = self.cfg_uplink_same_device(value)
        res = self.cfg_uplink_apply(value, flock_wait=0)   # тик монитора не ждёт замков
        if res.get("state") == "busy":
            return {"answer": None, "notes": notes}        # заняты — попробуем следующим тактом
        self.__dict__["_uplink_emergency_at"] = time.monotonic()
        if not res["ok"]:
            notes.append(self.cfg_uplink_fail_note(res))
            return {"answer": None, "notes": notes}        # вопрос висит, попытка повторится
        self.cfg_release("uplink")
        keep = self.cfg_keep()
        if keep.pop("uplink", None) is not None:
            self._cfg_json_set(self._CFG_KEEP_KEY, keep)
        notes.append(self.cfg_uplink_emergency_note(same))
        answer = {"ok": True, "section": "uplink", "accepted": True, "note": {"state": "ok"},
                  "gen": int(wait.get("gen") or 0), "hash": str(wait.get("hash") or ""), "print": wait.get("server", "")}
        return {"answer": answer, "notes": notes}

    # ── тексты итогов ────────────────────────────────────────────────────────

    @staticmethod
    def cfg_uplink_emergency_note(same_device: bool) -> str:
        tail = "ключ устройства тот же" if same_device else "сервер AWG выдал его этому шлюзу"
        return f"⚠️ Аплинк молчал 10 мин — применил новый конфиг с сервера AWG без подтверждения: {tail}"

    @staticmethod
    def cfg_uplink_fail_note(res: dict) -> str:
        import html
        why = html.escape(str(res.get("error") or "ошибка"), quote=False)
        state = res.get("state")
        if state == "reverted":
            return "⚠️ Новый конфиг аплинка не поднялся — вернул прежний"
        if state == "revert_failed":
            back = html.escape(str(res.get("back_why") or res.get("error") or "ошибка"), quote=False)
            return (f"🔴 Новый конфиг аплинка не поднялся, прежний вернуть не удалось: {back}. "
                    "Загляни в «🩺 Здоровье»; связь с сервером AWG может пропасть")
        return f"⚠️ Новый конфиг аплинка не применён: {why}"


class _Busy(Exception):
    """Замок агента или обвязки занят — транзакция не начиналась."""


def _reason(out: str) -> str:
    """Причина отказа из вывода скрипта: последняя строка «ОШИБКА…», иначе
    последняя строка, кроме итога «uplink: …»."""
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("ОШИБКА"):
            return ln
    rest = [ln for ln in lines if not ln.startswith("uplink: ")]
    return rest[-1] if rest else "скрипт отказал"

