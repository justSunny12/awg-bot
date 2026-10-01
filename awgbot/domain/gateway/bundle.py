"""bundle.py — файл конфигурации из чата, настройки с сервера по каналу, резервная копия."""

from __future__ import annotations

import html
import json
import re
import socket
import time
from awgbot.core import config, settings
from awgbot.util import timeutil
from awgbot.domain.services import ServiceError
from awgbot.util import gwlink  # noqa: E402
from awgbot.domain.gateway import base
from awgbot.domain.gateway.base import log


class BundleMixin:
    """Файл конфигурации из чата, настройки с сервера по каналу, резервная копия."""
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

    def reassert(self) -> tuple[bool, str]:
        """Полный реассерт: рестарт юнита шлюза — тот зовёт gw-скрипт, который
        идемпотентно переставляет правила и переподнимает линк. Под общим
        замком применения: параллельно с файлом конфигурации не дёргаем."""
        return self.reassert_guarded("кнопка «Восстановить»")

    _BACKUP_B64_RE = re.compile(r'^BACKUP_B64="([A-Za-z0-9+/=]+)"', re.M)

    def _bundle_plain(self, blob: bytes) -> str:
        from awgbot.util import bundlecrypt
        priv = bundlecrypt.read_privkey(base.pathlib_read(config.GW_LINK_CONF))
        return bundlecrypt.decrypt(blob, priv).decode(errors="replace")

    def _bundle_passphrase(self, text: str) -> str:
        m = self._BACKUP_B64_RE.search(text)
        if not m:
            return ""
        import base64
        try:
            return str(json.loads(base64.b64decode(m.group(1)).decode()).get("passphrase") or "")
        except Exception:                                 # noqa: BLE001
            return ""

    def inspect_bundle(self, blob: bytes) -> dict:
        """Что везёт бандл, до применения: почту, фразу бэкапов, и отличается ли
        фраза от той, что уже задана здесь (тогда перезапись — с вопроса)."""
        try:
            text = self._bundle_plain(blob)
        except (OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        phrase = self._bundle_passphrase(text)
        mine = self.db.get_state(self._BK_PASSPHRASE_KEY) or ""
        return {"ok": True, "mail": bool(re.search(r'^MAIL_B64="', text, re.M)),
                "passphrase": bool(phrase),
                "passphrase_differs": bool(phrase and mine and phrase != mine),
                "link_changed": self._bundle_link_changed(text)}

    def _bundle_link_changed(self, text: str) -> bool:
        """Конфиг линка в бандле отличается от установленного? Тот же — скрипт
        обвязки линк не переподнимет, и предупреждать об обрыве не о чем."""
        m = re.search(r"<<'__LINK_CONF_EOF__'\n(.*?)\n__LINK_CONF_EOF__", text, re.S)
        if not m:
            return True
        try:
            current = base.pathlib_read(config.GW_LINK_CONF)
        except OSError:
            return True
        return m.group(1).strip() != current.strip()

    def apply_bundle(self, blob: bytes, overwrite_passphrase: bool = False) -> tuple[bool, str]:
        """Принять шифрованный бандл из чата: расшифровать ключом, производным от
        ТЕКУЩЕГО приватного ключа линка, проверить, что это наш бандл, применить.

        Порядок проверок важен: сначала шифр (не наш файл / не тот ключ), потом
        структура (маркеры контракта) — и только затем запуск. Бандл исполняется
        тем же путём, что и руками: sh bundle --apply; он сам перепишет
        линк-конфиг, переподнимет линк и юнит.
        """
        from awgbot.util import bundlecrypt
        self.db.set_state(self._BUNDLE_MAIL_KEY, "")   # итог по почте — только этого файла
        try:
            priv = bundlecrypt.read_privkey(base.pathlib_read(config.GW_LINK_CONF))
            plain = bundlecrypt.decrypt(blob, priv)
        except (OSError, ValueError) as e:
            return False, str(e)                      # причина — под общей маской «не применена:»
        text = plain.decode(errors="replace")
        if "#__GW_SETUP_BELOW__" not in text or "__LINK_CONF_EOF__" not in text:
            return False, "внутри нет маркеров контракта линка"
        # Старый файл из истории чата расшифровывается тем же ключом: отказываем
        # выпуску старее уже применённого (метка ISSUED_AT в шапке; файлы прежних
        # выпусков без метки применяются как раньше)
        issued = self._bundle_issued_at(text)
        applied = int(self.db.get_state(self._BUNDLE_ISSUED_KEY) or 0)
        if issued and applied and issued < applied:
            from datetime import datetime
            when = timeutil.fmt_dt_ui(datetime.fromtimestamp(issued, tz=timeutil.TZ))
            return False, (f"эта конфигурация выпущена {when} — старее уже применённой; "
                           "перевыпусти конфигурацию шлюза с сервера AWG")
        m = re.search(r'^SERVER_NAME="([^"\n]{1,64})"', text, re.M)
        if m:
            self.db.set_state(self._SERVER_NAME_KEY, m.group(1))
        mail = self._apply_bundle_mail(text)
        phrase = self._bundle_passphrase(text)
        if phrase and (overwrite_passphrase or not self.backup_encryption_enabled()):
            try:
                self.backup_set_passphrase(phrase)
            except ValueError as e:
                log.warning("gateway: фраза из бандла не принята: %s", e)
        with base._APPLY_LOCK:
            ok, out = self._apply_bundle_run(plain)
        if ok and issued:
            self.db.set_state(self._BUNDLE_ISSUED_KEY, str(issued))
        # Почта приехала — проверяем сразу (вход по IMAP и SMTP), итог — строкой
        # в сообщении об итоге; иначе ящик висел бы «ещё не проверялось» до
        # ручной кнопки, а первый бэкап молча ушёл бы в чат. Бэкапы ВПС на
        # почте (backup=email) — свои туда же, но только после удачной проверки
        # и при включённом шифровании: открытый архив по почте не ездит.
        note: dict = {}
        if mail is not None:
            try:
                ok_mail, why = self.email_check()
            except Exception as e:                           # noqa: BLE001 — таймаут IMAP, сеть
                ok_mail, why = False, str(e)
            note = {"state": "ok" if ok_mail else "fail", "why": "" if ok_mail else why, "backup": False}
            # бэкапы — на почту вслед за сервером: только при удачном применении
            # файла и удачной проверке, при шифровании и включённых автобэкапах,
            # и только если канал ещё не почта — выбор человека не перебиваем
            if (ok and ok_mail and mail.get("backup") == "email" and self.backup_encryption_enabled()
                    and settings.get_bool("app.scheduler.backup_enabled", True)
                    and str(settings.get("app.scheduler.backup_channel", "telegram") or "").lower() != "email"):
                try:
                    settings.set_value("app.scheduler.backup_channel", "email")
                    note["backup"] = True
                except Exception as e:                       # noqa: BLE001
                    log.warning("gateway: канал бэкапов не переключён на почту: %s", e)
        self.db.set_state(self._BUNDLE_MAIL_KEY, json.dumps(note, ensure_ascii=False) if note else "")
        return ok, out

    _BUNDLE_ISSUED_KEY = "gw_bundle_issued_at"
    _BUNDLE_MAIL_KEY = "gw_bundle_mail"           # итог по почте из последнего файла: {state, why, backup} | пусто

    def bundle_mail_check(self) -> dict:
        """{state: ok|fail, why, backup} — что показала проверка почты из
        последнего применённого файла и переключились ли бэкапы на e-mail;
        пусто — почты в файле не было."""
        raw = self.db.get_state(self._BUNDLE_MAIL_KEY) or ""
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    @staticmethod
    def _bundle_issued_at(text: str) -> int:
        m = re.search(r"^# ISSUED_AT: (\d{9,11})$", text, re.M)
        return int(m.group(1)) if m else 0

    @staticmethod
    def _apply_bundle_run(plain: bytes) -> tuple[bool, str]:
        import os, tempfile
        fd, path = tempfile.mkstemp(prefix="awg-gw-bundle-", suffix=".sh", dir="/root")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(plain)
            # 600, как у настроек из канала: первое включение режима без VPN
            # ставит dnsmasq через apt, на малине это минуты
            proc = base._run(base._bundle_argv(path), timeout=600)
            out = (base._out(proc) + proc.stderr.decode(errors="replace")).strip()
            if proc.returncode == 124:
                # sh убит по таймауту, а скрипт (apt) ещё работает — под
                # systemd-run вне нашего cgroup он доработает сам
                out += ("\nприменение не уложилось в 10 минут — обвязка ещё работает, "
                        "проверь 🩺 Здоровье через несколько минут")
            tail = "\n".join(out.splitlines()[-6:])
            return proc.returncode == 0, tail
        finally:
            try:
                os.unlink(path)               # внутри приватный ключ — не оставляем
            except OSError:
                pass


    # ── настройки с сервера по каналу ────────
    def apply_link_settings(self, raw: dict) -> dict:
        """Применить настройки, присланные сервером по каналу.

        Возвращает {"ok", "changed": [ключи], "error"}. Без подтверждения
        человека: в канале нет ни кода, ни секретов, а кнопка «ок» без
        содержания учит нажимать не глядя; о факте человек узнаёт уведомлением.

        Совпало с тем, что уже в юните, — ничего не трогаем: первая доставка
        после включения канала на неизменённой системе не рестартит обвязку
        вовсе. Разошлось — переписываем строки юнита и перезапускаем его; скрипт
        обвязки идемпотентен и линк не трогает, если конфиг линка тот же, так
        что сессия канала переживает применение настроек, которые сама принесла.
        Отказ — юнит возвращается к прежнему тексту и перезапускается: лучше
        жить со старыми настройками, чем с половиной новых.
        """
        from awgbot.infra import gwguard
        from awgbot.util import gwlink
        try:
            want = gwlink.validate_settings(raw)
        except gwlink.ProtocolError as e:
            return {"ok": False, "changed": [], "error": str(e)}
        current = {k: " ".join(gwguard.unit_env(k).split()) for k in gwlink.SETTINGS_KEYS}
        changed = [k for k in gwlink.SETTINGS_KEYS if want[k] != current[k]]
        if not changed:
            return {"ok": True, "changed": [], "error": ""}
        # Замок — с ожиданием: файл конфигурации из чата и настройки из канала
        # применяются друг за другом, не вперемешку. Юнит в activating (запущен
        # извне: загрузка, тик) — ждём, пока доработает, а не перезапускаем поверх:
        # включение режима без VPN стоит на apt минуты, рестарт убил бы dpkg.
        with base._APPLY_LOCK:
            if self._unit_settled(120) == "activating":
                return {"ok": False, "changed": changed, "error": self.BUSY_ACTIVATING, "retry": True}
            try:
                before = gwguard.unit_set_env({k: want[k] for k in changed})
            except (OSError, gwguard.GwGuardError) as e:
                return {"ok": False, "changed": changed, "error": f"юнит не переписан ({e})"}
            # Запас на включение режима без VPN: скрипт ставит dnsmasq через apt,
            # а на малине это минуты, не секунды.
            self._last_reassert = time.monotonic()
            ok, err = gwguard.reassert(timeout=600)
            if not ok and gwguard.is_timeout(err):
                # systemctl не дождался, а юнит ещё работает: откатный рестарт
                # сейчас — ещё один рестарт идущего задания. Ждём, чем кончится.
                log.warning("gateway: настройки с сервера применяются дольше обычного: %s", err)
                state = self._unit_settled(300)
                if state == "activating":
                    # всё ещё работает (apt ждёт замок dpkg у OMV): откат поверх —
                    # то самое, от чего эта ветка защищает; юнит доработает сам,
                    # снимок покажет серверу итог, и тот повторит доставку
                    return {"ok": False, "changed": changed, "error": self.STILL_APPLYING, "retry": True}
                ok = state == "active"
                err = err if not ok else ""
            if ok:
                self.invalidate_static()
                return {"ok": True, "changed": changed, "error": ""}
            log.warning("gateway: настройки с сервера не применились (%s) — откатываю", err)
            try:
                gwguard.unit_restore(before)
                gwguard.reassert(timeout=600)
            except (OSError, gwguard.GwGuardError) as e2:
                log.warning("gateway: откат юнита не удался: %s", e2)
            return {"ok": False, "changed": changed, "error": err or "обвязка не применилась"}

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

    def link_settings_note(self, result: dict) -> str:
        """Текст уведомления в чат агента о настройках, пришедших по каналу."""
        what = ", ".join(gwlink.KEY_HUMAN.get(k, k) for k in result.get("changed") or [])
        err = html.escape(str(result.get("error") or "ошибка"), quote=False)
        if result.get("ok"):
            return f"⚙️ Сервер AWG прислал новые настройки шлюза — применены: {what}"
        if result.get("retry"):
            # юнит ещё работает (apt, замок dpkg): отката не было, сервер повторит
            return (f"⏳ Сервер AWG прислал новые настройки шлюза ({what}) — применяются дольше "
                    "обычного, сервер повторит доставку сам")
        if not result.get("changed"):
            # отвергнуты ещё на проверке значений — ничего не менялось, и
            # «вернул прежние» было бы неправдой
            return f"⚠️ Сервер AWG прислал настройки шлюза, которые не прошли проверку: {err}. Ничего не менял."
        return (f"⚠️ Сервер AWG прислал новые настройки шлюза ({what}), но они не "
                f"применились: {err}. Вернул прежние.")

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
        """Подписанный ключом линка токен «я шлюз с таким аплинком»."""
        from awgbot.util import bundlecrypt, gwsign
        priv = bundlecrypt.read_privkey(base.pathlib_read(config.GW_LINK_CONF))
        return gwsign.sign(priv, "claim", pubkey, host=socket.gethostname())

    def gateway_apply_report(self) -> str:
        """Человеческий отчёт после применения — из статуса скрипта."""
        from awgbot.infra import gwguard
        from awgbot.bot import texts
        return texts.gateway_apply_report(gwguard.script_status())

    def gateway_mark_status(self) -> str:
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

    def _apply_bundle_mail(self, text: str):
        """MAIL_B64 из файла конфигурации → настройки почты агента (креды в БД,
        серверы в conf). Возвращает принятый словарь (в нём и backup — канал
        бэкапов ВПС) или None: строки нет или она не принята — своё не трогаем."""
        m = re.search(r'^MAIL_B64="([A-Za-z0-9+/=]+)"', text, re.M)
        if not m:
            return None
        import base64
        try:
            d = json.loads(base64.b64decode(m.group(1)).decode())
            self.email_save(d["login"], d["password"], d["imap_host"], int(d["imap_port"]),
                            d["smtp_host"], int(d["smtp_port"]))
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway: почта из файла конфигурации не принята: %s", e)
            return None
        return d
