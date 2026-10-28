"""
guest.py — часть пакета reserve (см. __init__): бот основы на резерве
(гостевой режим, `config.GUEST`).

- шифрованные снимки `rep.bin`: тот же крючок коммита базы, что ставит
  отметку пакета у основы, здесь ставит отметку «снимок устарел» — любая
  запись, и счётчики тоже; такт пишет снимок базы под `K` из хранилища гостя
  (ключ в памяти, на диске — только шифротекст);
- состояние основы глазами резерва — из локального канала с ботом резерва
  (`runtime/pairlink/guestctl`): для Т-РГ1, Т-РГ3 и кнопки возврата;
- отказы хостовых сервисов: файервол хоста, SSH-доступ, резолвер админа,
  адреса в туннеле, шлюзы, бэкапы, обновления, переезд профилей — методы из
  HOST_ONLY в гостевом режиме отвечают ServiceError (сторож-тест по списку).
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time

from awgbot.core import config
from awgbot.util import timeutil

from ..types import Notification

from ..types import ServiceError
from ..gateway_link.common import log

SNAPSHOT_BATCH_SECONDS = 30
GUEST_REFUSED = "На резерве это недоступно — вернётся вместе с пользователями"
DEVICE_CONFIG_LATER = ("📄 Конфигурацию этого устройства покажу, когда сервер вернётся в строй. VPN на нём "
                       "работает, как прежде")

# методы Services, которые действуют на хост и в гостевом режиме отказывают
HOST_ONLY: tuple[str, ...] = (
    "reconcile_ssh_access", "ssh_port_change", "ssh_port_drift_notes", "firewall_enable", "firewall_disable",
    "firewall_confirm", "firewall_allow_add", "firewall_allow_remove", "retire_legacy_ssh_gate",
    "routing_provision", "routing_update_lists", "check_resource_alerts", "private_dns_decision",
    "gwcfg_doc", "gwlink_lan_feeds_update",
    "make_backup", "backup_set_passphrase", "restore_wait_start",
    "fleet_start", "fleet_run", "apply_update", "migration_prepare", "migration_start", "migration_finish",
    "reserve_invite_issue", "reserve_accept_go", "reserve_unpair", "reserve_new_generation",
)


class ReserveGuestMixin:
    @property
    def guest(self) -> bool:
        return bool(config.GUEST)

    # ── отказы хостовых сервисов ────────────────────────────────────────────
    def guest_install_refusals(self) -> list[str]:
        """Подменить методы HOST_ONLY отказом; вернуть имена, которых у класса
        нет (сторож-тест ждёт пустой список)."""
        missing = []
        for name in HOST_ONLY:
            if not hasattr(type(self), name):
                missing.append(name)
                continue
            self.__dict__[name] = self._guest_refuser(name)
        return missing

    @staticmethod
    def _guest_refuser(name: str):
        def refuse(*_a, **_k):
            raise ServiceError(GUEST_REFUSED)
        refuse.__name__ = name
        return refuse

    # ── снимки ──────────────────────────────────────────────────────────────
    def guest_snapshot_hook_install(self) -> None:
        self.db.set_commit_hook(self._guest_snapshot_touch)
        self.__dict__["_guest_snap_due"] = time.monotonic()

    def _guest_snapshot_touch(self, kind: str) -> None:
        if self.__dict__.get("_guest_snap_due") is None:   # первая запись после снимка — начало пачки
            self.__dict__["_guest_snap_due"] = time.monotonic()

    def guest_snapshot_mark(self) -> None:
        self.__dict__["_guest_snap_due"] = time.monotonic()

    def guest_snapshot_due(self) -> bool:
        due = self.__dict__.get("_guest_snap_due")
        return due is not None and time.monotonic() - due >= SNAPSHOT_BATCH_SECONDS

    def guest_snapshot_tick(self, force: bool = False) -> bool:
        """Снимок базы под `K` в `rep.bin`, если отметка старше такта (или force). С журналом
        снимков нет: на диске — журнал данных гостя."""
        if self.guest_jrn_on():
            return False
        if not force and not self.guest_snapshot_due():
            return False
        try:
            blob = self.guest_snapshot_bytes()
        except (OSError, ValueError, sqlite3.Error) as e:
            log.warning("гость: снимок не собрался: %s", e)
            return False
        from awgbot.infra import rsvzone
        try:
            rsvzone.write_private(rsvzone.REP_PATH, blob)
        except OSError as e:
            log.warning("гость: снимок не записан: %s", e)
            return False
        self.__dict__["_guest_snap_due"] = None
        self.__dict__["_guest_snap_at"] = timeutil.now_iso()
        return True

    def guest_snapshot_bytes(self) -> bytes:
        """Шифротекст снимка: вся база гостя (с историей с момента переезда)
        под `K` из хранилища; `pkg_gen` — из отметки процесса открытия."""
        from awgbot.infra import rsvzone
        from awgbot.util.rsvpkg import seal
        k = bytes.fromhex(rsvzone.K_FILE.read_text(encoding="utf-8").strip())
        meta = rsvzone.read_json(rsvzone.RUN_DIR / "pkg.json")
        obj = {"pkg_gen": int(meta.get("pkg_gen") or 0), "at": timeutil.now_iso(), "db": self._guest_db_bytes().hex()}
        return seal.snapshot_seal(k, obj)

    def _guest_db_bytes(self) -> bytes:
        fd, tmp = tempfile.mkstemp(prefix="rsv-snap-", suffix=".db", dir=str(config.DATA_DIR))
        os.close(fd)
        try:
            src = sqlite3.connect(str(config.DB_PATH))
            try:
                dst = sqlite3.connect(tmp)
                try:
                    src.backup(dst)
                    dst.execute("PRAGMA journal_mode = DELETE")
                    dst.commit()
                finally:
                    dst.close()
            finally:
                src.close()
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:
                    os.unlink(tmp + suffix)
                except OSError:
                    pass

    def guest_snapshot_at(self) -> str:
        return str(self.__dict__.get("_guest_snap_at") or "")

    # ── состояние от бота резерва ───────────────────────────────────────────
    def guest_state_in(self, body: dict) -> None:
        """`state` из локального канала: имя резерва, с какого момента несёт
        нагрузку, причина, состояние основы, рычаг возврата."""
        data = {k: body.get(k) for k in ("name", "since", "reason", "primary", "return", "self_host", "own_name",
                                          "rf_via", "rf_cidr")
                if k in body}
        data["at"] = timeutil.now_iso()
        self.__dict__["_guest_state"] = data

    def guest_state(self) -> dict:
        data = dict(self.__dict__.get("_guest_state") or {})
        if not data.get("own_name"):
            data["own_name"] = str(config.SERVER_NAME or "")
        return data

    def guest_devices_online(self) -> int:
        try:
            return len(self.online_devices())
        except Exception:                                     # noqa: BLE001
            return 0

    def guest_screen_data(self) -> dict:
        d = self.guest_state()
        d["online"] = self.guest_devices_online()
        d["snapshot_at"] = self.guest_snapshot_at()
        # запись и адрес основы — из её же настроек (гость — бот основы): Т-Р11 называет, куда вернётся запись
        try:
            d["record"] = self.reserve_dns_name()
            d["own_ip"] = self.reserve_own_ip() if d["record"] else ""
        except Exception:                                     # noqa: BLE001
            d["record"], d["own_ip"] = "", ""
        return d

    # ── сторона бота резерва: что сказать гостю ─────────────────────────────
    def reserve_guest_state_view(self) -> dict:
        """Состояние основы глазами резерва — гостю по локальному каналу:
        имя резерва и его адрес, с какого момента и почему несу нагрузку,
        основа жива или нет, рычаг возврата."""
        from awgbot.core import settings
        from ..selfhost import own_host
        inn = self.rsv_get("rsv_in")
        seen = self.reserve_peer_seen()
        ps = self.reserve_peer_state()
        primary = {"status": "dead" if seen == "down" else ("alive" if seen == "ok" else "suspect"),
                   "since": self.reserve_down_since() if seen == "down" else "",
                   "healthy": bool(ps.get("healthy")), "can_return": bool(ps.get("can_return"))}
        rf = self.reserve_rf_via_view() if hasattr(self, "reserve_rf_via_view") else {}
        return {"name": str(config.SERVER_NAME or ""), "self_host": own_host(),
                "since": str(inn.get("since") or ""), "reason": str(inn.get("why") or inn.get("reason") or "dead"),
                "primary": primary, "return": str(settings.get("app.reserve.return", "immediate") or "immediate"),
                "own_name": self.reserve_peer_name(), "rf_via": bool(rf.get("rf_via")), "rf_cidr": str(rf.get("rf_cidr") or "")}

    def reserve_guest_webhook_in(self, body: dict) -> list[tuple]:
        """Слово гостя о надзоре вебхука: застой доставки — Т-РУ19 админу резерва
        (один раз на расхождение), восстановление — 🟢."""
        from awgbot.domain.notices import reserve as rn
        from awgbot.infra import rsvzone
        cur = self.rsv_get("rsv_in")
        stalled = bool(body.get("stalled"))
        if stalled == bool(cur.get("webhook_stalled")):
            return []
        self.rsv_update("rsv_in", webhook_stalled=stalled)
        name = self.reserve_peer_name()
        if stalled:
            try:
                port = int(rsvzone.read_json(rsvzone.WEBHOOK_FILE).get("port") or 0)
            except Exception:                                 # noqa: BLE001
                port = 0
            text = rn.webhook_stalled_reserve(name, str(body.get("since") or ""), str(body.get("error") or ""), port)
        else:
            text = rn.webhook_restored_reserve(name)
        return [("notify", [Notification(tg_id=int(config.ADMIN_ID), text=text, silent=True)])]

    GUEST_BLOCKS_RESEND_S = 120

    def guest_blocks_to_send(self) -> list[str] | None:
        """Адреса заблокированных устройств — основе (её оставшимся сессиям, пока она следует):
        при изменении и раз в GUEST_BLOCKS_RESEND_S (сессия пары могла потерять прошлое);
        None — слать нечего. С журналом не шлются: блокировки едут основе событиями."""
        import time
        if self.guest_jrn_on():
            return None
        addrs = sorted({str(d.address) for d in self.db.list_all_devices() if getattr(d, "block_reason", 0) and d.address})
        sent = self.__dict__.get("_guest_blocks_sent")
        if sent is not None and sent[0] == addrs and time.monotonic() - sent[1] < self.GUEST_BLOCKS_RESEND_S:
            return None
        return addrs

    def guest_blocks_sent(self, addrs: list[str]) -> None:
        import time
        self.__dict__["_guest_blocks_sent"] = (list(addrs), time.monotonic())

    def guest_apply_blocks(self, addrs: list[str]) -> None:
        """Блокировки основы (её живые сессии): DROP в зоне тем же механизмом."""
        from awgbot.infra import awg
        mine = {str(d.address) for d in self.db.list_all_devices() if getattr(d, "block_reason", 0)}
        for a in addrs:
            if a not in mine:
                try:
                    awg.block_ip(str(a))
                except Exception as e:                        # noqa: BLE001
                    log.info("гость: блокировка %s не наложена: %s", a, e)

    def guest_adopt_snapshot(self) -> bool:
        """На старте: `rep.bin` новее базы хранилища (реплика при передаче
        бота или снимок до перезагрузки резерва) — база из него."""
        from awgbot.infra import rsvzone
        from awgbot.util.rsvpkg import seal
        if self.guest_jrn_on():
            return False                                      # база собрана процессом открытия по журналу
        try:
            blob = rsvzone.REP_PATH.read_bytes()
            k = bytes.fromhex(rsvzone.K_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        try:
            snap = seal.snapshot_open(k, blob)
        except seal.PackageError as e:
            log.warning("гость: rep.bin не открылся: %s", e)
            return False
        meta = rsvzone.read_json(rsvzone.RUN_DIR / "pkg.json")
        if not isinstance(snap, dict) or str(snap.get("at") or "") <= str(meta.get("db_at") or ""):
            return False
        # живая база в хранилище новее снимка (перезапуск гостя юнитом, не возврат) — снимок
        # откатил бы правки последних секунд; сравнение по времени записи файла базы
        try:
            from datetime import datetime, timezone
            db_mtime = datetime.fromtimestamp(os.path.getmtime(str(config.DB_PATH)), tz=timezone.utc)
            snap_at = timeutil.parse_iso(str(snap.get("at") or ""))
            older = snap_at < db_mtime
        except (OSError, ValueError, TypeError):
            older = False
        if older and str(meta.get("db_at") or ""):
            log.info("гость: rep.bin от %s старее живой базы — не подхватываю", snap.get("at"))
            return False
        try:
            db_bytes = bytes.fromhex(str(snap.get("db") or ""))
        except ValueError:
            return False
        if not db_bytes:
            return False
        path = str(config.DB_PATH)
        try:
            with open(path, "rb") as f:
                rsvzone.write_private(rsvzone.RUN_DIR / "data" / "bot.db.prev", f.read())
            rsvzone.write_private(rsvzone.RUN_DIR / "data" / "bot.db", db_bytes)
            for suffix in ("-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except OSError:
                    pass
            meta["db_at"] = str(snap.get("at") or "")
            rsvzone.write_private(rsvzone.RUN_DIR / "pkg.json", json.dumps(meta))
        except OSError as e:
            log.warning("гость: база из rep.bin не подменена: %s", e)
            return False
        log.info("гость: база взята из снимка rep.bin от %s", snap.get("at"))
        return True

    def guest_can_return(self) -> bool:
        """«↩️ Вернуть пользователей» — когда основа сама сказала резерву, что здорова
        и ждёт кнопки (рычаг «по кнопке»): признак идёт в `st` и дальше гостю."""
        d = self.guest_state()
        return bool((d.get("primary") or {}).get("can_return"))
