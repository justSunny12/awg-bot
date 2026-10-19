"""join.py — сопряжение на агенте шлюза: подключение командой из бота сервера AWG.

`awg-bot join <токен>` (tools/join.py) поднимает линк по токену и кладёт
файл сопряжения `/etc/awg-gw/join.json` (0600): id, слот, выпуск, срок,
порт канала, состояние pairing | accepted | linked | refused | done. Пока
сопряжение не доведено и токена бота нет, агент работает без Telegram
(runtime/main/join.py): только клиент канала и применение документа. Ход и
код — в `/run/awg-bot/join.status` (JSON), его читает терминал.

Сессия сопряжения: hello{join, eph, host} → join_eph{eph} — у обеих сторон
один код (util/joinsas); join_wrong{left}; join_refused{why} — сопряжение
закрыто; join_ok{box} — секреты под ключом сопряжения (ADMIN_ID) — в env
агента. Затем смена ключей `auto=join` без вопроса (cfg/rekey.py); на новых
ключах — обвязка `--apply` (карантинная таблица уходит), документ сервера
довозит аплинк (правило `fresh`), токен бота и остальное; раздел token
перезапускает агента полным, и он ставит отметку первого старта (отчёт
«установлен» — от конца сопряжения).
"""
from __future__ import annotations

import base64
import json
import os
import socket
import time

from awgbot.util import joinsas

from .base import log

