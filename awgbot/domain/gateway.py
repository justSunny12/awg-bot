"""
gateway.py — доменная механика роли gateway (docs/ROADMAP.md, п.7, этап 1).

Агент на шлюзе условной маршрутизации: наблюдаемость линка, обвязки и железа,
алерты с гистерезисом. НИКАКОЙ клиентской механики — у шлюза нет ни клиентов,
ни выдачи, ни awg-сервера; отдельный класс, а не наследник Services, потому что
из двух с половиной тысяч строк клиентского кода шлюзу не нужно ничего.

Команды — прямые (subprocess): роль живёт только на хосте, docker-плеча у неё
не бывает по построению. Каждая проба возвращает данные, а не бросает: панель
обязана рисоваться и на полумёртвом шлюзе — именно тогда она нужнее всего.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import asdict, dataclass, field, fields

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil

log = logging.getLogger("awgbot.gateway")

# Notification переиспользуем клиентский: notifier один на обе роли.
from awgbot.domain.services import Notification, ServiceError  # noqa: E402
from awgbot.domain.selfupdate import SelfUpdateMixin  # noqa: E402
from awgbot.domain.backupcrypto import BackupCryptoMixin  # noqa: E402
from awgbot.domain.mailmix import MailMixin  # noqa: E402


def _run(argv: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, timeout=timeout)


def _out(proc) -> str:
    return proc.stdout.decode(errors="replace")


@dataclass
class GwCheck:
    """Одна проверка доктора: имя, вердикт, деталь. ok=None — «нечем проверить»,
    и это ТРЕТЬЕ состояние, а не успех: спутать «не смог посмотреть» с «всё
    хорошо» — способ прозевать отказ ровно там, где смотреть перестали."""
    name: str
    ok: bool | None
    detail: str = ""


@dataclass
class GwStatus:
    """Снимок шлюза для панели. Сериализуется в state (gw_status) целиком:
    панель по /start рисуется из снимка последнего тика, а не гоняет пробы."""
    link_up: bool = False
    handshake_age: float | None = None      # секунд; None — хендшейка нет
    rx: int = 0                             # счётчики линка с момента подъёма
    tx: int = 0
    checks: list[GwCheck] = field(default_factory=list)   # монитор здоровья
    temp: float | None = None
    throttled: dict | None = None
    cpu: float | None = None
    ram: float | None = None
    ram_free_mb: int | None = None
    disk: float | None = None
    disk_free_gb: float | None = None
    smart: str | None = None                # "OK" / "FAIL" / None — не смотрели
    uptime_seconds: int | None = None
    hostname: str = ""
    server_name: str = ""                   # имя ВПС для «Линк до …»
    module_version: str = ""
    srcversion: str = ""
    kernels_missing: list[str] = field(default_factory=list)
    kernels_total: int = 0
    month_rx: int = 0                       # потребление линка за календарный месяц
    month_tx: int = 0
    egress_ms: float | None = None          # выход наружу через домашний канал, мс
    ts: str = ""                            # когда снят (ISO); пусто — живой

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "GwStatus":
        d = json.loads(raw)
        d["checks"] = [GwCheck(**c) for c in d.get("checks", [])]
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def age_seconds(self) -> float | None:
        if not self.ts:
            return None
        return max(0.0, (timeutil.now() - timeutil.parse_iso(self.ts)).total_seconds())


class GatewayServices(SelfUpdateMixin, BackupCryptoMixin, MailMixin):
    """Механика агента. db — обычная Database: нужен только state (гистерезис,
    снимки); клиентские таблицы просто пустуют, и городить отдельную схему ради
    их отсутствия — усложнение без выгоды."""

    def __init__(self, db):
        self.db = db

    # ── линк ─────────────────────────────────────────────────────────────────

    def link_status(self) -> tuple[bool, float | None, int, int]:
        """(интерфейс поднят, возраст хендшейка в сек | None, rx, tx)."""
        up = _run(["ip", "link", "show", config.GW_LINK_IF]).returncode == 0
        if not up:
            return False, None, 0, 0
        age = None
        try:
            out = _out(_run(["awg", "show", config.GW_LINK_IF, "latest-handshakes"]))
            ts = max((int(l.split()[-1]) for l in out.splitlines() if l.split()), default=0)
            if ts:
                age = max(0.0, timeutil.now().timestamp() - ts)
        except Exception as e:                          # noqa: BLE001
            log.warning("gateway: latest-handshakes: %s", e)
        rx = tx = 0
        try:
            out = _out(_run(["awg", "show", config.GW_LINK_IF, "transfer"]))
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    rx += int(parts[1]); tx += int(parts[2])
        except Exception as e:                          # noqa: BLE001
            log.warning("gateway: transfer: %s", e)
        return True, age, rx, tx

    # ── обвязка ──────────────────────────────────────────────────────────────

    def _wan_if(self) -> str:
        """Интерфейс выхода: из конфига либо автодетект по default-маршруту.
        Автодетект на каждом вызове: у домашней машины дефолт может переезжать
        (Ethernet ↔ Wi-Fi), и замороженное значение алертило бы на ровном месте."""
        if config.GW_WAN_IF:
            return config.GW_WAN_IF
        out = _out(_run(["ip", "route", "show", "default"]))
        m = re.search(r"\bdev\s+(\S+)", out)
        return m.group(1) if m else ""

    def plumbing_checks(self) -> list[GwCheck]:
        checks: list[GwCheck] = []
        try:
            fwd = pathlib_read("/proc/sys/net/ipv4/ip_forward").strip() == "1"
            checks.append(GwCheck("ip_forward", fwd,
                                  "" if fwd else "выключен — транзита клиентов нет"))
        except Exception:                                # noqa: BLE001
            checks.append(GwCheck("ip_forward", None, "не прочитался"))

        wan = self._wan_if()
        if config.GW_CLIENT_SUBNET and wan:
            rc = _run(["iptables", "-t", "nat", "-C", "POSTROUTING",
                       "-s", config.GW_CLIENT_SUBNET, "-o", wan,
                       "-j", "MASQUERADE"]).returncode
            checks.append(GwCheck(
                "MASQUERADE", rc == 0,
                "" if rc == 0 else
                f"нет -s {config.GW_CLIENT_SUBNET} -o {wan} — российские сервисы "
                f"увидят туннельный адрес и не ответят"))
        else:
            checks.append(GwCheck(
                "MASQUERADE", None,
                "gateway.client_subnet не задан — проверка выключена"
                if not config.GW_CLIENT_SUBNET else "нет default-маршрута"))

        proc = _run(["iptables", "-S", "AWGLINK_FWD"])
        if proc.returncode != 0:
            checks.append(GwCheck("изоляция LAN", False,
                                  "цепочки AWGLINK_FWD нет — клиентам открыта домашняя сеть"))
        else:
            rules = [l for l in _out(proc).splitlines() if l.startswith("-A ")]
            drops = [i for i, r in enumerate(rules) if " -j DROP" in r]
            accepts = [i for i, r in enumerate(rules) if r.endswith("-j ACCEPT")]
            ok = bool(drops) and bool(accepts) and max(drops) < min(accepts)
            checks.append(GwCheck("изоляция LAN", ok,
                                  "" if ok else "DROP-правила не выше ACCEPT — порядок нарушен"))
            hook = _run(["iptables", "-C", "FORWARD", "-i", config.GW_LINK_IF,
                         "-j", "AWGLINK_FWD"]).returncode == 0
            checks.append(GwCheck("хук изоляции", hook,
                                  "" if hook else "FORWARD не заходит в AWGLINK_FWD"))

        rc = _run(["systemctl", "is-enabled", config.GW_UNIT]).returncode
        checks.append(GwCheck("юнит реассерта", rc == 0,
                              "" if rc == 0 else
                              f"{config.GW_UNIT} не включён — ребут не восстановит обвязку"))
        return checks

    # ── ядро/версии ──────────────────────────────────────────────────────────

    _KVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(.*)$")

    def kernel_coverage(self, modules_root: str = "/lib/modules",
                        running: str | None = None) -> tuple[list[str], int]:
        """Ядра без модуля amneziawg среди ТЕХ, в которые эта машина может
        загрузиться: тот же вариант, что запущенное (суффикс после версии —
        Raspberry Pi OS кладёт в один пакет ядра всех плат: -v8, -2712, -v7l,
        и чужие здесь не загрузятся никогда), и не старше запущенного (назад
        не откатываемся). Смысл проверки — НОВОЕ ядро из apt до ребута: dkms
        молча пропускает ядро без headers, и ребут в него оставил бы шлюз без
        awg. Возвращает (без модуля, сколько ядер рассмотрено)."""
        running = running or os.uname().release
        m = self._KVER_RE.match(running)
        run_ver = tuple(int(x) for x in m.groups()[:3]) if m else None
        run_flavor = m.group(4) if m else ""
        missing: list[str] = []
        considered = 0
        kernels = sorted(
            d for d in glob.glob(os.path.join(modules_root, "*")) if os.path.isdir(d))
        for kdir in kernels:
            name = os.path.basename(kdir)
            km = self._KVER_RE.match(name)
            if run_ver is not None:
                if not km or km.group(4) != run_flavor:
                    continue                     # ядро другой платы/варианта
                if tuple(int(x) for x in km.groups()[:3]) < run_ver:
                    continue                     # старее запущенного — назад не идём
            considered += 1
            # БЕЗ рекурсии по дереву модулей: `**` обходил тысячи файлов на
            # каждое ядро, и панель на Pi рисовалась секунды. Модуль лежит в
            # известных местах: DKMS — updates/dkms, пакетная сборка — kernel/net.
            found = (glob.glob(os.path.join(kdir, "updates", "dkms", "amneziawg.ko*"))
                     or glob.glob(os.path.join(kdir, "kernel", "net", "amneziawg.ko*"))
                     or glob.glob(os.path.join(kdir, "extra", "amneziawg.ko*")))
            if not found:
                missing.append(name)
        return missing, considered

    def versions(self) -> tuple[str, str]:
        """(version, srcversion) модуля. version у сборок AWG ВРЁТ (тег 0828 нёс
        строку 0812) — различать сборки можно только по srcversion."""
        out = _out(_run(["modinfo", "amneziawg"]))
        ver = src = ""
        for line in out.splitlines():
            if line.startswith("version:"):
                ver = line.split(":", 1)[1].strip()
            elif line.startswith("srcversion:"):
                src = line.split(":", 1)[1].strip()
        return ver, src

    # ── гистерезис ───────────────────────────────────────────────────────────

    def _streak_alert(self, key: str, bad: bool | None, streak: int,
                      on_text: str, off_text: str, loud: bool = True,
                      critical: bool = True) -> list[Notification]:
        """Обобщение паттерна ресурс-алертов: алерт после N плохих замеров
        ПОДРЯД, отбой после N хороших. None не двигает счётчики: «не смог
        посмотреть» — не норма и не отказ."""
        if bad is None:
            return []
        hi = int(self.db.get_state(f"gwst_hi_{key}") or 0)
        lo = int(self.db.get_state(f"gwst_lo_{key}") or 0)
        armed = self.db.get_state(f"gwst_armed_{key}") == "1"
        notes: list[Notification] = []
        if bad:
            hi, lo = hi + 1, 0
            if hi >= streak and not armed:
                self.db.set_state(f"gwst_armed_{key}", "1")
                notes.append(Notification(config.ADMIN_ID, on_text, force_sound=loud,
                                          critical=critical))
        else:
            lo, hi = lo + 1, 0
            if lo >= streak and armed:
                self.db.set_state(f"gwst_armed_{key}", "0")
                notes.append(Notification(config.ADMIN_ID, off_text))
        self.db.set_state(f"gwst_hi_{key}", str(hi))
        self.db.set_state(f"gwst_lo_{key}", str(lo))
        return notes

    # ── тик монитора ─────────────────────────────────────────────────────────

    def monitor_tick(self) -> list[Notification]:
        """Один проход: снять всё, сохранить снимок для панели, вернуть алерты.

        Отказ линка — ГРОМКИЙ и с коротким стриком: шлюз существует ради линка,
        и час тишины здесь равен часу неработающего РФ-доступа у всех. Остальное
        — обычные уведомления с обычными стриками.
        """
        st = self.snapshot()
        notes: list[Notification] = []
        streak = settings.get_int("app.monitoring.alert_streak", 5)

        hs_bad = not self.link_ok(st)
        notes += self._streak_alert(
            "link", hs_bad, settings.get_int("app.gateway.link_alert_streak", 2),
            "🚨 Линк до ВПС мёртв: хендшейка нет дольше допустимого. РФ-доступ "
            "у клиентов не работает.",
            "✅ Линк до ВПС ожил, хендшейк свежий.",
            loud=settings.get_bool("app.gateway.link_alert_loud", True))

        notes += self._streak_alert(
            "egress", st.egress_ms is None, streak,
            "⚠️ Шлюз не выходит наружу: домашний канал не отвечает. РФ-доступ "
            "через шлюз не работает.",
            "✅ Домашний канал шлюза снова отвечает.")

        broken = [c for c in st.checks if c.ok is False]
        notes += self._streak_alert(
            "plumbing", bool(broken), streak,
            "⚠️ Обвязка шлюза неисправна: "
            + "; ".join(f"{c.name} — {c.detail}" for c in broken[:3]),
            "✅ Обвязка шлюза снова в порядке.")

        notes += self._streak_alert(              # не критично: стреляет только на ребуте
            "kernels", bool(st.kernels_missing), streak,
            "⚠️ Ядра без модуля awg: " + ", ".join(st.kernels_missing[:4]) +
            ". Ребут в такое ядро оставит шлюз без туннелей.",
            "✅ Все установленные ядра покрыты модулем awg.", critical=False)

        under_now = bool(st.throttled and st.throttled.get("now"))
        notes += self._streak_alert(
            "power", under_now, 2,
            "⚠️ Питание Pi: " + "; ".join((st.throttled or {}).get("now", [])) +
            ". Классика тихой смерти — проверь блок питания.",
            "✅ Питание Pi в норме.")

        temp_bad = None if st.temp is None else \
            st.temp >= settings.get_int("app.gateway.temp_alert_c", 75)
        notes += self._streak_alert(
            "temp", temp_bad, streak,
            f"🌡 SoC {st.temp:.0f}°C — перегрев." if st.temp is not None else "",
            "✅ Температура SoC в норме.")

        # алерты хоста — общим тумблером и порогами с основным ботом
        if settings.get_bool("resource_alerts.enabled", True):
            for name, val, label, unit in (("cpu", st.cpu, "CPU", "%"), ("ram", st.ram, "RAM", "%"),
                                           ("disk", st.disk, "Диск", "%")):
                bad = None if val is None else \
                    val >= settings.get_int(f"resource_alerts.thresholds_percent.{name}", 80)
                notes += self._streak_alert(
                    name, bad, streak,
                    f"📈 {label} шлюза: {val:.0f}{unit} — выше порога." if val is not None else "",
                    f"✅ {label} шлюза снова в норме.")

        try:
            self.tg_mark_ensure()
        except Exception as e:                          # noqa: BLE001
            log.warning("gateway: tg_mark_ensure: %s", e)

        return [n for n in notes if n.text]

    # ── путь к Telegram: маркировка диапазонов (этап 2) ──────────────────────

    # Диапазоны Telegram (AS62014/62041/59930/44907) — стабильны годами. Трафик
    # к ним метится 0x1 и уходит в туннель до ВПС по уже существующей политике
    # шлюза (fwmark 0x1 → table 100 → awg0): без этого агент нем — Telegram в
    # юрисдикции шлюза заблокирован. Персист — в PostUp клиентского конфига
    # шлюза; агент лишь реассертит недостающее: правило идемпотентно, и
    # автоматика здесь хуже не сделает.
    TG_RANGES = ("91.108.4.0/22", "91.108.8.0/22", "91.108.12.0/22",
                 "91.108.16.0/22", "91.108.20.0/22", "91.108.56.0/22",
                 "149.154.160.0/20", "185.76.151.0/24")

    def tg_mark_missing(self) -> list[str]:
        return [n for n in self.TG_RANGES
                if _run(["iptables", "-t", "mangle", "-C", "OUTPUT", "-d", n,
                         "-j", "MARK", "--set-mark", "0x1"]).returncode != 0]

    def tg_mark_ensure(self) -> int:
        """Доставить недостающие правила. Возвращает число поставленных."""
        n = 0
        for net in self.tg_mark_missing():
            if _run(["iptables", "-t", "mangle", "-A", "OUTPUT", "-d", net,
                     "-j", "MARK", "--set-mark", "0x1"]).returncode == 0:
                n += 1
        if n:
            log.warning("gateway: маркировка Telegram доставлена: %d правил", n)
        return n

    # ── операции с кнопки (этап 2) ───────────────────────────────────────────

    def restart_link(self) -> tuple[bool, str]:
        """Мягкий рестарт линка: down/up интерфейса без пересборки обвязки.
        Секунды обрыва RF у всех — поэтому только с подтверждения."""
        down = _run(["awg-quick", "down", config.GW_LINK_IF], timeout=30)
        up = _run(["awg-quick", "up", config.GW_LINK_IF], timeout=30)
        ok = up.returncode == 0
        tail = (_out(up) + up.stderr.decode(errors="replace")).strip().splitlines()[-3:]
        return ok, "\n".join(tail) if tail else ("поднят" if ok else "не поднялся")

    def reassert(self) -> tuple[bool, str]:
        """Полный реассерт: рестарт юнита шлюза — тот зовёт gw-скрипт, который
        идемпотентно переставляет правила и переподнимает линк."""
        proc = _run(["systemctl", "restart", config.GW_UNIT], timeout=90)
        ok = proc.returncode == 0
        return ok, "" if ok else proc.stderr.decode(errors="replace").strip()[-300:]

    _BACKUP_B64_RE = re.compile(r'^BACKUP_B64="([A-Za-z0-9+/=]+)"', re.M)

    def _bundle_plain(self, blob: bytes) -> str:
        from awgbot.util import bundlecrypt
        priv = bundlecrypt.read_privkey(pathlib_read(config.GW_LINK_CONF))
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
            current = pathlib_read(config.GW_LINK_CONF)
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
        import os, tempfile
        from awgbot.util import bundlecrypt
        try:
            priv = bundlecrypt.read_privkey(pathlib_read(config.GW_LINK_CONF))
            plain = bundlecrypt.decrypt(blob, priv)
        except (OSError, ValueError) as e:
            return False, f"бандл не принят: {e}"
        text = plain.decode(errors="replace")
        if "#__GW_SETUP_BELOW__" not in text or "__LINK_CONF_EOF__" not in text:
            return False, "бандл не принят: внутри нет маркеров контракта линка"
        m = re.search(r'^SERVER_NAME="([^"\n]{1,64})"', text, re.M)
        if m:
            self.db.set_state(self._SERVER_NAME_KEY, m.group(1))
        self._apply_bundle_mail(text)
        phrase = self._bundle_passphrase(text)
        if phrase and (overwrite_passphrase or not self.backup_encryption_enabled()):
            try:
                self.backup_set_passphrase(phrase)
            except ValueError as e:
                log.warning("gateway: фраза из бандла не принята: %s", e)
        fd, path = tempfile.mkstemp(prefix="awg-gw-bundle-", suffix=".sh", dir="/root")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(plain)
            proc = _run(["sh", path, "--apply"], timeout=180)
            out = (_out(proc) + proc.stderr.decode(errors="replace")).strip()
            tail = "\n".join(out.splitlines()[-6:])
            return proc.returncode == 0, tail
        finally:
            try:
                os.unlink(path)               # внутри приватный ключ — не оставляем
            except OSError:
                pass

    def doctor(self) -> list[GwCheck]:
        """Все проверки — живьём. Список, а не вердикт: чинить будут по строкам."""
        return self.status().checks

    # ── выход наружу через домашний канал ────────────────────────────────────

    def egress_probe(self) -> float | None:
        """TCP-коннект к российскому и к зарубежному адресу через default-маршрут
        (домашний канал, не туннель). Возвращает время первого удачного, мс;
        None — никто не ответил. Две цели: один внешний хост сам по себе точка
        отказа, и его заминка выглядела бы как отвал канала."""
        targets = settings.get("app.gateway.egress_targets", None) or ["77.88.8.8", "8.8.8.8"]
        port = int(settings.get("app.gateway.egress_port", 53))
        for host in targets:
            t0 = time.monotonic()
            try:
                with socket.create_connection((str(host), port), timeout=3.0):
                    return round((time.monotonic() - t0) * 1000, 1)
            except OSError:
                continue
        return None

    # ── резервная копия ──────────────────────────────────────────────────────

    def make_backup(self) -> list[str]:
        """Один архив: БД агента, все conf/*.yaml, env и ВСЕ конфиги
        awg-интерфейсов шлюза (в них приватные ключи линка и туннеля —
        единственная копия вне этой машины). Только шифрованно: без парольной
        фразы отказ, открытые ключи в чат и на почту не уезжают."""
        if not self.backup_encryption_enabled():
            raise ServiceError("резервная копия шлюза только шифрованная: задай парольную "
                               "фразу в ⚙️ Настройки → 💾 Резервное копирование → 🔐 Шифрование")
        extra: list[tuple[str, bytes]] = []
        for p in sorted(glob.glob(os.path.join(config.GW_CONF_DIR, "*.conf"))):
            try:
                extra.append((f"awg/{os.path.basename(p)}", open(p, "rb").read()))
            except OSError:
                pass
        return self.write_backup_archive("gw", extra, require_encryption=True)

    def _apply_bundle_mail(self, text: str) -> bool:
        """MAIL_B64 из бандла → настройки почты агента (креды в БД, серверы в
        conf). Нет строки — свои настройки не трогаем."""
        m = re.search(r'^MAIL_B64="([A-Za-z0-9+/=]+)"', text, re.M)
        if not m:
            return False
        import base64
        try:
            d = json.loads(base64.b64decode(m.group(1)).decode())
            self.email_save(d["login"], d["password"], d["imap_host"], int(d["imap_port"]),
                            d["smtp_host"], int(d["smtp_port"]))
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway: почта из бандла не принята: %s", e)
            return False
        return True

    # ── имя ВПС ──────────────────────────────────────────────────────────────

    _SERVER_NAME_KEY = "gw_server_name"

    def server_name(self) -> str:
        """Имя ВПС для «Линк до …»: явная настройка → имя из бандла → «ВПС»."""
        return (str(settings.get("app.gateway.server_name", "") or "").strip()
                or (self.db.get_state(self._SERVER_NAME_KEY) or "").strip()
                or "ВПС")

    # ── потребление за месяц ─────────────────────────────────────────────────

    _TRAFFIC_KEY = "gw_traffic"

    def _account_traffic(self, rx: int, tx: int) -> tuple[int, int]:
        """Счётчики линка живут от подъёма интерфейса и обнуляются каждым
        рестартом. Копим дельты в месячный итог: счётчик меньше прошлого —
        значит, обнулился, и дельта — весь текущий. Новый календарный месяц
        начинает итог заново."""
        month = timeutil.now().strftime("%Y-%m")
        try:
            acc = json.loads(self.db.get_state(self._TRAFFIC_KEY) or "{}")
        except (json.JSONDecodeError, ValueError):
            acc = {}
        if acc.get("month") != month:
            acc = {"month": month, "rx": 0, "tx": 0,
                   "last_rx": acc.get("last_rx", 0), "last_tx": acc.get("last_tx", 0)}
        last_rx, last_tx = int(acc.get("last_rx", 0)), int(acc.get("last_tx", 0))
        d_rx = rx - last_rx if rx >= last_rx else rx
        d_tx = tx - last_tx if tx >= last_tx else tx
        acc["rx"] = int(acc.get("rx", 0)) + max(0, d_rx)
        acc["tx"] = int(acc.get("tx", 0)) + max(0, d_tx)
        acc["last_rx"], acc["last_tx"] = rx, tx
        self.db.set_state(self._TRAFFIC_KEY, json.dumps(acc))
        return acc["rx"], acc["tx"]

    # ── сводка ───────────────────────────────────────────────────────────────

    def status(self) -> GwStatus:
        """Живой снимок: линк, монитор здоровья, железо. Одна прогулка по всем
        пробам — доли секунды на Pi; панель по /start берёт снимок тика
        (cached_status), живьём ходят «Обновить», «Монитор здоровья» и сам тик."""
        from awgbot.runtime import hostmetrics
        st = GwStatus()
        st.link_up, st.handshake_age, st.rx, st.tx = self.link_status()
        checks = list(self.plumbing_checks())
        missing = self.tg_mark_missing()
        checks.append(GwCheck("маршрут к Telegram", not missing,
                              "" if not missing else
                              f"нет маркировки для {len(missing)} диапазонов — "
                              f"мастер восстановления поставит"))
        ok_link = st.link_up and st.handshake_age is not None
        checks.append(GwCheck("линк", ok_link, "" if ok_link else
                              ("интерфейс лежит" if not st.link_up else "хендшейка не было")))
        # Живьём, без кэша: кэш на 10 минут показывал «модуль: ?» и «ядро без
        # модуля» всё время после обновления модуля — снимок середины операции.
        st.module_version, st.srcversion = self.versions()
        st.kernels_missing, st.kernels_total = self.kernel_coverage()
        checks.append(GwCheck("ядра", not st.kernels_missing,
                              "" if not st.kernels_missing else
                              "без модуля awg: " + ", ".join(st.kernels_missing)))
        st.egress_ms = self.egress_probe()
        checks.append(GwCheck("выход наружу", st.egress_ms is not None,
                              f"{st.egress_ms:.0f} мс" if st.egress_ms is not None else
                              "домашний канал не отвечает — РФ-доступ через шлюз не работает"))
        st.checks = checks
        st.temp = hostmetrics.read_soc_temp()
        st.throttled = hostmetrics.read_pi_throttled()
        st.cpu = hostmetrics.read_cpu_percent()
        ram = hostmetrics.read_ram()
        if ram is not None:
            st.ram, st.ram_free_mb = ram
        disk = hostmetrics.read_disk()
        if disk is not None:
            st.disk, st.disk_free_gb = disk
        st.smart = hostmetrics.read_smart_health()
        st.uptime_seconds = hostmetrics.read_uptime_seconds()
        st.hostname = socket.gethostname()
        st.server_name = self.server_name()
        return st

    _SNAPSHOT_KEY = "gw_status"

    def link_ok(self, st: GwStatus) -> bool:
        return bool(st.link_up and st.handshake_age is not None and
                    st.handshake_age <= settings.get_int("app.gateway.handshake_max_age", 300))

    def snapshot(self) -> GwStatus:
        """Живой статус + учёт трафика + сохранить как снимок для панели."""
        st = self.status()
        st.month_rx, st.month_tx = self._account_traffic(st.rx, st.tx)
        st.ts = timeutil.to_iso(timeutil.now())
        self.db.set_state(self._SNAPSHOT_KEY, st.to_json())
        return st

    def cached_status(self, max_age_seconds: float) -> GwStatus | None:
        """Снимок последнего тика, если он не старше max_age; иначе None —
        вызывающий снимет живьём. Панель из снимка стоит ноль проб и рисуется
        мгновенно; свежесть видна строкой «Обновлено …»."""
        raw = self.db.get_state(self._SNAPSHOT_KEY)
        if not raw:
            return None
        try:
            st = GwStatus.from_json(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        age = st.age_seconds()
        if age is None or age > max_age_seconds:
            return None
        return st


def pathlib_read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()
