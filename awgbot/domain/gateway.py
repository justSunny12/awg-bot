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
import subprocess
import urllib.request
from dataclasses import dataclass, field

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil

log = logging.getLogger("awgbot.gateway")

# Notification переиспользуем клиентский: notifier один на обе роли.
from awgbot.domain.services import Notification  # noqa: E402


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
    link_up: bool = False
    handshake_age: float | None = None      # секунд; None — хендшейка нет
    rx: int = 0
    tx: int = 0
    checks: list[GwCheck] = field(default_factory=list)
    temp: float | None = None
    throttled: dict | None = None
    disk: float | None = None
    module_version: str = ""
    srcversion: str = ""
    kernels_missing: list[str] = field(default_factory=list)
    kernels_total: int = 0
    ext_ip: str = ""


class GatewayServices:
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

    def kernel_coverage(self, modules_root: str = "/lib/modules") -> tuple[list[str], int]:
        """Ядра без модуля amneziawg. Дыра, найденная руками: dkms молча
        пропускает ядро без headers, и загрузка в него оставляет шлюз без awg."""
        missing: list[str] = []
        kernels = sorted(
            d for d in glob.glob(os.path.join(modules_root, "*")) if os.path.isdir(d))
        for kdir in kernels:
            if not glob.glob(os.path.join(kdir, "**", "amneziawg.ko*"), recursive=True):
                missing.append(os.path.basename(kdir))
        return missing, len(kernels)

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

    # ── внешний IP ───────────────────────────────────────────────────────────

    _EXT_IP_KEY = "gw_ext_ip"

    def fetch_external_ip(self) -> str:
        """Текущий внешний IP (пустая строка — не удалось). Два независимых
        сервиса: один недоступный не должен выглядеть сменой адреса."""
        for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    ip = r.read().decode(errors="replace").strip()
                if re.fullmatch(r"[0-9.]{7,15}", ip):
                    return ip
            except Exception:                            # noqa: BLE001
                continue
        return ""

    # ── гистерезис ───────────────────────────────────────────────────────────

    def _streak_alert(self, key: str, bad: bool | None, streak: int,
                      on_text: str, off_text: str) -> list[Notification]:
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
                notes.append(Notification(config.ADMIN_ID, on_text, force_sound=True))
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
        from awgbot.runtime import hostmetrics
        st = self.status()
        notes: list[Notification] = []
        streak = settings.get_int("app.monitoring.alert_streak", 5)

        hs_bad = (not st.link_up) or st.handshake_age is None or \
            st.handshake_age > settings.get_int("app.gateway.handshake_max_age", 300)
        notes += self._streak_alert(
            "link", hs_bad, settings.get_int("app.gateway.link_alert_streak", 2),
            "🚨 Линк до ВПС мёртв: хендшейка нет дольше допустимого. РФ-доступ "
            "у клиентов не работает.",
            "✅ Линк до ВПС ожил, хендшейк свежий.")

        broken = [c for c in st.checks if c.ok is False]
        notes += self._streak_alert(
            "plumbing", bool(broken), streak,
            "⚠️ Обвязка шлюза неисправна: "
            + "; ".join(f"{c.name} — {c.detail}" for c in broken[:3]),
            "✅ Обвязка шлюза снова в порядке.")

        notes += self._streak_alert(
            "kernels", bool(st.kernels_missing), streak,
            "⚠️ Ядра без модуля awg: " + ", ".join(st.kernels_missing[:4]) +
            ". Ребут в такое ядро оставит шлюз без туннелей.",
            "✅ Все установленные ядра покрыты модулем awg.")

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

        disk_bad = None if st.disk is None else \
            st.disk >= settings.get_int("resource_alerts.thresholds_percent.disk", 80)
        notes += self._streak_alert(
            "disk", disk_bad, streak,
            f"💽 Карта заполнена на {st.disk:.0f}%." if st.disk is not None else "",
            "✅ Место на карте снова в норме.")

        try:
            self.tg_mark_ensure()
        except Exception as e:                          # noqa: BLE001
            log.warning("gateway: tg_mark_ensure: %s", e)

        ip = self.fetch_external_ip()
        if ip:
            prev = self.db.get_state(self._EXT_IP_KEY) or ""
            if prev and prev != ip:
                notes.append(Notification(
                    config.ADMIN_ID,
                    f"🌍 Внешний IP шлюза сменился: {prev} → {ip}. Эндпоинт "
                    f"линка на ВПС смотрит на DDNS — проверь, что тот догнал.",
                    force_sound=True))
            if prev != ip:
                self.db.set_state(self._EXT_IP_KEY, ip)
            st.ext_ip = ip

        self.db.set_state("gw_status", json.dumps({
            "ts": timeutil.to_iso(timeutil.now()),
            "link_up": st.link_up, "handshake_age": st.handshake_age,
            "rx": st.rx, "tx": st.tx, "temp": st.temp, "disk": st.disk,
            "ext_ip": st.ext_ip or (self.db.get_state(self._EXT_IP_KEY) or ""),
        }))
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

    def apply_bundle(self, blob: bytes) -> tuple[bool, str]:
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
        """Доктор: все проверки панели плюс путь к Telegram и линк — как список,
        а не как вердикт: чинить будут по строкам."""
        checks = list(self.plumbing_checks())
        missing = self.tg_mark_missing()
        checks.append(GwCheck("маршрут к Telegram", not missing,
                              "" if not missing else
                              f"нет маркировки для {len(missing)} диапазонов — "
                              f"реассерт поставит"))
        up, age, _, _ = self.link_status()
        checks.append(GwCheck("линк", up and age is not None,
                              "" if up and age is not None else
                              ("интерфейс лежит" if not up else "хендшейка не было")))
        km, kt = self.kernel_coverage()
        checks.append(GwCheck("ядра", not km,
                              "" if not km else "без модуля: " + ", ".join(km)))
        return checks

    # ── сводка для панели ────────────────────────────────────────────────────

    def status(self) -> GwStatus:
        from awgbot.runtime import hostmetrics
        st = GwStatus()
        st.link_up, st.handshake_age, st.rx, st.tx = self.link_status()
        st.checks = self.plumbing_checks()
        st.temp = hostmetrics.read_soc_temp()
        st.throttled = hostmetrics.read_pi_throttled()
        st.disk = hostmetrics.read_disk_percent()
        st.module_version, st.srcversion = self.versions()
        st.kernels_missing, st.kernels_total = self.kernel_coverage()
        st.ext_ip = self.db.get_state(self._EXT_IP_KEY) or ""
        return st


def pathlib_read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()
