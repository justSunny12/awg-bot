"""bundle.py — операции с кнопки, метка выпуска файла конфигурации, токен пометки, резервная копия.

Метку выпуска применённого файла первого применения (ISSUED_AT) хранят база
агента и обвязка; её двигает и смена ключей линка: прежние файлы несут
прежние ключи, и скрипт обвязки откажет им при ручном запуске."""

from __future__ import annotations

import socket
import time
from pathlib import Path
from awgbot.core import config
from awgbot.domain.services import ServiceError
from awgbot.domain.gateway import base
from awgbot.domain.gateway.base import log


class BundleMixin:
    """Операции с кнопки, метка выпуска, токен пометки, резервная копия."""
    # ── операции с кнопки (этап 2) ───────────────────────────────────────────

    def restart_link(self) -> tuple[bool, str]:
        """Мягкий рестарт линка: down/up интерфейса без пересборки обвязки.
        Секунды обрыва RF у всех — поэтому только с подтверждения."""
        # Результат down намеренно не проверяем: интерфейс мог быть уже опущен,
        # и это не отказ — важно только, поднялся ли он обратно.
        base._run(["awg-quick", "down", config.GW_LINK_IF], timeout=30)
        up = base._run(["awg-quick", "up", config.GW_LINK_IF], timeout=30)
        ok = up.returncode == 0
        tail = (base._out(up) + up.stderr.decode(errors="replace")).strip().splitlines()[-3:]
        return ok, "\n".join(tail) if tail else ("поднят" if ok else "не поднялся")

    def restart_awg(self) -> tuple[bool, str]:
        """То же, что restart_link, — общее имя для раздела «🔧 Сервис» обеих ролей."""
        return self.restart_link()

    def reassert(self) -> tuple[bool, str]:
        """Полный реассерт: рестарт юнита шлюза — тот зовёт gw-скрипт, который
        идемпотентно переставляет правила и переподнимает линк. Под общим
        замком применения: параллельно с документом конфигурации не дёргаем."""
        return self.reassert_guarded("кнопка «Восстановить»")

    _BUNDLE_ISSUED_KEY = "gw_bundle_issued_at"

    def bundle_issued_applied(self) -> int:
        """Метка выпуска применённого — старшая из базы агента и файла обвязки
        (его пишет и скрипт обвязки, когда файл запускают руками)."""
        from awgbot.infra import gwguard
        try:
            on_disk = int((base.pathlib_read(f"{gwguard.GW_ETC}/issued_at") or "").strip() or 0)
        except (OSError, ValueError):
            on_disk = 0
        return max(int(self.db.get_state(self._BUNDLE_ISSUED_KEY) or 0), on_disk)

    def bundle_issued_raise(self, ts: int) -> None:
        """Применённое — не старее ts: файл, выпущенный раньше, отвергнет
        скрипт обвязки при ручном запуске. Двигается после смены ключей линка
        (прежние файлы несут прежние ключи)."""
        from awgbot.infra import gwguard
        from awgbot.util.fsatomic import write_private
        ts = int(ts)
        if ts <= self.bundle_issued_applied():
            return
        self.db.set_state(self._BUNDLE_ISSUED_KEY, str(ts))
        try:
            write_private(Path(gwguard.GW_ETC) / "issued_at", f"{ts}\n")
        except OSError as e:
            log.warning("gateway: метка выпуска не записана в обвязку: %s", e)

    @staticmethod
    def _unit_settled(limit: float) -> str:
        """Подождать выхода юнита обвязки из activating (до limit секунд, опрос
        раз в 5 с); вернуть итоговое ActiveState («activating» — не дождались)."""
        from awgbot.infra import gwguard
        deadline = time.monotonic() + limit
        state = gwguard.unit_state().get("ActiveState", "")
        while state == "activating" and time.monotonic() < deadline:
            time.sleep(5)
            state = gwguard.unit_state().get("ActiveState", "")
        return state

    def gateway_claim_if_needed(self) -> str | None:
        """Токен пометки для канала, если шлюз в основном боте не помечен; иначе
        None. Без побочных эффектов: статус — последний, что записал скрипт
        обвязки, ключ аплинка — с машины. Ручная пересылка остаётся как была:
        канал лишь избавляет человека от копирования сообщения между чатами."""
        from awgbot.infra import gwguard
        if self.gateway_mark_status() not in ("unmarked", "foreign", "unconfirmed"):
            return None
        _iface, pub = gwguard.uplink_pubkey()
        if not pub:
            return None
        try:
            return self.gateway_claim_message(pub)
        except (OSError, ValueError) as e:
            log.warning("gateway: claim для канала не собран: %s", e)
            return None

    def gateway_claim_message(self, pubkey: str) -> str:
        """Подписанный ключом линка токен «я шлюз с таким аплинком» — с
        возможностями канала агента (контракт канала)."""
        from awgbot.runtime.linkclient.common import CAPS
        from awgbot.util import bundlecrypt, gwsign
        priv = bundlecrypt.read_privkey(base.pathlib_read(config.GW_LINK_CONF))
        return gwsign.sign(priv, "claim", pubkey, host=socket.gethostname(), caps=CAPS)

    def gateway_mark_status(self) -> str:
        """Помечен ли шлюз: решение скрипта обвязки при последнем прогоне
        (gateway.status — пишется каждым применением и реассертом). Раньше
        значение в базу клал только итог применения файла перевыпуска из чата;
        с его снятием запись застыла бы — у нового шлюза навсегда «?», и claim
        по каналу не уходил бы. Запись в базе — запасная, пока файла нет."""
        from awgbot.infra import gwguard
        live = (gwguard.script_status().get("GW_STATUS") or "").strip()
        if live in ("unmarked", "confirmed", "foreign", "unconfirmed"):
            return live
        return self.db.get_state(self._GW_MARK_KEY) or "?"


    # ── резервная копия ──────────────────────────────────────────────────────

    def make_backup(self) -> list[str]:
        """Один архив: БД агента, все conf/*.yaml, env и ВСЕ конфиги
        awg-интерфейсов шлюза (в них приватные ключи линка и туннеля —
        единственная копия вне этой машины). Только шифрованно: без парольной
        фразы отказ, открытые ключи в чат и на почту не уезжают."""
        if not self.backup_encryption_enabled():
            raise ServiceError("резервная копия шлюза только шифрованная: задай парольную "
                               "фразу в ⚙️ Настройки → 💾 Бэкапы → 🔐 Шифрование")
        return self.write_backup_archive("gw", self.backup_extra(), require_encryption=True)