JOIN_FILE = "/etc/awg-gw/join.json"
STATUS_FILE = "/run/awg-bot/join.status"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64u(text: str) -> bytes:
    s = str(text or "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class GatewayJoinMixin:
    """Сопряжение со стороны агента."""

    _join_file = JOIN_FILE
    _join_status_file = STATUS_FILE

    # ── файл сопряжения и файл хода ──────────────────────────────────────────

    def join_state(self) -> dict:
        try:
            with open(self._join_file, encoding="utf-8") as f:
                data = json.loads(f.read() or "{}")
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _join_save(self, data: dict) -> None:
        from awgbot.util.fsatomic import write_private
        write_private(self._join_file, json.dumps(data, ensure_ascii=False))

    def join_status(self, **fields) -> None:
        """Ход для терминала (`awg-bot join`, `--status`) — целиком, атомарно."""
        from awgbot.util.fsatomic import write_private
        st = self.join_state()
        data = {"state": fields.pop("state", ""), "slot": st.get("slot"), "name": st.get("name", ""),
                "until": st.get("expires", ""), "at": int(time.time()), **fields}
        try:
            write_private(self._join_status_file, json.dumps(data, ensure_ascii=False))   # в нём код — 0600
        except OSError as e:
            log.warning("gateway: ход сопряжения не записан: %s", e)

    def join_pairing(self) -> bool:
        """hello несёт join: код не введён, или код верный, а линк ещё на ключах
        токена (обрыв между join_ok и link_go — сервер в такой сессии повторит
        join_ok и link_next тому же эфемерному ключу, join-1)."""
        st = self.join_state()
        if st.get("state") == "pairing":
            return True
        if st.get("state") != "accepted":
            return False
        rk = getattr(self, "rekey_state", None)
        return not (callable(rk) and rk().get("state") == "switched")

    def join_active(self) -> bool:
        """Агенту ещё нужен режим сопряжения (без Telegram): код не введён или
        после кода ещё не приехал токен бота шлюза."""
        return self.join_state().get("state") in ("pairing", "accepted", "linked")

    # ── сессия сопряжения ────────────────────────────────────────────────────

    def join_hello(self) -> dict:
        """Поля hello сессии сопряжения (новая эфемерная пара на каждую сессию);
        не сопряжение — пусто."""
        st = self.join_state()
        if not self.join_pairing() or not st.get("id"):
            return {}
        # пара — на всё сопряжение до линка на постоянных ключах (файл 0600):
        # после верного кода сервер ведёт сессию только с тем эфемерным
        # ключом, чей код ввели
        try:
            priv = bytes.fromhex(str(st.get("eph_priv") or ""))
        except ValueError:
            priv = b""
        if len(priv) != 32:
            priv, _pub = joinsas.new_pair()
            st["eph_priv"] = priv.hex()
            self._join_save(st)
        from nacl.public import PrivateKey
        pub = bytes(PrivateKey(priv).public_key)
        self.__dict__["_join_eph"] = (priv, pub)
        host = socket.gethostname().split(".")[0][:63]
        out = {"join": st["id"], "eph": _b64u(pub)}
        if host and all(c.isalnum() or c in ".-" for c in host):
            out["host"] = host
        return out

    def join_eph_in(self, msg: dict) -> str:
        """join_eph: код подтверждения на экран терминала. Возвращает код."""
        st = self.join_state()
        pair = self.__dict__.get("_join_eph")
        if not pair or not st.get("id"):
            return ""
        try:
            srv = _unb64u(str(msg.get("eph") or ""))
            k = joinsas.shared(pair[0], srv)
        except (ValueError, TypeError):
            return ""
        code = joinsas.code(st["id"], int(st.get("slot") or msg.get("slot") or 0), pair[1], srv, k)
        self.__dict__["_join_kp"] = joinsas.pair_key(st["id"], k)
        name = str(msg.get("slot_name") or "")[:64]
        if name and name != st.get("name"):
            st["name"] = name
            self._join_save(st)
        if msg.get("accepted"):
            self.join_status(state="accepted")        # код уже верный — ждём ключи
        else:
            self.join_status(state="code", code=joinsas.show(code))
        return code

    def join_next_open(self, msg: dict) -> dict:
        """link_next сессии сопряжения: конфиг новых ключей — под ключом
        сопряжения (box); расшифровать. Не расшифровалось или ключа нет —
        без conf (проверка откажет)."""
        kp = self.__dict__.get("_join_kp")
        if not kp:
            return {k: v for k, v in msg.items() if k != "box"}
        try:
            data = joinsas.unseal(kp, str(msg.get("box") or ""))
        except ValueError:
            log.warning("gateway: новые ключи линка в сессии сопряжения не расшифровались")
            return {k: v for k, v in msg.items() if k != "box"}
        return {**{k: v for k, v in msg.items() if k != "box"}, "conf": str(data.get("conf") or "")}

    def join_wrong_in(self, msg: dict) -> None:
        try:
            left = max(0, int(msg.get("left") or 0))
        except (TypeError, ValueError):
            left = 0
        cur = {}
        try:
            with open(self._join_status_file, encoding="utf-8") as f:
                cur = json.loads(f.read() or "{}")
        except (OSError, ValueError):
            pass
        self.join_status(state="wrong", left=left, code=cur.get("code", ""))

    def join_refused_in(self, msg: dict) -> None:
        why = "".join(c for c in str(msg.get("why") or "") if c.isalnum())[:16] or "gone"
        st = self.join_state()
        if st:
            st["state"] = "refused"
            st["why"] = why
            st.pop("eph_priv", None)                      # пара отслужила — закрытый ключ прочь
            self._join_save(st)
        self.__dict__.pop("_join_eph", None)
        self.__dict__.pop("_join_kp", None)
        self.join_status(state="refused", why=why)
        log.info("gateway: сервер AWG закрыл сопряжение (%s)", why)

    def join_ok_in(self, msg: dict) -> bool:
        """join_ok: секреты под ключом сопряжения — ADMIN_ID в env агента."""
        kp = self.__dict__.get("_join_kp")
        st = self.join_state()
        if st.get("state") == "accepted":
            return True                                   # повтор после обрыва (join-1): секреты уже записаны
        if not kp or st.get("state") != "pairing":
            return False
        try:
            secrets_ = joinsas.unseal(kp, str(msg.get("box") or ""))
            admin_id = int(secrets_.get("admin_id") or 0)
        except (ValueError, TypeError):
            log.warning("gateway: секреты сопряжения не расшифровались")
            return False
        if admin_id <= 0:
            return False
        try:
            self._join_env_set("ADMIN_ID", str(admin_id))
        except OSError as e:
            log.warning("gateway: ADMIN_ID не записан: %s", e)
            return False
        st.update(state="accepted", server_bot=str(secrets_.get("server_bot") or "")[:64],
                  name=str(secrets_.get("name") or st.get("name") or "")[:64])
        self._join_save(st)
        self.join_status(state="accepted")
        log.info("gateway: код подтверждён сервером AWG — жду постоянные ключи линка")
        return True

    def _join_env_set(self, key: str, value: str) -> None:
        from awgbot.util.fsatomic import write_private
        path = self._cfg_env_path()
        try:
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if not ln.startswith(f"{key}=")]
        except FileNotFoundError:
            lines = []
        lines.append(f"{key}={value}")
        write_private(path, "\n".join(lines) + "\n")

    def join_linked(self) -> None:
        """Линк на постоянных ключах (смена ключей после кода закреплена):
        обвязка полным прогоном — карантинная таблица уходит, появляется юнит,
        строки которого дальше ведёт документ сервера."""
        st = self.join_state()
        if st.get("state") != "accepted":
            return
        from awgbot.infra import gwguard
        ok, out = gwguard.join_finish(st.get("channel_port") or 0)
        if not ok:
            log.warning("gateway: обвязка после сопряжения не применилась: %s", out[-300:])
        st.pop("eph_priv", None)                          # линк на постоянных ключах — пара отслужила
        st["state"] = "linked"
        self._join_save(st)
        self.join_status(state="linked")
        from awgbot.core import config
        if config.BOT_TOKEN:
            # агент полный (стоял раньше, подключён заново): токен бота у него уже есть —
            # конец сопряжения здесь, не ждём раздела token (тот же бот — раздела
            # нет вовсе: отпечатки совпадают)
            self.join_finish_start(getattr(self, "bot_username", "") or "")

    def join_finish_start(self, bot_username: str = "") -> bool:
        """Полный старт агента после сопряжения (токен приехал): отметка первого
        старта — отсюда, а не от первого запуска процесса (отчёт «установлен»);
        файл сопряжения — прочь. True — это был конец сопряжения."""
        st = self.join_state()
        if st.get("state") not in ("accepted", "linked"):
            return False
        self.first_start_note(True)
        # итог сопряжения сервер узнаёт своим словом join_done (и у агента,
        # стоявшего раньше, — отметка первого старта у него давняя): обычное
        # «установлен» не нужно — второго «настроен» админ не получит
        self.installed_report_done()
        if hasattr(self, "cfg_token_note_clear"):
            self.cfg_token_note_clear()                   # «бот на новом токене» новому шлюзу ни к чему
        st["state"] = "done"
        self._join_save(st)
        self.join_status(state="done", bot=bot_username, name=st.get("name", ""))
        return True

    def join_done_pending(self) -> dict:
        """Тело join_done серверу ({"id"}) — агент полный, сопряжение доведено;
        иначе пусто."""
        st = self.join_state()
        return {"id": str(st.get("id") or "")} if st.get("state") == "done" and st.get("id") else {}

    def join_done_sent(self) -> None:
        """Сервер принял join_done — файл сопряжения больше не нужен."""
        try:
            os.remove(self._join_file)
        except OSError:
            pass

    def join_fresh_uplink(self) -> bool:
        """Правило `fresh` первого аплинка: агент подключён командой (код
        подтверждён) — вопрос задать негде, чата ещё нет."""
        return self.join_state().get("state") in ("accepted", "linked")
