"""
package.py — часть пакета reserve (см. __init__): пакет переезда — отметки,
сборка по событию и по таймеру, снимки `base-<gen>`, приём на стороне резерва.

Основа: любая запись в данные пользователей (крючок коммита базы с видом `data`,
`infra/db/core.py`) ставит отметку в памяти; такт пары собирает пакет, если с
отметки прошло ≥ 10 с (пачка изменений — одним пакетом) и линк пары жив, или
по таймеру `pkg_minutes` ± 40 % — но только если логический отпечаток
содержимого изменился. Пакет уходит частями по каналу; отметка снимается
только по `pkg_ok`. Снимок реплики, ушедший в пакете, лежит в
`reserve/out/base-<gen>.db` до подтверждения следующего — по нему считается
слияние при возврате (§3.11).

Резерв: части складываются в сессии, целый пакет проверяется по отпечатку
(заголовок открыт, ключей нет), кладётся в `reserve/guest.pkg` (0600) вместе
с долей `M` в `rsv_pkg_in`; ответ — `pkg_ok` / `pkg_bad`.

Журнал (`jrn1` у обеих сторон, journal/ship): пакет — снимок `pkg3`. Запись в
данные пользователей — событие журнала, а не отметка пакета; таймер собирает
снимок, только если изменились части вне базы или пора плановый снимок
(`reserve_jrn_snap_due`); в снимке — часть `jrn` (линия, эпоха, номер
последнего учтённого события и дельты счётчиков, из той же копии базы), её
открытая копия идёт в `pkg{jrn}` — резерв по ней снимает учтённое. Базы
слияния `base-<gen>.db` нет: возврат идёт без слияния.
"""
from __future__ import annotations

import hashlib
import os
import random
import time

from awgbot.core import config, settings
from awgbot.util import pairproto, rsvjrn, timeutil
from awgbot.util.rsvpkg import bot as pkgbot, seal, vpn as pkgvpn

from ..gateway_link.common import log
from .state import HOSTING, _LOCK

PKG_BATCH_SECONDS = 10
PKG_PART = "vpn"


def reserve_dir() -> str:
    return os.path.join(str(config.DATA_DIR), "reserve")


def guest_pkg_path() -> str:
    return os.path.join(reserve_dir(), "guest.pkg")


