"""agentbot.py — токен и имя бота агента по слотам."""

from __future__ import annotations

import os
import re
from typing import Optional
from awgbot.core import config
from awgbot.domain.services.types import ServiceError
from .common import log


class AgentBotMixin:
    """Токен и имя бота агента по слотам."""
    # ── токен бота-агента: свой у каждого слота (два агента на одном токене
    # перехватывали бы апдейты друг у друга), спрашивается один раз на слот ──
    _GW_TOKEN_ENV = "GW_BOT_TOKEN"

    def _gw_token_env(self, slot_id: Optional[int]) -> str:
        return self._GW_TOKEN_ENV if not slot_id or int(slot_id) == 1 else f"{self._GW_TOKEN_ENV}_{int(slot_id)}"

    @staticmethod
    def _env_path() -> str:
        return os.environ.get("AWG_BOT_ENV", "/etc/awg-bot/env")

    def gw_bot_token(self, slot_id: Optional[int] = None) -> str:
        """Токен бота шлюза слота из env. Пусто — ещё не спрашивали."""
        key = self._gw_token_env(slot_id)
        try:
            with open(self._env_path(), encoding="utf-8") as f:
                for line in f:
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""


    # ── кто такой бот шлюза: username и имя из getMe по токену слота ───────
    _GW_BOT_ME_KEY = "gw_bot_me"

    def _gw_token_id(self, slot_id: Optional[int]) -> str:
        import hashlib
        token = self.gw_bot_token(slot_id)
        return hashlib.sha256(token.encode()).hexdigest()[:12] if token else ""

    def gw_bot_identity(self, slot_id: Optional[int]) -> dict:
        """{'username', 'name'} бота шлюза слота: из кэша getMe по токену, а без
        него — из снимка канала (агент знает своего бота сам). Слот, заведённый
        до того, как токен стал спрашиваться на сервере, иначе остался бы без
        ссылки: токен задаётся один раз при настройке и в интерфейсе не вводится.
        Пусто — не спрашивали, токен сменился или канал ещё не поднимался."""
        me = self._gw_bot_identity_cached(slot_id)
        if me.get("username"):
            return me
        snap = self.gwlink_snapshot(int(slot_id or 1)).get("agent_bot")
        if isinstance(snap, dict) and snap.get("username"):
            return {"username": str(snap["username"]), "name": str(snap.get("name") or "")}
        return {}

    def _gw_bot_identity_cached(self, slot_id: Optional[int]) -> dict:
        data = self.db.get_state_json(self._gw_slot_key(self._GW_BOT_ME_KEY, int(slot_id or 1)), {})
        if data.get("token_id") != self._gw_token_id(slot_id):
            return {}
        return {"username": str(data.get("username") or ""), "name": str(data.get("name") or "")}

    def set_gw_bot_identity(self, slot_id: Optional[int], username: str, name: str) -> None:
        import json
        self.db.set_state(self._gw_slot_key(self._GW_BOT_ME_KEY, int(slot_id or 1)),
                          json.dumps({"username": username, "name": name[:64],
                                      "token_id": self._gw_token_id(slot_id)}, ensure_ascii=False))

    def gw_bot_identity_missing(self) -> list[int]:
        """Слоты, у которых токен есть, а ответа Telegram ещё нет (снимок
        канала не в счёт: ответ по токену точнее и переживает смену агента)."""
        return [g.id for g in self.db.gateways()
                if self.gw_bot_token(g.id) and not self._gw_bot_identity_cached(g.id)]

    def forget_gw_bot_token(self, slot_id: Optional[int] = None) -> None:
        """Убрать токен бота слота из env (снятие слота) и память getMe."""
        path = self._env_path()
        key = self._gw_token_env(slot_id)
        try:
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if not ln.startswith(key + "=")]
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("gateway_remove: токен слота %s не убран из env: %s", slot_id, e)
            return
        try:
            from awgbot.util.fsatomic import write_private
            write_private(path, "\n".join(lines) + "\n")
        except OSError as e:
            log.warning("gateway_remove: токен слота %s не убран из env: %s", slot_id, e)
        try:
            from awgbot.runtime import gwbotme
            gwbotme.forget(int(slot_id or 1))
        except Exception:                                 # noqa: BLE001
            pass

    def set_gw_bot_token(self, token: str, slot_id: Optional[int] = None) -> None:
        """Запомнить токен агента. Хранение осознанное: без него перевыпуск
        файла первого применения (переустановили машину-шлюз, сменили её)
        снова требовал бы идти в BotFather. Уровень доверия тот же, что у
        приватных ключей, которые в этом файле и так лежат."""
        token = str(token).strip()
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ServiceError("это не похоже на токен бота — жду строку вида 123456789:AA…")
        path = self._env_path()
        key = self._gw_token_env(slot_id)
        try:
            lines = []
            try:
                with open(path, encoding="utf-8") as f:
                    lines = [ln for ln in f.read().splitlines()
                             if not ln.startswith(key + "=")]
            except FileNotFoundError:
                pass
            lines.append(f"{key}={token}")
            # атомарно: усечение с последующей записью при ENOSPC оставляло
            # пустой env — и основной бот не стартовал
            from awgbot.util.fsatomic import write_private
            write_private(path, "\n".join(lines) + "\n")
        except OSError as e:
            raise ServiceError(f"не записать {path}: {e}")

    def _bundle_with_agent(self, plain: bytes, slot_id: Optional[int] = None) -> bytes:
        """Токен агента и ADMIN_ID — в файл первого применения, чтобы установка
        на шлюзе не задавала ВООБЩЕ ни одного вопроса.

        Строки кладутся ПОСЛЕ `exit` в теле бандла (перед маркером скрипта
        обвязки), то есть при запуске бандла не исполняются — это данные для
        установщика, а не команды.
        """
        token = self.gw_bot_token(slot_id)
        if not token:
            return plain
        m = self._MAIL_MARK_LINE.search(plain)
        if m is None:
            return plain
        lines = (f'AGENT_BOT_TOKEN="{token}"\n'
                 f'AGENT_ADMIN_ID="{config.ADMIN_ID}"\n').encode()
        return plain[:m.start()] + lines + plain[m.start():]

    def _bundle_with_mail(self, plain: bytes) -> bytes:
        """Настройки почты и парольная фраза бэкапов — в бандл, чтобы не вводить
        их дважды: строки MAIL_B64 / BACKUP_B64 (JSON в base64) перед строкой
        маркера контракта. Бандл шифрован ключом линка. Чего нет на ВПС — не
        добавляется, агент оставляет своё как есть."""
        m = self._MAIL_MARK_LINE.search(plain)
        if m is None:
            return plain
        import base64
        import json
        lines = b""
        acc = self.email_account()
        if acc is not None:
            # backup=email — бэкапы ВПС уходят на почту: шлюз, приняв ящик,
            # переключит свои туда же (при включённом шифровании)
            payload = json.dumps({"login": acc.login, "password": acc.password,
                                  "imap_host": acc.imap_host, "imap_port": acc.imap_port,
                                  "smtp_host": acc.smtp_host, "smtp_port": acc.smtp_port,
                                  "backup": self.backup_channel()},
                                 ensure_ascii=False).encode()
            lines += b'MAIL_B64="' + base64.b64encode(payload) + b'"\n'
        if self.backup_encryption_mode() == "passphrase":
            phrase = self.db.get_state(self._BK_PASSPHRASE_KEY) or ""
            lines += b'BACKUP_B64="' + base64.b64encode(json.dumps({"passphrase": phrase}).encode()) + b'"\n'
        if not lines:
            return plain
        return plain[:m.start()] + lines + plain[m.start():]
