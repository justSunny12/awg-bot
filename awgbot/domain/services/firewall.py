"""
firewall.py — файервол из чата (README §6b).
"""
from __future__ import annotations

import contextlib
import html
import logging
import os
import sys

from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import awg
from awgbot.domain.services.types import ServiceError


log = logging.getLogger("awgbot.services")


class FirewallMixin:
    # ── файервол из чата (README §6b) ────────────────────────────────────────
    # Раньше единственным интерфейсом был CLI, и «подтверди вход» требовало
    # второго SSH-сеанса. Из чата это честнее: Telegram доступен независимо от
    # того, заперли вы себе SSH или нет, а таймер отката страхует ровно от
    # этого случая.

    def firewall_screen(self) -> dict:
        from awgbot.infra import nftguard
        st = nftguard.status(self.db.admin_device_addresses(config.ADMIN_ID),
                             transit_links=self.gateway_transit_links())
        spec = st["spec"]
        drift = self.ssh_port_drift()
        owner = self.ssh_owner()
        return {"enabled": st["enabled"], "present": st["present"],
                "rollback": st["rollback"], "ufw": st["ufw"], "firewalld": st.get("firewalld", False),
                "listening": drift["listening"], "drift": drift["drift"],
                "owner": owner.kind, "owner_where": owner.where, "owner_port": owner.port,
                "owner_detail": owner.detail, "owner_files": list(owner.files),
                "sshd_down": drift["listening"] is None,
                "ssh_port": spec.ssh_port, "allow": list(spec.ssh_allow4) + list(spec.ssh_allow6),
                "raw_allow": list(settings.get("app.firewall.ssh_allow", []) or []),
                "unresolved": list(spec.unresolved), "held": list(spec.ssh_held),
                "admin_ips": list(spec.tunnel_admin4),
                "nat": spec.nat, "bot_username": self.bot_username or ""}

    # ── порт SSH (кнопка «Изменить порт») ────────────────────────────────────
    # Порт живёт в conf (его фильтрует таблица) и в sshd; из чата меняются оба.
    # Порядок: сначала conf — хук on_change пересобирает таблицу, и к моменту
    # рестарта sshd новый порт уже открыт; sshd не принял — conf возвращаем.
    # Таймера отката нет намеренно: текущие SSH-сеансы рестарт не рвёт, а
    # вернуть порт можно этой же кнопкой — чат от SSH не зависит.

    def ssh_port_busy(self, port: int) -> str:
        """Имя процесса, который слушает порт («?» — есть сокет, имя не
        видно); пусто — свободен. Свой текущий порт — тоже «занят»: sshd на
        нём и стоит."""
        from awgbot.infra import sshd
        if not sshd.valid_port(port):
            raise ServiceError("порт — число от 1 до 65535")
        try:
            return sshd.port_busy(int(port))
        except sshd.SshdError as e:
            raise ServiceError(str(e))

    def ssh_owner(self):
        """Кто владеет sshd_config на сервере (OMV здесь не бывает, но
        cloud-образы и конфиг-менеджеры — да). Живьём: дёшево (чтение файлов)."""
        from awgbot.infra import sshd
        return sshd.owner()

    def ssh_port_change(self, port: int) -> int:
        """Перевести sshd и фильтр на порт. Возвращает прежний порт. Конфигом
        sshd владеет другая программа — отказ: она перепишет файл при первой
        своей генерации, и порт вернётся."""
        from awgbot.infra import sshd
        port = int(port)
        if not sshd.valid_port(port):
            raise ServiceError("порт — число от 1 до 65535")
        old = int(settings.get_int("app.network.ssh_port", config.SSH_PORT))
        owner = sshd.owner()
        if owner:
            from awgbot.domain.gwssh import SshOwnerRefusal
            try:
                listening = (sshd.listening_ports() or [None])[0]
            except sshd.SshdError:
                listening = None
            raise SshOwnerRefusal(owner, listening)
        if port == old:
            raise ServiceError(f"порт {port} уже выбран текущий")
        busy = self.ssh_port_busy(port)
        if busy:
            raise ServiceError(f"порт {port} уже занят процессом {busy}")
        settings.set_value("app.network.ssh_port", port)
        try:
            for line in sshd.set_port(port):
                log.info("sshd: %s", line)
        except sshd.SshdError as e:
            settings.set_value("app.network.ssh_port", old)
            raise ServiceError(str(e))
        return old

    @contextlib.contextmanager
    def _own_settings_change(self):
        """Правка firewall.* из сервиса: таблицу он применит сам, хуку
        планировщика (settings.on_change) второй раз её не применять."""
        self.__dict__["_fw_own_change"] = True
        try:
            yield
        finally:
            self.__dict__["_fw_own_change"] = False

    def firewall_settings_changed_externally(self) -> bool:
        """Для хука планировщика: правка не наша (руками в conf, awg-bot
        firewall …) — применить таблицу; свою домен уже применил."""
        return not self.__dict__.get("_fw_own_change", False)

    def firewall_allow_add(self, raw: str) -> list[str]:
        """Добавить адреса в вайтлист SSH. Добавление запереть не может —
        применяем без таймера отката."""
        from awgbot.infra import nftguard
        entries: list[str] = []
        for tok in str(raw).replace(",", " ").split():
            try:
                nftguard.classify(tok)
            except ValueError as e:
                raise ServiceError(f"«{tok}» не адрес, не подсеть и не имя ({e}). Попробуй ещё раз")
            if tok not in entries:
                entries.append(tok)
        if not entries:
            raise ServiceError("пусто: жду адрес, подсеть или имя")
        cur = list(settings.get("app.firewall.ssh_allow", []) or [])
        cur += [e for e in entries if e not in cur]
        with self._own_settings_change():
            settings.set_value("app.firewall.ssh_allow", cur)
        if nftguard.enabled():
            self.reconcile_ssh_access()
        self._channel_touch()                              # ADMIN_IPS — шлюзам сразу
        return cur

    def firewall_allow_remove(self, entry: str) -> list[str]:
        """Убрать адрес — сразу, без таймера: из чата любое действие
        отменяется здесь же, а чат от SSH не зависит. Последний адрес при
        включённом фильтре — отказ: пустой список открывает SSH снаружи всем
        (как и включение без адресов, которого нет)."""
        from awgbot.infra import nftguard
        cur = [v for v in (settings.get("app.firewall.ssh_allow", []) or []) if v != entry]
        if not cur and nftguard.enabled():
            raise ServiceError("это последний адрес: без адресов фильтр открывает SSH снаружи всем. "
                               "Сначала выключи «✅ Фильтр снаружи»")
        with self._own_settings_change():
            settings.set_value("app.firewall.ssh_allow", cur)
        if nftguard.enabled():
            self._firewall_apply(rollback=False)
        self._channel_touch()                              # ADMIN_IPS — шлюзам сразу
        return cur

    _FW_ROLLBACK_SECONDS = 300

    def _firewall_apply(self, rollback: bool) -> None:
        from awgbot.infra import nftguard
        spec = nftguard.build_spec(self.db.admin_device_addresses(config.ADMIN_ID),
                                   channel_extra=self.gwcfg_channel_ports()[1:],
                                   transit_links=self.gateway_transit_links())
        for line in nftguard.ensure_persistence():
            log.info("firewall: %s", line)
        nftguard.apply_text(nftguard.render(spec))
        if not rollback:
            return
        env = {k: str(v) for k, v in (("AWG_BOT_CONF_DIR", config.CONF_DIR),
                                      ("AWG_BOT_DATA_DIR", config.DATA_DIR),
                                      ("AWG_BOT_ENV", os.environ.get("AWG_BOT_ENV", "")))
               if v}
        cmd = ["-p", f"WorkingDirectory={config.BASE_DIR}", sys.executable,
               "-m", "tools.firewall", "rollback"]
        nftguard.arm_rollback(self._FW_ROLLBACK_SECONDS, cmd, env)

    def firewall_enable(self) -> None:
        """Включить фильтр — сразу, без таймера отката (он остался у CLI, где
        кроме терминала другого пути нет; из чата фильтр выключается той же
        кнопкой). Без адресов не включаем: такой фильтр открывает SSH всем и
        не фильтрует ничего — кнопки в разделе тоже нет."""
        from awgbot.infra import nftguard
        if not (settings.get("app.firewall.ssh_allow", []) or []):
            raise ServiceError("сначала добавь хотя бы один адрес для входа снаружи")
        with self._own_settings_change():
            settings.set_value("app.firewall.enabled", True)
        try:
            self._firewall_apply(rollback=False)
        except nftguard.GuardError as e:
            with self._own_settings_change():
                settings.set_value("app.firewall.enabled", False)
            raise ServiceError(str(e))
        self._smb_tunnel_follow()

    def firewall_confirm(self) -> bool:
        from awgbot.infra import nftguard
        return nftguard.disarm_rollback()

    def firewall_disable(self) -> list[str]:
        """Снять фильтр. NAT пользователей таблица держит дальше: «выключить
        файервол» не обещает «оставить пользователей без интернета»."""
        from awgbot.infra import nftguard
        nftguard.disarm_rollback()
        done = nftguard.remove(transit_links=self.gateway_transit_links())
        with self._own_settings_change():
            settings.set_value("app.firewall.enabled", False)
        self._smb_tunnel_follow()
        return done

    def _smb_tunnel_follow(self) -> None:
        """«SMB в туннеле» держит записи только при фильтре снаружи: без него
        имена увидели бы все пользователи — снять или вернуть сразу, не тактом."""
        sync = getattr(self, "smb_tunnel_sync", None)
        if sync is None:
            return
        try:
            sync()
        except Exception as e:                            # noqa: BLE001
            log.warning("SMB в туннеле: записи за фильтром не подтянуты: %s", e)

    # ── порт как факт на сервере: сверка, не следование ──────────────────────
    # Владелец конфига здесь — бот; расхождение порта sshd с conf почти всегда
    # ошибка человека (правил sshd_config руками при включённом фильтре — и
    # запер себя молча). Поэтому предупреждаем, а не следуем; следуем только
    # при найденном чужом владельце (cloud-init порт не трогает, а конфиг-
    # менеджер — может), как агент шлюза.
    _SSH_DRIFT_KEY = "ssh_port_drift_seen"

    def ssh_port_drift(self) -> dict:
        """{'listening': int|None, 'conf': int, 'ports', 'drift'} — расхождения
        нет, если sshd слушает порт из conf или не запущен."""
        from awgbot.infra import sshd
        conf = int(settings.get_int("app.network.ssh_port", config.SSH_PORT))
        try:
            ports = sshd.listening_ports()
        except sshd.SshdError:
            ports = []
        listening = conf if conf in ports else (ports[0] if ports else None)
        return {"listening": listening, "conf": conf, "ports": ports,
                "drift": listening is not None and listening != conf}

    def ssh_port_drift_notes(self) -> list:
        """Тик: порт sshd ≠ conf → при чужом владельце следовать (conf →
        хук пересобирает таблицу), иначе — предупредить один раз на значение."""
        from awgbot.domain.services import Notification
        from awgbot.infra import nftguard, sshd
        d = self.ssh_port_drift()
        if not d["drift"] or not nftguard.enabled():
            # фильтр выключен — никакой порт не заперт, предупреждать не о чем
            if self.db.get_state(self._SSH_DRIFT_KEY):
                self.db.set_state(self._SSH_DRIFT_KEY, "")
            return []
        seen = self.db.get_state(self._SSH_DRIFT_KEY) or ""
        if seen == str(d["listening"]):
            return []
        self.db.set_state(self._SSH_DRIFT_KEY, str(d["listening"]))
        owner = sshd.owner()
        if owner:
            settings.set_value("app.network.ssh_port", int(d["listening"]))
            who = "OMV" if owner.kind == "omv" else "другой процесс"
            return [Notification(config.ADMIN_ID,
                                 f"🛡 Порт SSH на сервере изменился: <code>{d['conf']}</code> → <code>{d['listening']}</code> "
                                 f"(задал {who}). Фильтр переведён на новый порт. Если снаружи стоит "
                                 "файервол провайдера — открой в нём новый порт", critical=False)]
        return [Notification(config.ADMIN_ID,
                             f"⚠️ <code>sshd</code> слушает порт <code>{d['listening']}</code>, а фильтр держит "
                             f"<code>{d['conf']}</code> — вход снаружи и из туннеля закрыт. Нажми «🅿️ Изменить порт» → "
                             f"<code>{d['listening']}</code> или верни <code>sshd</code> на <code>{d['conf']}</code>", critical=True)]

    _SSH_OPEN_KEY = "ssh_open_unresolved_seen"

    def firewall_open_notes(self) -> list:
        """Тик: фильтр включён, в списке только имена, ни одно не резолвится,
        и прежних адресов в файле таблицы нет — таблица открыла SSH снаружи
        всем (запереть админа хуже, чем открыть). Сказать один раз на состав
        имён; адреса появились — отметку снять. Резолв — тот же, что у
        сверки таблицы (кэш), остального файервола не собираем."""
        from awgbot.domain.services import Notification
        from awgbot.infra import nftguard
        raw = list(settings.get("app.firewall.ssh_allow", []) or [])
        bad: list[str] = []
        if raw and nftguard.enabled():
            a = nftguard.ssh_allow(raw)
            if not a["v4"] and not a["v6"]:
                bad = list(a["bad"])
        seen = self.db.get_state(self._SSH_OPEN_KEY) or ""
        if not bad:
            if seen:
                self.db.set_state(self._SSH_OPEN_KEY, "")
            return []
        key = ",".join(sorted(bad))
        if seen == key:
            return []
        self.db.set_state(self._SSH_OPEN_KEY, key)
        log.warning("firewall: ни одно имя списка SSH не резолвится (%s), прежних адресов нет — SSH снаружи открыт всем",
                    ", ".join(bad))
        names = ", ".join(f"<code>{html.escape(n)}</code>" for n in bad[:5]) + (f" [+{len(bad) - 5}]" if len(bad) > 5 else "")
        return [Notification(config.ADMIN_ID,
                             f"⚠️ Фильтр снаружи: имена из списка адресов не резолвятся ({names}), прежних адресов нет — "
                             "SSH снаружи открыт всем (только по SSH-ключам). Проверь DNS сервера или добавь адрес: "
                             "⚙️ Настройки → 🛡 SSH-доступ → «➕ Адрес»", critical=False)]

    def retire_legacy_ssh_gate(self) -> None:
        """Разово снять прежние ворота (цепочка AWGBOT_SSH + PostUp-страж) —
        но ТОЛЬКО когда новая таблица уже держит SSH. Иначе снятие открыло бы
        SSH из туннеля всем пирам до включения файервола."""
        from awgbot.infra import nftguard
        if not nftguard.enabled() or self.db.get_state("legacy_ssh_gate_removed"):
            return
        try:
            if awg.remove_legacy_ssh_gate():
                log.info("firewall: старые ворота SSH (AWGBOT_SSH, PostUp) сняты")
        except awg.AwgError as e:
            log.warning("firewall: старые ворота не сняты: %s", e)
            return
        self.db.set_state("legacy_ssh_gate_removed", "1")