class ReservePackageMixin:

    # ── отметки ─────────────────────────────────────────────────────────────

    def reserve_pkg_hook_install(self) -> None:
        """Крючок коммита базы — ставит отметку в памяти (в базу из крючка не
        пишем: это снова коммит и снова крючок)."""
        self.db.set_commit_hook(self._reserve_pkg_touch)

    def _reserve_pkg_touch(self, kind: str) -> None:
        if kind != "data":
            return
        if self.__dict__.get("_jrn_capture_on"):
            return                                            # журнал: правку пишет триггер событием, такт журнала её увидит
        self.__dict__["_rsv_pkg_due_at"] = time.monotonic()

    def reserve_pkg_mark(self) -> None:
        """Отметка руками (смена ключей, настройки из белого списка, старт)."""
        self.__dict__["_rsv_pkg_due_at"] = time.monotonic()

    def _pkg_timer_due(self) -> bool:
        nxt = self.__dict__.get("_rsv_pkg_timer_at")
        now = time.monotonic()
        if nxt is None:
            self.__dict__["_rsv_pkg_timer_at"] = now + self._pkg_timer_delay()
            return False
        if now >= nxt:
            self.__dict__["_rsv_pkg_timer_at"] = now + self._pkg_timer_delay()
            return True
        return False

    def _pkg_for_move(self) -> bool:
        """Переезд кнопкой на шаге `pkg` ждёт свежий пакет (`rsv_pkg_sent.at` не раньше
        начала переезда) — в `leaving` до этого шага такт пакета не молчит."""
        out = self.rsv_get("rsv_out")
        return out.get("state") == "leaving" and str(out.get("step") or "pkg") == "pkg"

    @staticmethod
    def _pkg_timer_delay() -> float:
        minutes = max(1, settings.get_int("app.reserve.pkg_minutes", 30))
        return minutes * 60 * random.uniform(0.6, 1.4)

    # ── сборка ──────────────────────────────────────────────────────────────

    def reserve_pkg_tick(self) -> dict | None:
        """Такт пары: пора ли слать пакет. Возвращает {gen, fp, blob (hex),
        share, m_gen} для отправки частями или None."""
        if not self.reserve_paired() or not self.reserve_online() or (self.reserve_away() and not self._pkg_for_move()):
            return None
        if self.reserve_peer_seen() == "down":
            return None                                       # сеанс мёртвого линка ещё держится — пакет в никуда
        if self.__dict__.get("_rsv_pkg_sending"):
            return None
        due_at = self.__dict__.get("_rsv_pkg_due_at")
        by_event = due_at is not None and time.monotonic() - due_at >= PKG_BATCH_SECONDS
        by_timer = self._pkg_timer_due()
        if self._pkg_journal() and by_timer and self.reserve_jrn_snap_due():
            by_event = True                                   # плановый снимок журнала
        if not by_event and not by_timer:
            return None
        move = by_event and self._pkg_for_move() and self._pkg_journal()
        try:
            # шаг pkg переезда кнопкой при журнале: снимок — только если изменилось что-то вне базы
            # (события у резерва и так все); не изменилось — отметка автомату, go_req уйдёт с прежним
            built = self.reserve_pkg_build(force=by_event and not move)
            if built is None and move:
                self.__dict__.pop("_rsv_pkg_due_at", None)
                self.rsv_update("rsv_out", pkg_unchanged=True)
                return None
        except Exception as e:                            # noqa: BLE001
            self.reserve_jrn_snap_dropped()
            if str(e) != str(self.__dict__.get("_rsv_pkg_err") or ""):   # не каждые 5 с
                log.warning("резерв: пакет не собрался: %s", e)
                self.__dict__["_rsv_pkg_err"] = str(e)
                self.rsv_update("rsv_view", pkg_error=str(e)[:160])
            return None
        self.__dict__.pop("_rsv_pkg_err", None)
        if self.rsv_get("rsv_view").get("pkg_error"):
            self.rsv_update("rsv_view", pkg_error="")
        if built is None:
            self.reserve_jrn_snap_dropped()
            return None
        self.__dict__["_rsv_pkg_sending"] = {"gen": built["gen"], "at": time.monotonic()}
        return built

    def reserve_pkg_sending_stale(self) -> bool:
        """Отправка без ответа дольше двух минут — считать несостоявшейся."""
        s = self.__dict__.get("_rsv_pkg_sending")
        if s and time.monotonic() - s["at"] > 120:
            self.__dict__.pop("_rsv_pkg_sending", None)
            self.reserve_jrn_snap_dropped()
            return True
        return False

    def _pkg_ifaces(self) -> list[tuple[str, str]]:
        from awgbot.infra import awg
        out = [(config.AWG_INTERFACE, awg.read_file(config.CONF_PATH))]
        if config.MIGRATION_INTERFACE:
            try:
                out.append((config.MIGRATION_INTERFACE, awg.read_file(f"{config.AWG_DIR}/{config.MIGRATION_INTERFACE}.conf")))
            except Exception:                             # noqa: BLE001 — интерфейса переезда нет
                pass
        return out

    def _pkg_admin_addrs(self) -> list[str]:
        admin = self.admin_client() if hasattr(self, "admin_client") else None
        if admin is None:
            return []
        return [d.address for d in self.db.list_devices(admin.id) if getattr(d, "address", "")]

    @staticmethod
    def _pkg_dns_addrs() -> list[str]:
        out = [f"{config.SUBNET_PREFIX}.1"]
        if config.MIGRATION_DNS:
            out.append(str(config.MIGRATION_DNS))
        try:
            from awgbot.infra import admindns
            if admindns.installed():
                out.append(admindns.addr())
        except Exception:                                 # noqa: BLE001
            pass
        return [a for a in dict.fromkeys(out) if a]

    @staticmethod
    def _pkg_upstreams() -> list[str]:
        try:
            from awgbot.infra.resolver import CONF_PATH
            text = CONF_PATH.read_text(encoding="utf-8")
        except OSError:
            return []
        out = []
        for line in text.splitlines():
            if line.startswith("server="):
                out.append(line.split("=", 1)[1].split("#", 1)[0].strip())
        return out[:8]

    def _pkg_journal(self) -> bool:
        """Пакет — снимок журнала (`pkg3`): направление журналом и захват включён."""
        try:
            return bool(self.reserve_jrn_on() and self.db.jrn_meta()["capture"])
        except Exception as e:                                # noqa: BLE001
            if rsvjrn.schema_too_old(e):                      # база без журнала (копия, тесты) — прежний путь
                return False
            raise                                             # ошибка базы — не повод собрать pkg2 (резерв снёс бы журнал)

    def reserve_pkg_build(self, force: bool = False) -> dict | None:
        """Собрать пакет. Без force — только если логический отпечаток изменился."""
        with _LOCK:
            keys = self.reserve_keys()
            if not keys.get("k") or not keys.get("m"):
                return None
            journal = self._pkg_journal()
            vpn_part = pkgvpn.build(self._pkg_ifaces(), admin=self._pkg_admin_addrs(), dns=self._pkg_dns_addrs(),
                                    upstreams=self._pkg_upstreams(), tz=str(config.TZ_NAME or ""))
            bot_part = {"conf": pkgbot.conf_part(str(config.CONF_DIR)),
                        "app_guest": pkgbot.guest_part(str(config.CONF_DIR)),
                        "env": pkgbot.env_part(config.BOT_TOKEN, config.ADMIN_ID),
                        "lists": pkgbot.lists_part(str(config.DATA_DIR))}
            import json

            def _logical(db: bytes) -> str:
                return hashlib.blake2b(db + json.dumps(
                    [vpn_part, bot_part["conf"], bot_part["app_guest"], bot_part["env"], bot_part["lists"],
                     int(keys.get("m_gen") or 0), int(keys.get("k_gen") or 0), "pkg3" if journal else "pkg2"],
                    sort_keys=True).encode(), digest_size=16).hexdigest()
            sent = self.rsv_get("rsv_pkg_sent")
            if journal:
                # журнал: база едет событиями — отпечаток таймера только по частям вне базы; решить
                # «снимок не нужен» — до копии базы (таймер раз в pkg_minutes делал бы её впустую)
                logical = _logical(b"")
                if not force and logical in (sent.get("logical"), sent.get("pending_logical")):
                    return None                           # по таймеру без изменений — не уходит
                self._jrn_snap_begin()                        # пачки счётчиков — не сворачивать, пока снимок в пути
            self.__dict__["_rsv_pkg_built_mono"] = time.monotonic()   # что записано после — не в пакете
            db_bytes, marks = pkgbot.replica_marked(self.db.path if hasattr(self.db, "path") else self._db_path())
            if journal:
                marks["cb"] = self.db.jrn_counts_bn()         # пачки по этот номер — из строк, учтённых снимком
                marks["reset"] = bool(self.rsv_get("rsv_jrn_out").get("reset"))
            pkgbot.assert_clean(db_bytes)
            bot_part["db"] = db_bytes.hex()
            if not journal:
                logical = _logical(db_bytes)
                if not force and logical in (sent.get("logical"), sent.get("pending_logical")):
                    return None                           # по таймеру без изменений — не уходит
            gen = max(int(sent.get("gen") or 0), int(sent.get("pending_gen") or 0)) + 1
            dns = self._pkg_dns_envelope()
            parts = {PKG_PART: vpn_part, "bot": bot_part}
            jrn = {"v": 1, **marks} if journal else None
            if jrn:
                parts["jrn"] = jrn
            blob = seal.seal(parts, k=bytes.fromhex(keys["k"]), m=bytes.fromhex(keys["m"]),
                             pkg_gen=gen, m_gen=int(keys.get("m_gen") or 0), dns=dns)
            if not journal:
                self._pkg_store_base(gen, db_bytes)           # база слияния — только прежнему пути
            fp = seal.digest(blob)
            self.rsv_update("rsv_pkg_sent", pending_gen=gen, pending_fp=fp, pending_logical=logical,
                            pending_at=timeutil.now_iso(), pending_jrn=jrn)
            log.info("резерв: %s %s собран (%d КБ%s)", "снимок" if journal else "пакет", gen, len(blob) // 1024,
                     f", журнал по {marks['i']}" if journal else "")
            built = {"gen": gen, "fp": fp, "blob": blob.hex()}
            if jrn:
                # открыто — только то, что резерву нужно для снятия учтённого (номер строки счётчиков
                # cs — внутри: по нему видно, сколько раз устройства были активны)
                built["jrn"] = {k: jrn[k] for k in ("line", "e", "i", "cb", "reset")}
            return built

    def _db_path(self) -> str:
        return os.path.join(str(config.DATA_DIR), "bot.db")

    def _pkg_dns_envelope(self) -> dict | None:
        dns = self.rsv_get("rsv_dns")
        name = self.reserve_dns_name()                        # имя записи — её меняет резерв
        if not dns.get("provider") or not name:
            return None
        return {**dns, "name": name, "self_host": self._own_host_or_empty()}

    def _pkg_store_base(self, gen: int, db_bytes: bytes) -> None:
        out = os.path.join(reserve_dir(), "out")
        os.makedirs(out, mode=0o700, exist_ok=True)
        path = os.path.join(out, f"base-{gen}.db")
        # снимок нужен ровно в сценарии «основа умерла»: tmp + fsync + replace + fsync каталога,
        # иначе после краха на диске файл нулевой длины или усечённый
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(db_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            dfd = os.open(out, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def _pkg_prune_bases(self, keep: list[int]) -> None:
        out = os.path.join(reserve_dir(), "out")
        try:
            names = os.listdir(out)
        except OSError:
            return
        for n in names:
            if n.startswith("base-") and n.endswith(".db"):
                try:
                    g = int(n[5:-3])
                except ValueError:
                    continue
                if g not in keep:
                    try:
                        os.unlink(os.path.join(out, n))
                    except OSError:
                        pass

    def reserve_pkg_ok(self, gen: int, fp: str) -> None:
        """Резерв подтвердил пакет: отметка снята, снимок зафиксирован."""
        with _LOCK:
            sent = self.rsv_get("rsv_pkg_sent")
            self.__dict__.pop("_rsv_pkg_sending", None)
            if int(sent.get("pending_gen") or 0) != int(gen) or str(sent.get("pending_fp") or "") != str(fp):
                log.info("резерв: pkg_ok на пакет %s не того поколения — игнорирую", gen)
                return
            prev = int(sent.get("gen") or 0)
            jrn = sent.get("pending_jrn") or None
            self.rsv_put("rsv_pkg_sent", {"gen": int(gen), "fp": str(fp), "logical": sent.get("pending_logical", ""),
                                          "at": timeutil.now_iso(), "prev_gen": prev, "jrn": jrn})
            if jrn:
                self._pkg_prune_bases([])                     # журнал: возврат без слияния, базы не нужны
                self.reserve_jrn_snap_ok(jrn)
            else:
                self._pkg_prune_bases([int(gen), prev] if prev else [int(gen)])
            due_at = self.__dict__.get("_rsv_pkg_due_at")
            built = self.__dict__.get("_rsv_pkg_built_mono")
            if due_at is not None and built is not None and due_at <= built:
                self.__dict__.pop("_rsv_pkg_due_at", None)      # правка после сборки — в следующий пакет
            log.info("резерв: пакет %s подтверждён", gen)

    def reserve_pkg_bad(self, gen: int, reason: str) -> None:
        self.__dict__.pop("_rsv_pkg_sending", None)
        self.reserve_jrn_snap_dropped()
        log.warning("резерв: пакет %s отвергнут: %s", gen, reason[:120])

    def reserve_pkg_resend_if_stale(self) -> None:
        """`st` резерва называет не тот пакет, что подтверждён (или никакого —
        резерв восстановлен из копии без чужого пакета), — отметка заново."""
        if self.reserve_away():
            return
        peer = self.reserve_peer_state()
        have = (peer.get("pkg") or {}).get("fp") or ""
        sent = self.rsv_get("rsv_pkg_sent")
        if sent.get("gen") and have != sent.get("fp") and self.__dict__.get("_rsv_pkg_due_at") is None:
            log.info("резерв: у «%s» пакет %s вместо %s — отправлю заново", self.reserve_peer_name(),
                     have[:8] or "—", str(sent.get("fp") or "")[:8])
            self.reserve_pkg_mark()

    def reserve_pkg_in_present(self) -> bool:
        """Пакет второй стороны на месте — переезд к себе возможен (ящик кода проверяет
        reserve_open_block)."""
        pkg_in = self.rsv_get("rsv_pkg_in")
        return bool(pkg_in.get("gen")) and os.path.exists(guest_pkg_path())

    # ── приём (резерв) ──────────────────────────────────────────────────────

    def reserve_pkg_in(self, gen: int, fp: str, blob: bytes, jrn: dict | None = None) -> tuple[str, str]:
        """Целый пакет от основы: проверка, запись на диск; поколение `M` — из заголовка
        (с ним сверяется ящик кода, `qkeys`). Возвращает (ответ: pkg_ok | pkg_bad, причина)."""
        if self.reserve_in() in HOSTING:
            return "pkg_bad", "зона занята"                  # гость стартовал с прежнего пакета — новый сбил бы поколение слияния
        try:
            head = seal.header(blob)
        except seal.PackageError as e:
            return "pkg_bad", str(e)
        if head.pkg_gen != int(gen) or head.digest != str(fp):
            return "pkg_bad", "поколение или отпечаток не сошлись"
        os.makedirs(reserve_dir(), mode=0o700, exist_ok=True)
        path = guest_pkg_path()
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())                              # пакет — опора переезда после смерти основы: на диск
        os.replace(tmp, path)
        self.rsv_put("rsv_pkg_in", {"gen": int(gen), "fp": str(fp), "size": len(blob), "at": timeutil.now_iso(),
                                    "m_gen": int(head.m_gen), "has_dns": head.has_dns})
        if jrn:
            self.reserve_keep_snapshot_in(dict(jrn))          # учтённое снимком — из сегментов прочь
        elif self.reserve_keep_line():
            self.reserve_keep_drop()                          # основа ушла на прежний путь — журнал не нужен
        log.info("резерв: принят пакет %s от «%s» (%d КБ)", gen, self.reserve_peer_name(), len(blob) // 1024)
        return "pkg_ok", ""

    def reserve_pkg_in_drop(self) -> None:
        """Удаление пары или возврат: чужой пакет и доля — прочь."""
        try:
            os.unlink(guest_pkg_path())
        except OSError:
            pass
        self.rsv_put("rsv_pkg_in", None)
        self.reserve_keep_drop()

    @staticmethod
    def pkg_chunks(blob_hex: str) -> list[str]:
        return pairproto.chunks(blob_hex)
