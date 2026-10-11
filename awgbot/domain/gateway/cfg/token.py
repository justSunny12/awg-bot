"""token.py — токен бота шлюза с сервера AWG: тот же бот, getMe новым токеном, запись в env, перезапуск агента после ответа серверу."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from awgbot.core import config
from awgbot.domain import gwcfg

from ..base import log

GETME_TIMEOUT = 15          # с — getMe новым токеном (через аплинк, как вся связь агента с Telegram)
_NOTE_KEY = "cfg_token_note"   # после перезапуска — сказать в чат, что бот на новом токене


class CfgTokenMixin:
    """Токен меняют аварийно: утёк — отозвали в BotFather, вписали новый на
    сервере AWG («⚙️ Настройки → 🔧 Сервис → 🤖 Токен бота»). Старый
    токен к этому моменту мёртв, бот шлюза из Telegram пропал — новый может
    довезти только канал. Принимается только токен ТОГО ЖЕ бота (id — до
    двоеточия, и его же вернул getMe): чужой бот перехватил бы чат админа.
    Записали — ответ серверу, затем перезапуск агента вне своего cgroup."""

    @staticmethod
    def _cfg_env_path() -> str:
        return os.environ.get("AWG_BOT_ENV", "/etc/awg-bot/env")

    def cfg_token_print(self) -> str:
        return gwcfg.token_print(config.BOT_TOKEN) if config.BOT_TOKEN else ""

    @staticmethod
    def _cfg_token_getme(token: str) -> tuple[str, int]:
        """("ok", id) | ("bad", 0) — Telegram токен не принял | ("net", 0) — не ответил."""
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe",
                                        timeout=GETME_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            return ("bad", 0) if e.code in (401, 404) else ("net", 0)
        except (OSError, ValueError):
            return "net", 0
        if not data.get("ok"):
            return "bad", 0
        try:
            return "ok", int((data.get("result") or {}).get("id") or 0)
        except (TypeError, ValueError):
            return "bad", 0

    def cfg_token_apply(self, value: dict) -> dict:
        """{"state": same | applied | failed | retry, "error"}. applied — env
        переписан, нужен перезапуск агента (его делает клиент канала после
        ответа серверу)."""
        new = str(value.get("value") or "").strip()
        cur = config.BOT_TOKEN
        if not new or new == cur:
            return {"state": "same", "error": ""}
        if cur and new.split(":", 1)[0] != cur.split(":", 1)[0]:
            return {"state": "failed", "error": "токен другого бота — не применяю"}
        how, bot_id = self._cfg_token_getme(new)
        if how == "net":
            return {"state": "retry", "error": "Telegram не ответил на проверку нового токена"}
        if how == "bad" or str(bot_id) != new.split(":", 1)[0]:
            return {"state": "failed", "error": "Telegram не принял новый токен"}
        try:
            self._cfg_token_write(new)
        except OSError as e:
            return {"state": "failed", "error": f"env не переписан ({e})"}
        self.db.set_state(_NOTE_KEY, "1")
        log.info("gateway: токен бота сменён с сервера AWG — агент перезапустится")
        return {"state": "applied", "error": ""}

    def _cfg_token_write(self, token: str) -> None:
        """BOT_TOKEN в env агента — атомарно, прочие строки как были."""
        from awgbot.util.fsatomic import write_private
        path = self._cfg_env_path()
        try:
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if not ln.startswith("BOT_TOKEN=")]
        except FileNotFoundError:
            lines = []
        lines.append(f"BOT_TOKEN={token}")
        write_private(path, "\n".join(lines) + "\n")

    def cfg_token_note_pending(self) -> bool:
        """После перезапуска: бот поднялся на токене с сервера — надо сказать."""
        return bool(self.db.get_state(_NOTE_KEY))

    def cfg_token_note_clear(self) -> None:
        """Сказали — метку прочь (не дошло — останется до следующего старта)."""
        self.db.set_state(_NOTE_KEY, "")

    def cfg_token_note_take(self) -> bool:
        if self.cfg_token_note_pending():
            self.cfg_token_note_clear()
            return True
        return False
