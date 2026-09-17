"""
gateway_link.py — шлюз условной маршрутизации: пометка устройства, бандл,
токен бота-агента.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.infra import routing
from awgbot.domain import configgen
from awgbot.domain.services.types import Notification, ServiceError


log = logging.getLogger("awgbot.services")


class GatewayLinkMixin:
    # ── бандл шлюза: скрипт линка и сборка архива ────────────────────────────
    _GW_BUNDLE_ISSUED_KEY = "gw_bundle_issued_at"

    def _link_script(self) -> str:
        return str(config.BASE_DIR / "install" / "routing-link-setup.sh")

    def _run_link_script(self, mode: str, env: dict | None = None) -> None:
        import subprocess
        proc = subprocess.run(["sh", self._link_script(), mode], capture_output=True,
                              timeout=120, env={**os.environ, **(env or {})})
        if proc.returncode != 0:
            raise ServiceError(f"скрипт линка ({mode}) не отработал: "
                               + proc.stderr.decode(errors="replace").strip()[-200:])

    def _gw_bundle_env(self) -> tuple[dict, list[str]]:
        """Окружение сборки бандла: устройства админа, ключ и конфиг аплинка
        назначенного шлюза (в окне переезда — двойника, старый ключ отдельно)."""
        admin_ips = self._gw_ssh_allow()
        env = {"ADMIN_IPS": " ".join(admin_ips)}
        gw = self.db.gateway_device()
        if gw is not None:
            import base64
            twin = self.db.twin_of_device(gw.id)
            target = twin or gw
            env["GATEWAY_PUBKEY"] = target.public_key
            env["GATEWAY_PREV_PUBKEY"] = gw.public_key if twin else ""
            try:
                env["UPLINK_B64"] = base64.b64encode(self.gateway_uplink_conf(target).encode()).decode()
            except ServiceError as e:
                log.warning("bundle: конфиг аплинка шлюза не собран: %s", e)
        return env, admin_ips

    def _gw_bundle_build(self) -> tuple[bytes, str]:
        """Собрать бандл скриптом линка (ключи не меняются) и дополнить почтой,
        фразой бэкапов. Возвращает (открытый текст, приватный ключ шлюза)."""
        from awgbot.util import bundlecrypt
        env, admin_ips = self._gw_bundle_env()
        self._run_link_script("--bundle", env)
        link_if = config.ROUTING_GW_INTERFACE or "awglink"
        with open(f"/root/gw-{link_if}.conf", encoding="utf-8") as f:
            priv = bundlecrypt.read_privkey(f.read())
        with open("/root/awg-gw-bundle.sh", "rb") as f:
            plain = f.read()
        plain = self._bundle_with_mail(plain)
        plain = self._bundle_with_agent(plain)
        self.db.set_state(self._GW_BUNDLE_SSH_KEY, " ".join(admin_ips))
        self.db.set_state(self._GW_BUNDLE_SSH_NOTIFIED_KEY, "")
        self.db.set_state(self._GW_BUNDLE_ISSUED_KEY, timeutil.to_iso(timeutil.now()))
        return plain, priv

    def gw_bundle_encrypted(self) -> tuple[bytes, str]:
        """Бандл для доставки чатом: шифрован ключом линка, который есть только
        у уже настроенного шлюза. Открытый бандл на диске ВПС остаётся под 600."""
        from awgbot.util import bundlecrypt
        plain, priv = self._gw_bundle_build()
        return bundlecrypt.encrypt(plain, priv), "awg-gw-bundle.enc"

    def gw_bundle_plain(self) -> tuple[bytes, str]:
        """Открытый бандл — для ПЕРВОГО применения на машине, у которой ключа
        линка ещё нет (новая машина или новые ключи). Внутри приватные ключи:
        тот же уровень доверия, что у ссылок vpn:// с ключами устройств."""
        plain, _ = self._gw_bundle_build()
        return plain, "awg-gw-bundle.sh"

    # ── шлюз условной маршрутизации: пометка устройства ──────────────────────
    _GW_NONCES_KEY = "gw_claim_nonces"

    def _link_privkey(self) -> str:
        from awgbot.util import bundlecrypt
        link_if = config.ROUTING_GW_INTERFACE or "awglink"
        try:
            with open(f"/root/gw-{link_if}.conf", encoding="utf-8") as f:
                return bundlecrypt.read_privkey(f.read())
        except (OSError, ValueError) as e:
            raise ServiceError(f"ключ линка не прочитан: {e}")

    def _gw_nonce_seen(self, nonce: str) -> bool:
        seen = (self.db.get_state(self._GW_NONCES_KEY) or "").split()
        if nonce in seen:
            return True
        self.db.set_state(self._GW_NONCES_KEY, " ".join((seen + [nonce])[-50:]))
        return False

    def gateway_claim(self, text: str) -> dict:
        """Пересланное от агента сообщение с токеном `claim`. Проверка подписи
        ключом линка, поиск устройства по ключу аплинка, единственность.
        Возвращает {'status': 'marked'|'already', 'device'}; занятый другим
        устройством шлюз — ServiceError, менять его — через настройки."""
        from awgbot.util import gwsign
        data = gwsign.verify(self._link_privkey(), text)
        if self._gw_nonce_seen(data["nonce"]):
            raise ServiceError("это сообщение уже принимали — пусть шлюз выдаст новое")
        dev = self.db.get_device_by_pubkey(data["pub"])
        if dev is None:
            raise ServiceError("устройства с таким ключом нет: аплинк шлюза должен быть "
                               "устройством админа, выпущенным этим ботом")
        admin = self.admin_client()
        if admin is None or dev.client_id != admin.id:
            raise ServiceError("шлюзом может быть только устройство профиля админа")
        if dev.is_gateway:
            return {"status": "already", "device": dev}
        prev = self.db.gateway_device()
        if prev is not None:
            raise ServiceError(f"шлюз уже назначен: «{prev.name}». Сменить его можно в "
                               "настройках условной маршрутизации («🔁 Сменить шлюз»)")
        self.db.set_gateway(dev.id)
        return {"status": "marked", "device": self.db.get_device(dev.id)}

    def gateway_candidates(self) -> list:
        """Устройства админа, выпущенные ботом, кроме текущего шлюза."""
        admin = self.admin_client()
        if admin is None:
            return []
        return [d for d in self.db.list_devices(admin.id) if d.private_key and not d.is_gateway]

    _GW_NEW_NAME = "Шлюз"

    def gateway_setup(self, device_id: Optional[int] = None, *, rekey: bool = False) -> dict:
        """Назначить шлюз. device_id — существующее устройство админа; None —
        создать новое устройство «Шлюз» в профиле админа (новая машина).
        rekey — новые ключи линка: прежняя машина теряет линк по построению,
        а первый бандл для новой едет открытым (ключа у неё ещё нет).
        Возвращает {'device', 'previous', 'created', 'rekeyed'}."""
        admin = self.admin_client()
        if admin is None:
            raise ServiceError("профиль админа ещё не создан")
        created = False
        if device_id is None:
            # Лимит шлюзу не делают исключением: профиль админа безлимитный по
            # построению, а счётчик, который врёт на одну строку, хуже лимита.
            dc = self.add_device(admin.id, self._GW_NEW_NAME)
            device_id = dc.device_id
            created = True
            rekey = True                       # новая машина без ключа линка
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        if dev.client_id != admin.id:
            raise ServiceError("шлюзом может быть только устройство профиля админа")
        if not dev.private_key:
            raise ServiceError("это устройство создавал не бот — его конфиг в бандл не собрать")
        prev = self.db.gateway_device()
        if prev is not None and prev.id == dev.id:
            prev = None
        self.db.set_gateway(dev.id)
        if rekey:
            self._run_link_script("--rekey")
            routing.invalidate_self_check()
        return {"device": self.db.get_device(dev.id), "previous": prev,
                "created": created, "rekeyed": rekey}

    def gateway_remove(self) -> Optional[object]:
        """Убрать шлюз: флаг снять, ключи линка сменить (прежняя машина теряет
        линк), условную маршрутизацию выключить. Возвращает бывший шлюз."""
        prev = self.db.gateway_device()
        if prev is None:
            return None
        self.db.set_gateway(None)
        try:
            settings.set_value("app.routing.enabled", False)
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway_remove: маршрутизация не выключена: %s", e)
        try:
            self._run_link_script("--rekey")
            routing.invalidate_self_check()
        except ServiceError as e:
            log.warning("gateway_remove: ключи линка не сменены: %s", e)
        try:
            self.reconcile_routing()
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway_remove: реконсиляция: %s", e)
        return prev

    def gateway_state(self) -> dict:
        """Одно состояние для экрана: устройство, когда выпущен бандл, жив ли
        линк по последнему замеру и возраст хендшейка."""
        gw = self.db.gateway_device()
        issued = self.db.get_state(self._GW_BUNDLE_ISSUED_KEY) or ""
        age = None
        if gw is not None and config.ROUTING_GW_INTERFACE:
            try:
                age = routing.link_handshake_age()
            except Exception:                             # noqa: BLE001
                age = None
        return {"device": gw, "issued_at": issued, "link_ok": self.routing_link_ok(),
                "handshake_age": age}

    def gateway_uplink_conf(self, dev) -> str:
        """Конфиг аплинка шлюза для бандла: обычный клиентский .conf устройства
        в форме для машины-шлюза (без DNS, Table = off)."""
        cfg = self.generate_config(dev.id, for_bundle=True)
        return configgen.gateway_uplink_conf(cfg["conf"])

    _GW_BUNDLE_SSH_KEY = "gw_bundle_ssh_allow"
    _GW_BUNDLE_SSH_NOTIFIED_KEY = "gw_bundle_ssh_allow_notified"

    def _gw_ssh_allow(self) -> list[str]:
        return sorted(set(self.db.admin_device_addresses(config.ADMIN_ID)))

    def gw_bundle_drift_notes(self) -> list[Notification]:
        """Состав устройств админа разошёлся с тем, что уехало в бандл шлюза:
        напомнить один раз на каждое новое расхождение. Пока бандл не собирали
        — молчим: напоминать не о чем."""
        if not config.ROUTING_ENABLED:
            return []
        sent = self.db.get_state(self._GW_BUNDLE_SSH_KEY)
        if sent is None:
            return []
        cur = " ".join(self._gw_ssh_allow())
        if cur == sent or self.db.get_state(self._GW_BUNDLE_SSH_NOTIFIED_KEY) == cur:
            return []
        self.db.set_state(self._GW_BUNDLE_SSH_NOTIFIED_KEY, cur)
        return [Notification(config.ADMIN_ID,
                             "🛰 Состав устройств админа изменился, а на шлюз уехал прежний: "
                             "доступ к шлюзу и домашней сети через туннель — по старому списку. "
                             "Перевыпусти конфигурацию шлюза (🛰 Шлюз → Конфигурация шлюза).")]

    # Маркер контракта как ОТДЕЛЬНАЯ СТРОКА. Тот же текст встречается в бандле и
    # внутри sed-выражения, которым он вырезает скрипт обвязки; вставка туда
    # ломала sed, и на шлюз ложился пустой скрипт (наступили: 09.09.2026).
    _MAIL_MARK_LINE = re.compile(rb"^#__GW_SETUP_BELOW__$", re.M)

    # ── токен бота-агента: спрашиваем один раз, храним рядом со своим ────────
    _GW_TOKEN_ENV = "GW_BOT_TOKEN"

    @staticmethod
    def _env_path() -> str:
        return os.environ.get("AWG_BOT_ENV", "/etc/awg-bot/env")

    def gw_bot_token(self) -> str:
        """Токен бота шлюза из env. Пусто — ещё не спрашивали."""
        try:
            with open(self._env_path(), encoding="utf-8") as f:
                for line in f:
                    if line.startswith(self._GW_TOKEN_ENV + "="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    def set_gw_bot_token(self, token: str) -> None:
        """Запомнить токен агента. Хранение осознанное: без него перевыпуск
        файла первого применения (переустановили машину-шлюз, сменили её)
        снова требовал бы идти в BotFather. Уровень доверия тот же, что у
        приватных ключей, которые в этом файле и так лежат."""
        token = str(token).strip()
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ServiceError("это не похоже на токен бота — жду строку вида 123456789:AA…")
        path = self._env_path()
        try:
            lines = []
            try:
                with open(path, encoding="utf-8") as f:
                    lines = [ln for ln in f.read().splitlines()
                             if not ln.startswith(self._GW_TOKEN_ENV + "=")]
            except FileNotFoundError:
                pass
            lines.append(f"{self._GW_TOKEN_ENV}={token}")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(path, 0o600)
        except OSError as e:
            raise ServiceError(f"не записать {path}: {e}")

    def _bundle_with_agent(self, plain: bytes) -> bytes:
        """Токен агента и ADMIN_ID — в файл первого применения, чтобы установка
        на шлюзе не задавала ВООБЩЕ ни одного вопроса.

        Строки кладутся ПОСЛЕ `exec` в теле бандла (перед маркером скрипта
        обвязки), то есть при запуске бандла не исполняются — это данные для
        установщика, а не команды.
        """
        token = self.gw_bot_token()
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
            payload = json.dumps({"login": acc.login, "password": acc.password,
                                  "imap_host": acc.imap_host, "imap_port": acc.imap_port,
                                  "smtp_host": acc.smtp_host, "smtp_port": acc.smtp_port},
                                 ensure_ascii=False).encode()
            lines += b'MAIL_B64="' + base64.b64encode(payload) + b'"\n'
        if self.backup_encryption_mode() == "passphrase":
            phrase = self.db.get_state(self._BK_PASSPHRASE_KEY) or ""
            lines += b'BACKUP_B64="' + base64.b64encode(json.dumps({"passphrase": phrase}).encode()) + b'"\n'
        if not lines:
            return plain
        return plain[:m.start()] + lines + plain[m.start():]
