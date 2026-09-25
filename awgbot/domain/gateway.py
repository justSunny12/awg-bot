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
import hashlib
import html
import json
import logging
import os
import re
import secrets
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
from awgbot.domain.gwssh import GwSshMixin  # noqa: E402
from awgbot.domain import gwownlists, gwservices  # noqa: E402
from awgbot.domain.gwchecks import CHECK_GROUPS_SOFT  # noqa: E402
from awgbot.util import gwlink  # noqa: E402


import threading  # noqa: E402

# Бандл из чата и настройки из канала применяются одним и тем же скриптом через
# тот же юнит — вперемешку они переписывали бы юнит друг другу посреди прогона.
_APPLY_LOCK = threading.Lock()


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
    group: str = ""                          # "lan" — локальная сеть без VPN: свой стрик, не критично


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
    egress_ms: float | None = None          # выход наружу через канал квартиры, мс последнего замера
    egress_ok: bool | None = None           # он же вердиктом: улики или зонд
    egress_src: str = ""                    # чем доказан: трафик | проба | кэш
    tg_missing: list[str] = field(default_factory=list)   # диапазоны Telegram без маркировки
    mark_status: str = ""                   # шлюзовое устройство: confirmed|unmarked|foreign|unconfirmed
    lan: dict = field(default_factory=dict) # локальная сеть без VPN: пусто — выключена
    ssh: dict = field(default_factory=dict) # порт (факт), владелец, фильтр снаружи, адреса — для панели
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


class GatewayServices(SelfUpdateMixin, BackupCryptoMixin, MailMixin, GwSshMixin):
    """Механика агента. db — обычная Database: нужен только state (гистерезис,
    снимки); клиентские таблицы просто пустуют, и городить отдельную схему ради
    их отсутствия — усложнение без выгоды."""

    def __init__(self, db):
        self.db = db
        # канал до ВПС: открыта ли сессия и байты через линк — пишет
        # runtime/linkclient (слот 0), читают зонд наружу и задача списков
        from awgbot.domain.channelstate import ChannelState
        self.channel = ChannelState()

    # ── линк ─────────────────────────────────────────────────────────────────

    def link_status(self) -> tuple[bool, float | None, int, int]:
        """(интерфейс поднят, возраст хендшейка в сек | None, rx, tx) — одним
        `awg show <if> dump`: хендшейк и счётчики в нём же, два вызова были лишними."""
        up = _run(["ip", "link", "show", config.GW_LINK_IF]).returncode == 0
        if not up:
            return False, None, 0, 0
        age = None
        rx = tx = 0
        try:
            from awgbot.infra.awg import parse_dump
            peers = parse_dump(_out(_run(["awg", "show", config.GW_LINK_IF, "dump"])))
            ts = max((int(p["last_handshake"] or 0) for p in peers), default=0)
            if ts:
                age = max(0.0, timeutil.now().timestamp() - ts)
            rx = sum(int(p.get("rx") or 0) for p in peers)
            tx = sum(int(p.get("tx") or 0) for p in peers)
        except Exception as e:                          # noqa: BLE001
            log.warning("gateway: awg show dump: %s", e)
        return True, age, rx, tx

    # ── обвязка ──────────────────────────────────────────────────────────────

    def plumbing_checks(self) -> list[GwCheck]:
        checks: list[GwCheck] = []
        try:
            fwd = pathlib_read("/proc/sys/net/ipv4/ip_forward").strip() == "1"
            checks.append(GwCheck("ip_forward", fwd,
                                  "" if fwd else "выключен — транзита клиентов нет"))
        except Exception:                                # noqa: BLE001
            checks.append(GwCheck("ip_forward", None, "не прочитался"))

        # Вся обвязка — одна nft-таблица (gwguard): MASQUERADE, изоляция,
        # метки Telegram, защита машины от туннеля. Одним `nft -j list table`.
        from awgbot.infra import gwguard
        try:
            info = gwguard.table_info()
        except gwguard.GwGuardError as e:
            info = None
            checks.append(GwCheck("таблица awg_gw_guard", None, str(e)))
        else:
            if info is None:
                checks.append(GwCheck(
                    "таблица awg_gw_guard", False,
                    "нет — обвязка старого образца или снята; перевыпусти "
                    "конфигурацию шлюза с сервера AWG"))
        self._guard_info = info
        if info is not None:
            nets = info["sets"].get("tunnel_nets4", set())
            client_subnet = gwguard.client_subnet()   # conf агента или юнит обвязки
            if client_subnet:
                ok = client_subnet in nets
                checks.append(GwCheck(
                    "MASQUERADE/изоляция", ok,
                    "" if ok else f"{client_subnet} нет в tunnel_nets4 — "
                    "бандл собран под другую подсеть"))
            else:
                checks.append(GwCheck("MASQUERADE/изоляция", None,
                                      "подсеть клиентов неизвестна (нет бандла) — проверка выключена"))
            missing_chains = [c for c in gwguard.CHAINS if c not in info["chains"]]
            checks.append(GwCheck("цепочки таблицы", not missing_chains,
                                  "" if not missing_chains else
                                  "нет: " + ", ".join(missing_chains)))
            pol = gwguard.iptables_forward_policy()
            ok_fwd = pol in (None, "accept")
            if not ok_fwd:
                # docker ставит DROP; раздел 3a скрипта открывает транзит своим
                # интерфейсам в той же цепочке. Есть наши ACCEPT для линка —
                # транзит идёт, и красная проверка была бы ложной тревогой.
                acc = gwguard.forward_accepts()
                ok_fwd = {f"i:{config.GW_LINK_IF}", f"o:{config.GW_LINK_IF}"} <= acc
            checks.append(GwCheck("политика FORWARD", ok_fwd,
                                  "" if ok_fwd else
                                  f"ip filter FORWARD: {pol} — drop чужой таблицы "
                                  "перекрывает транзит клиентов"))
        # Включённость юнита меняется только руками — статический ярус (45
        # мин, кнопки «Статус»/«Монитор здоровья» сбрасывают), а не exec на тик.
        enabled = self._unit_enabled()
        checks.append(GwCheck("юнит реассерта", enabled,
                              "" if enabled else
                              f"{config.GW_UNIT} не включён — ребут не восстановит обвязку"))
        # Политика «Telegram → аплинк»: без неё метка стоит, а пакеты агента
        # уходят домашнему провайдеру — Telegram недоступен, агент молчит.
        # Аплинк (автодетект — три exec) и состояние политики запоминаем на
        # тик: heal возьмёт их отсюда, а не снимет заново.
        uplink = gwguard.uplink_interface()
        pol = gwguard.uplink_policy(uplink) if uplink else None
        self._uplink_state = (uplink, pol)
        if uplink:
            ok = pol["rule"] and pol["route"]
            lack = [n for n, v in (("правило по метке", pol["rule"]),
                                   (f"маршрут в {uplink}", pol["route"])) if not v]
            checks.append(GwCheck("политика аплинка", ok,
                                  "" if ok else "нет: " + ", ".join(lack) + " — агент перевыставит"))
            # Маскарад в аплинк: без него локальный пакет уходит в туннель с
            # адресом домашней сети, и ВПС его отбрасывает — Telegram через ВПС
            # не проходит. На прежней малине это делало чужое правило домашней
            # схемы, чистая установка без него нема.
            info = self.__dict__.get("_guard_info")
            if info is not None:
                masq = uplink in info.get("masq_ifaces", set())
                checks.append(GwCheck("маскарад в аплинк", masq,
                                      "" if masq else f"нет masquerade в {uplink}: пакеты агента "
                                      "уходят в туннель с локальным адресом — перевыпусти "
                                      "конфигурацию шлюза с сервера AWG и примени её здесь"))
        else:
            checks.append(GwCheck("политика аплинка", None, "аплинк не найден"))
        return checks

    def _unit_enabled(self) -> bool:
        c = self.__dict__.get("_unit_enabled_cache")
        if c and time.monotonic() - c[0] < self._STATIC_TTL:
            return c[1]
        enabled = _run(["systemctl", "is-enabled", config.GW_UNIT]).returncode == 0
        self.__dict__["_unit_enabled_cache"] = (time.monotonic(), enabled)
        return enabled

    def uplink_policy_heal(self) -> list[str]:
        """Перевыставить правило/маршрут аплинка, если пропали (см. gwguard).
        Каждый тик: аплинк и состояние политики — те, что только что снял
        plumbing_checks (дубль автодетекта и двух `ip -j` на тик снят); вне
        тика — свои пробы. Idempotent-команды ip только при пропаже."""
        from awgbot.infra import gwguard
        state = self.__dict__.pop("_uplink_state", None)
        if state is None:
            uplink = gwguard.uplink_interface()
            pol = gwguard.uplink_policy(uplink) if uplink else None
        else:
            uplink, pol = state
        if not uplink:
            return []
        fixed = gwguard.uplink_policy_ensure(uplink, pol)
        if fixed:
            log.warning("gateway: политика аплинка перевыставлена: %s", ", ".join(fixed))
        return fixed

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

    def _armer(self, key: str, value: str):
        """Отложенная отметка «алерт показан» — её ставит рассылка по факту
        доставки (Notification.on_sent)."""
        def _mark() -> None:
            self.db.set_state(f"gwst_armed_{key}", value)
        return _mark

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
        # Потолок на пороге: выше него счётчик ничего не решает, а без потолка
        # каждый спокойный тик был бы записью на SD-карту.
        # «Взведён» переключаем только ПОСЛЕ доставки. Запись до отправки давала
        # одинокий отбой: сеть у шлюза падает вместе с линком, алерт не улетал,
        # а «✅ ожил» приходил первым и единственным словом — беда выглядела
        # так, будто её не было. Не дошло — следующий тик скажет то же самое.
        if bad:
            hi, lo = min(hi + 1, streak), 0
            if hi >= streak and not armed:
                notes.append(Notification(config.ADMIN_ID, on_text, force_sound=loud,
                                          critical=critical,
                                          on_sent=self._armer(key, "1")))
        else:
            lo, hi = min(lo + 1, streak), 0
            if lo >= streak and armed:
                notes.append(Notification(config.ADMIN_ID, off_text,
                                          on_sent=self._armer(key, "0")))
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
        notes: list[Notification] = []
        streak = settings.get_int("app.monitoring.alert_streak", 5)

        # Весь тик — одной транзакцией: снимок для панели, месячный трафик и
        # стрики. Было три коммита за тик (снимок, трафик, стрики) — на флеш
        # малины это три fsync каждые три минуты; стал один.
        with self.db.transaction():
            st = self.snapshot()
            notes += self._tick_alerts(st, streak)

        try:
            self.tg_mark_ensure(st.tg_missing)          # без повторной пробы
        except Exception as e:                          # noqa: BLE001
            log.warning("gateway: tg_mark_ensure: %s", e)
        try:
            fixed = self.uplink_policy_heal()
        except Exception as e:                          # noqa: BLE001
            log.warning("gateway: uplink_policy_heal: %s", e)
            fixed = []
        # Реконсайл SSH прошёл внутри status() — до проверок, чтобы снимок не
        # называл «реассерт не прошёл» то, что ещё не пробовали; уведомления
        # оттуда забираем здесь.
        notes += self.__dict__.pop("_ssh_pending", [])
        if fixed:
            # Одно уведомление на факт: пропажа правила — событие (обычно
            # перезапуск systemd-networkd), о котором стоит знать, а не стрик.
            notes.append(Notification(
                config.ADMIN_ID,
                "🔧 Политика «Telegram → аплинк» пропала и перевыставлена: "
                + ", ".join(fixed) + ". Обычно так делает перезапуск "
                "systemd-networkd — установщик шлюза запрещает ему трогать "
                "чужие правила, перевыпусти конфигурацию шлюза с сервера AWG, если "
                "повторится.", critical=False))

        return [n for n in notes if n.text]

    def _tick_alerts(self, st: GwStatus, streak: int) -> list[Notification]:
        notes: list[Notification] = []
        hs_bad = not self.link_ok(st)
        notes += self._streak_alert(
            "link", hs_bad, settings.get_int("app.gateway.link_alert_streak", 2),
            "🚨 Линк до сервера AWG мёртв: хендшейка нет дольше допустимого. РФ-доступ "
            "у клиентов не работает.",
            "✅ Линк до сервера AWG ожил, хендшейк свежий.",
            loud=settings.get_bool("app.gateway.link_alert_loud", True))

        # Лежащий домашний канал для РФ-доступа равносилен лежащему линку —
        # тот же короткий стрик, а не общий на пять тиков.
        host = html.escape(socket.gethostname(), quote=False)
        notes += self._streak_alert(
            "egress", st.egress_ok is False,
            settings.get_int("app.gateway.egress_alert_streak", 2),
            "⚠️ Шлюз не выходит наружу: канал не отвечает. РФ-доступ "
            "через шлюз не работает.",
            f"✅ Канал шлюза {host} снова отвечает")

        broken = [c for c in st.checks if c.ok is False and c.group not in CHECK_GROUPS_SOFT]
        notes += self._streak_alert(
            "plumbing", bool(broken), streak,
            "⚠️ Обвязка шлюза неисправна: "
            + "; ".join(f"{html.escape(c.name, quote=False)} — {html.escape(c.detail, quote=False)}"
                        for c in broken[:3]),
            "✅ Обвязка шлюза снова в порядке")
        # локальная сеть без VPN — отдельно и не критично: тишина в пустой
        # квартире или упавший резолвер — не «РФ-доступ у всех лёг»
        lan_broken = [c for c in st.checks if c.ok is False and c.group == "lan"]
        subnet = html.escape((st.lan or {}).get("subnet", "") or "", quote=False)
        notes += self._streak_alert(
            "lan", bool(lan_broken), streak,
            "⚠️ Локальная сеть без VPN: "
            + "; ".join(f"{html.escape(c.name, quote=False)} — {html.escape(c.detail, quote=False)}"
                        for c in lan_broken[:3]),
            f"✅ Локальная сеть без VPN ({host}" + (f", <code>{subnet}</code>" if subnet else "")
            + ") снова в порядке", critical=False)

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

        return notes

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
    # GitHub той же меткой в аплинк: с него агент обновляется, а в юрисдикции
    # шлюза он без туннеля недоступен. Список — как в routing-gw-setup.sh.
    GH_RANGES = ("140.82.112.0/20", "143.55.64.0/20", "185.199.108.0/22", "192.30.252.0/22")

    _guard_info: dict | None = None
    _REASSERT_MIN_INTERVAL = 10 * 60
    _last_reassert: float = float("-inf")   # monotonic: 0.0 на свежезагруженной малине откладывал бы первый реассерт

    def tg_mark_missing(self, info: dict | None = None) -> list[str]:
        """Диапазоны Telegram, которых нет в set tg_nets4 таблицы. Таблица —
        одно целое: пропали диапазоны — значит пропала (или устарела) вся она."""
        if info is None:
            from awgbot.infra import gwguard
            try:
                info = gwguard.table_info()
            except gwguard.GwGuardError:
                info = None
        if info is None:
            return list(self.TG_RANGES)
        present = info["sets"].get("tg_nets4", set())
        return [n for n in self.TG_RANGES if n not in present]

    def gh_route_check(self, info: dict | None) -> GwCheck:
        """Отдельно от Telegram: реассерт тут не поможет — старый скрипт
        обвязки набора gh_nets4 не знает, нужен новый файл конфигурации с ВПС."""
        present = (info or {}).get("sets", {}).get("gh_nets4", set())
        missing = [n for n in self.GH_RANGES if n not in present]
        return GwCheck("маршрут к GitHub", not missing,
                       "" if not missing else
                       "обвязка без маршрута GitHub в аплинк — агент не сможет обновляться; "
                       "перевыпусти конфигурацию шлюза с сервера AWG и примени её здесь")

    @staticmethod
    def peer_nets_missing(info: dict | None) -> list[str] | None:
        """Чего из PEER_HOME_NETS нет в наборе peer_nets4. None — переменная
        пуста, функции на этом шлюзе нет. Отдельно от проверки, потому что тот
        же ответ нужен снимку для канала — но строкой для человека, собранной
        здесь, там делать нечего."""
        from awgbot.infra import gwguard
        want = gwguard.unit_env("PEER_HOME_NETS").split()
        if not want:
            return None
        present = (info or {}).get("sets", {}).get("peer_nets4", set())
        return [n for n in want if n not in present]

    def peer_nets_check(self, info: dict | None) -> GwCheck | None:
        """Подсети за другими шлюзами: набор
        peer_nets4 против PEER_HOME_NETS из юнита. Переменная пустая — проверки
        нет: функции на этом шлюзе нет."""
        missing = self.peer_nets_missing(info)
        if missing is None:
            return None
        return GwCheck("локальные подсети других шлюзов", not missing,
                       "" if not missing else
                       f"в таблице нет {', '.join(missing)}; перевыпусти конфигурацию шлюза с сервера AWG "
                       "и примени её здесь")

    def tg_mark_ensure(self, missing: list[str] | None = None) -> int:
        """Таблицу правит только скрипт: недостающее восстанавливаем рестартом
        юнита (скрипт идемпотентен, линк без нужды не трогает). Не чаще раза в
        10 минут — иначе устаревший бандл дёргал бы юнит каждый тик."""
        if missing is None:
            missing = self.tg_mark_missing()
        if not missing:
            return 0
        ok = self._reassert_throttled(f"не хватало {len(missing)} диапазонов Telegram")
        return len(missing) if ok else 0

    # ── локальная сеть без VPN ──────────────
    _LAN_LAST_KEY = "gw_lan_counters"       # {"lan": pkts, "dns": pkts, "lan_at": iso, "dns_at": iso}
    _LAN_QUIET_SECONDS = 24 * 3600          # столько без пакетов из LAN — «роутер не заворачивает»
    _LAN_FAILS_KEY = "gw_lan_lists_fails"

    def _reassert_throttled(self, why: str) -> bool:
        """Рестарт юнита обвязки не чаще раза в 10 минут (общий троттлинг с
        tg_mark_ensure): скрипт идемпотентен, вернёт и guard, и awg_home."""
        from awgbot.infra import gwguard
        if time.monotonic() - self._last_reassert < self._REASSERT_MIN_INTERVAL:
            return False
        # Идёт применение (бандл из чата, настройки из канала) — не трогаем:
        # включение режима без VPN минуты стоит на apt, таблицы awg_home в это
        # время ещё нет, и рестарт юнита посреди него убил бы dpkg. Юнит уже
        # в activating — то же самое, только запущено извне.
        if not _APPLY_LOCK.acquire(blocking=False):
            return False
        try:
            if gwguard.unit_state().get("ActiveState") == "activating":
                return False
            self._last_reassert = time.monotonic()
            ok, err = gwguard.reassert()
        finally:
            _APPLY_LOCK.release()
        if ok:
            log.warning("gateway: обвязка перевыставлена: %s", why)
        else:
            log.warning("gateway: реассерт обвязки не удался (%s): %s", why, err)
        return ok

    def _upstream_verdict(self) -> bool | None:
        """Отвечает ли апстрим — по статистике dnsmasq, без запроса наружу.

        За тик росли отказы и не росли успешные отправки — апстрим молчит.
        Запросов не было вовсе (квартира спит) — держим прошлый вердикт: ждать
        ответа некому, а спрашивать ради проверки значит завести маячок."""
        from awgbot.infra import gwguard
        stats = gwguard.upstream_stats()
        if not stats:
            return None
        sent = sum(v[0] for v in stats.values())
        failed = sum(v[1] for v in stats.values())
        prev = self.__dict__.get("_upstream_prev")
        self.__dict__["_upstream_prev"] = (sent, failed)
        if prev is None or sent < prev[0] or failed < prev[1]:
            # первый взгляд (или dnsmasq перезапустился) — по накопленному с
            # его старта: ни одного ответа на все отправленные — апстрим молчит
            if sent == 0:
                return self.__dict__.setdefault("_upstream_ok", True)
            ok = failed < sent
            self.__dict__["_upstream_ok"] = ok
            return ok
        d_sent, d_failed = sent - prev[0], failed - prev[1]
        if d_sent == 0 and d_failed == 0:
            return self.__dict__.get("_upstream_ok", True)
        ok = d_failed < d_sent or d_failed == 0
        self.__dict__["_upstream_ok"] = ok
        return ok

    def lan_status(self) -> tuple[dict, list[GwCheck]]:
        """(блок для панели, проверки) — только при LAN_MODE=1 в юните; иначе
        ({}, []). Счётчики заворота и DNS — по разности с прошлым тиком:
        растут — роутер шлёт; не растут дольше _LAN_QUIET_SECONDS — нет."""
        from awgbot.infra import gwguard
        if not gwguard.lan_mode():
            return {}, []
        st = gwguard.script_status()
        iface, addr = st.get("LAN_IF", ""), st.get("LAN_ADDR", "")
        resolver = gwguard.unit_env("RESOLVER")
        nets_all = gwguard.unit_env("HOME_SUBNETS").split()
        info: dict = {"iface": iface, "addr": addr, "resolver": resolver or "1.1.1.1 (запасной)",
                      "subnet": nets_all[0] if nets_all else "",
                      "uplink": st.get("UPLINK_IF", "")}
        checks: list[GwCheck] = []
        # скрипта списков нет — фиды применить нечем, ни свои, ни из канала
        if not os.path.exists(gwguard.LAN_LISTS_SCRIPT):
            checks.append(GwCheck("скрипт списков", False,
                                  "скрипт обновления списков условной маршрутизации не найден — "
                                  "перевыпусти конфигурацию шлюза"))
        # скрипт обвязки не смог применить раздел — причина в статусе, а не «перевыпусти»
        err = st.get("LAN_ERROR", "")
        if err:
            checks.append(GwCheck("применение локальной сети", False, err))
        # интерфейс переехал (OMV собрал bridge/bond — адрес теперь на br0): правила
        # остались на старом имени, весь LAN идёт мимо маркировки. Перевыставить.
        nets = gwguard.unit_env("HOME_SUBNETS").split()
        if nets and iface:
            live = gwguard.iface_for_subnet(nets[0])
            if live and live[0] != iface:
                self._reassert_throttled(f"локальная сеть переехала {iface} → {live[0]}")
                checks.append(GwCheck("применение локальной сети", False,
                                      f"адрес подсети теперь на {live[0]}, правила стоят на {iface} — перевыставляю"))
        # резолвер
        active = gwguard.dnsmasq_active()
        checks.append(GwCheck("резолвер", active, "" if active else
                              ("dnsmasq не запущен: journalctl -u dnsmasq -e" if active is False
                               else "systemctl не ответил")))
        up = self._upstream_verdict() if active else None
        checks.append(GwCheck("апстрим через аплинк", up,
                              "" if up else
                              (f"{info['resolver']} не отвечает через аплинк: аплинк или резолвер сервера"
                               if up is False else ("резолвер не запущен — не проверяли" if not active
                                                    else "статистика dnsmasq не прочиталась"))))
        # таблица и наборы
        try:
            home = gwguard.home_table_info()
        except gwguard.GwGuardError:
            home = None
        table_ok = home is not None and "prerouting" in home["chains"]
        if not table_ok and not err:
            # таблицу снёс кто-то посторонний (nftables.service с flush ruleset,
            # ручной nft) — вернёт юнит; перевыпуск тут ни при чём
            fixed = self._reassert_throttled("таблицы awg_home нет")
            checks.append(GwCheck("таблица локальной сети", False,
                                  "таблицы awg_home нет — перевыставляю обвязку" if fixed
                                  else "таблицы awg_home нет — перевыставлю обвязку в ближайший такт"))
        else:
            checks.append(GwCheck("таблица локальной сети", table_ok, "" if table_ok else err))
        ls = gwguard.lists_status()
        info["domains"] = int(ls.get("domains") or 0)
        info["nets"] = int(ls.get("nets") or 0)
        info["updated_at"] = ls.get("updated_at", "")
        info["own_vpn"], info["own_ru"] = gwguard.lan_own_lists()
        lists_ok = table_ok and (info["nets"] > 0 or info["domains"] > 0)
        checks.append(GwCheck("списки", lists_ok, "" if lists_ok else
                              "списки не загружены: обновление не прошло"))
        # заворот и DNS с роутера — по росту счётчиков
        if home is not None:
            last = self.db.get_state_json(self._LAN_LAST_KEY, {})
            now = timeutil.now()
            cur = {"lan": home["lan_pkts"], "dns": home["dns_pkts"]}
            for key, name, why in (("lan", "трафик с роутера",
                                     "роутер не маршрутизирует трафик на шлюз (❓ Настройка роутера)"),
                                    ("dns", "DNS с роутера",
                                     "DHCP роутера раздаёт не адрес шлюза")):
                prev = int(last.get(key, -1))
                at = last.get(f"{key}_at") or ""
                if prev < 0 or cur[key] > prev or cur[key] < prev:      # первый тик / рост / сброс
                    at = timeutil.to_iso(now)
                    ok: bool | None = True if prev >= 0 else None
                    detail = "" if prev >= 0 else "первый замер"
                else:
                    try:
                        quiet = (now - timeutil.parse_iso(at)).total_seconds() if at else 0.0
                    except ValueError:
                        quiet = 0.0
                    ok = quiet < self._LAN_QUIET_SECONDS
                    detail = "" if ok else (f"пакетов из локальной сети нет с "
                                            f"{timeutil.fmt_dt(timeutil.parse_iso(at)) if at else '?'}: {why}")
                info[f"{key}_pkts"] = cur[key]
                last[key] = cur[key]
                last[f"{key}_at"] = at
                checks.append(GwCheck(name, ok, detail))
            if json.dumps(last) != (self.db.get_state(self._LAN_LAST_KEY) or ""):
                self.db.set_state(self._LAN_LAST_KEY, json.dumps(last))   # запись только при изменении
        for c in checks:
            c.group = "lan"
        return info, checks

    def lan_domains(self, cmd: str, domains: list[str]) -> tuple[bool, str]:
        """Свои списки из чата: add | ru | del. Разбор и денилист — в скрипте.
        Счётчики в снимке панели — сразу, не ждать тика монитора."""
        from awgbot.infra import gwguard
        ok, out = gwguard.run_lan_domain(cmd, domains)
        if ok:
            self.lan_own_counts_refresh()
        return ok, out

    def lan_own_counts_refresh(self) -> None:
        """Обновить в снимке последнего тика счётчики своих списков: панель
        рисуется из снимка, и после правки списка показывала бы прежние числа
        до следующего тика монитора."""
        from awgbot.infra import gwguard
        raw = self.db.get_state(self._SNAPSHOT_KEY)
        if not raw:
            return
        try:
            st = GwStatus.from_json(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            return
        lan = getattr(st, "lan", None)
        if not lan:
            return
        lan["own_vpn"], lan["own_ru"] = gwguard.lan_own_lists()
        self.db.set_state(self._SNAPSHOT_KEY, st.to_json())

    def lan_own_lists(self) -> list[tuple[str, str]]:
        """[(vpn|ru, домен)] из скрипта списков."""
        from awgbot.infra import gwguard
        ok, out = gwguard.run_lan_domain("list", [])
        return [(k, d) for d, k in gwownlists.parse_list(out).items()] if ok else []

    def lan_lists_now(self) -> tuple[bool, str]:
        """Обновить списки (кнопка и задача): (ok, хвост вывода); счётчик
        провалов подряд ведётся здесь, чтобы ручной успех его сбрасывал."""
        from awgbot.infra import gwguard
        ok, tail = gwguard.run_lan_lists()
        fails = 0 if ok else int(self.db.get_state(self._LAN_FAILS_KEY) or 0) + 1
        self.db.set_state(self._LAN_FAILS_KEY, str(fails))
        return ok, tail

    def lan_router_params(self) -> tuple[str, str, list[str]]:
        """(подсеть, адрес шлюза, локальные подсети других шлюзов) для рецепта
        роутера — их знает только малина: подсети из юнита обвязки, адрес из
        статуса скрипта."""
        from awgbot.infra import gwguard
        nets = gwguard.unit_env("HOME_SUBNETS").split()
        peers = gwguard.unit_env("PEER_HOME_NETS").split()
        return (nets[0] if nets else ""), gwguard.script_status().get("LAN_ADDR", ""), peers

    # ── первый выход на связь после установки — серверу каналом ─────────────
    _FIRST_START_KEY = "agent_first_start_at"
    _INSTALLED_SENT_KEY = "gwlink_installed_sent"
    _INSTALLED_REPORT_WINDOW_S = 3600

    def first_start_note(self, fresh_db: bool) -> None:
        """Отметить первый запуск агента — только когда базы до этого запуска не
        было (её создал установщик). Обновлённый со старой версии агент отметки
        не имеет и не получает: иначе первый запуск после обновления сошёл бы за
        установку, и админ получил бы «успешно настроен» по каждому шлюзу.
        Повторные запуски отметку не двигают."""
        if fresh_db and not self.db.get_state(self._FIRST_START_KEY):
            self.db.set_state(self._FIRST_START_KEY, timeutil.to_iso(timeutil.now()))

    def installed_report_pending(self) -> bool:
        """Сказать ли серверу «установлен»: один раз, и только если агент
        впервые запустился меньше часа назад. Канала в этот час не было —
        уведомления не будет вовсе: «шлюз настроен» спустя дни только пугает."""
        if self.db.get_state(self._INSTALLED_SENT_KEY):
            return False
        try:
            age = (timeutil.now() - timeutil.parse_iso(self.db.get_state(self._FIRST_START_KEY) or "")).total_seconds()
        except ValueError:
            return False
        return age <= self._INSTALLED_REPORT_WINDOW_S

    def installed_report_done(self) -> None:
        self.db.set_state(self._INSTALLED_SENT_KEY, timeutil.to_iso(timeutil.now()))

    # ── итог применения файла из чата — серверу каналом ──────────────────────
    _APPLIED_PENDING_KEY = "gwlink_applied_pending"

    _APPLIED_PENDING_TTL_S = 24 * 3600

    def applied_pending_set(self, ok: bool, error: str, fp: str = "") -> None:
        """Итог ждёт отправки: канал после применения бандла обычно
        переподключается, а сказать серверу надо ровно один раз. fp — отпечаток
        применённого файла: сервер по нему узнаёт, о каком файле речь."""
        self.db.set_state(self._APPLIED_PENDING_KEY,
                          json.dumps({"ok": bool(ok), "error": " ".join((error or "").split())[:300],
                                      "fp": str(fp or "")[:32],
                                      "at": timeutil.to_iso(timeutil.now())}, ensure_ascii=False))

    def applied_pending_get(self) -> dict:
        """Итог в очереди; просроченный (сутки без канала) — как пустой: столько
        у файла на сервере никто не ждёт, а сервер без ответа его и не убрал бы."""
        data = self.db.get_state_json(self._APPLIED_PENDING_KEY, {})
        if "ok" not in data:
            return {}
        try:
            age = (timeutil.now() - timeutil.parse_iso(str(data.get("at") or ""))).total_seconds()
        except ValueError:
            return {}                                 # без времени — как просроченный
        return {} if age > self._APPLIED_PENDING_TTL_S else data

    def applied_pending_clear(self) -> None:
        self.db.set_state(self._APPLIED_PENDING_KEY, "")

    # ── свои списки, общие для всех шлюзов ──
    # Не настраивается: действует при режиме без VPN и канале линка. Правки
    # (кнопка, `awg-bot lan`, файл руками) сверкой становятся событиями и уходят
    # серверу; его канон применяется скриптом `sync` — только когда все свои
    # правки в нём учтены, иначе правки уходят ещё раз.
    _OWN_BASE_KEY = "gw_own_base"            # последний применённый канон: {gen, ver, hash, items}
    _OWN_PENDING_KEY = "gw_own_pending"      # свои правки, ещё не в каноне: {run, n, ev, sent_at}
    _OWN_FP_KEY = "gw_own_fp"                # отпечаток файлов на момент последней сверки
    _OWN_ERR_KEY = "gw_own_err"              # хвост ошибки последнего применения
    _OWN_REJ_KEY = "gw_own_rej"              # что сервер отверг из последнего пакета: [[домен, причина]]
    _OWN_DEFER_KEY = "gw_own_defer"          # канон, отложенный из-за обвязки старого образца
    _OWN_STALE_SERVER_S = 600                # правки без ответа сервера дольше — «обнови основной бот»
    _own_run: str = ""                       # метка запуска: номера правок — в её пределах

    def own_active(self) -> bool:
        from awgbot.infra import gwguard
        return gwguard.lan_mode() and gwguard.unit_env("LINK_CHANNEL") == "1"

    def own_base(self) -> dict:
        return self.db.get_state_json(self._OWN_BASE_KEY, {})

    def _own_pending_raw(self) -> dict:
        p = self.db.get_state_json(self._OWN_PENDING_KEY, {})
        if not isinstance(p.get("ev"), list):
            p["ev"] = []
        return p

    def _own_run_id(self) -> str:
        if not self._own_run:
            type(self)._own_run = secrets.token_hex(6)
        return self._own_run

    def _own_pending_set(self, p: dict) -> None:
        """Очередь правок. Пустая очередь хранит run и n: номера правок в
        пределах запуска только растут, иначе после применения канона следующая
        правка ушла бы с n=1 и сервер отбросил бы её как повтор."""
        keep = {"run": p.get("run") or "", "n": int(p.get("n") or 0), "ev": p.get("ev") or []}
        if p.get("sent_at") and keep["ev"]:
            keep["sent_at"] = p["sent_at"]
        self.db.set_state(self._OWN_PENDING_KEY, json.dumps(keep, ensure_ascii=False)
                          if (keep["ev"] or keep["run"]) else "")

    def _own_files_fp(self) -> str:
        """sha256 обоих файлов своих списков: два чтения, без exec."""
        from awgbot.infra import gwguard
        h = hashlib.sha256()
        for name in gwguard.OWN_LIST_FILES:
            try:
                with open(os.path.join(gwguard.DNSMASQ_D, name), "rb") as f:
                    h.update(f.read())
            except OSError:
                h.update(b"-")
            h.update(b"\0")
        return h.hexdigest()

    def own_local(self) -> dict[str, str] | None:
        """{домен: вид} из файлов через скрипт; None — файлов нет (режим
        выключен, раздел не применился): отсутствие файла удалением не считается."""
        from awgbot.infra import gwguard
        if not all(os.path.exists(os.path.join(gwguard.DNSMASQ_D, n)) for n in gwguard.OWN_LIST_FILES):
            return None
        ok, out = gwguard.run_lan_domain("list", [])
        return gwownlists.parse_list(out) if ok else None

    def _own_add_events(self, changes, init: bool = False) -> list:
        """Дописать правки в pending под текущей меткой запуска; вернуть их."""
        p = self._own_pending_raw()
        run = self._own_run_id()
        if p.get("run") != run:
            # неотправленные правки прошлого запуска — под новую метку, с единицы
            p = {"run": run, "n": 0, "ev": [[i + 1, e[1], e[2], e[3] if len(e) > 3 else False]
                                            for i, e in enumerate(p.get("ev") or []) if len(e) >= 3]}
            p["n"] = len(p["ev"])
        new = []
        for d, kind in changes:
            p["n"] = int(p.get("n") or 0) + 1
            ev = [p["n"], d, kind, bool(init)]
            p["ev"].append(ev)
            new.append(ev)
        if new:
            p.pop("sent_at", None)
        self._own_pending_set(p)
        return new

    def own_unsent(self) -> bool:
        """Есть правки, которые ещё не уходили серверу в этой сессии: уже
        отправленные повторно на каждом тике не шлём — это был бы маячок;
        их повторит подключение (hello) или ответ сервера."""
        p = self._own_pending_raw()
        return bool(p.get("ev")) and not p.get("sent_at")

    def own_reconcile(self) -> bool:
        """Сверка файлов с базой ⊕ pending (тик монитора, после кнопки): нашлась
        правка мимо канона — событием в pending. True — нашлись новые правки.
        Базы нет — не сверяем: первый список уйдёт целиком с init по канону."""
        if not self.own_active():
            return False
        fp = self._own_files_fp()
        if fp == (self.db.get_state(self._OWN_FP_KEY) or ""):
            return False
        base = self.own_base()
        local = self.own_local()
        if local is None:
            return False
        self.db.set_state(self._OWN_FP_KEY, fp)
        if not base:
            return False
        expected = gwownlists.apply_events(base.get("items") or {}, self._own_pending_raw().get("ev"))
        changes = gwownlists.diff(local, expected)
        if changes:
            self._own_add_events(changes)
            log.info("свои списки: правок мимо канона — %s, уходят серверу", len(changes))
        return bool(changes)

    def own_pending_events(self) -> tuple[str, list]:
        """(run, события) для отправки серверу; пусто — ("", []). Момент первой
        отправки запоминается: по нему считается «сервер не отвечает»."""
        p = self._own_pending_raw()
        if not p.get("ev"):
            return "", []
        if p.get("run") != self._own_run_id():
            self._own_add_events([])          # перенумеровать под текущую метку
            p = self._own_pending_raw()
        if not p.get("sent_at"):
            p["sent_at"] = timeutil.to_iso(timeutil.now())
            self._own_pending_set(p)
        return str(p.get("run") or ""), [list(e) for e in p["ev"]]

    def own_applied_hash(self) -> str:
        return str(self.own_base().get("hash") or "")

    def own_rejected_in(self, rej) -> None:
        rows = [[str(r[0])[:64], str(r[1])[:64]] for r in (rej or [])
                if isinstance(r, (list, tuple)) and len(r) >= 2][:20]
        self.db.set_state(self._OWN_REJ_KEY, json.dumps(rows, ensure_ascii=False) if rows else "")

    def apply_own_lists(self, body: dict) -> dict:
        """Канон от сервера → файлы через `sync`: {ok, hash, n, error, skipped}.
        skipped — канон не применён по правилам сверки (правки не учтены, первая
        синхронизация, откат сервера): свои правки уходят ещё раз, ack не шлётся."""
        from awgbot.infra import gwguard
        digest = gwlink.clean_hex(body.get("hash"))
        gen = str(body.get("gen") or "")[:32]
        try:
            ver = int(body.get("ver") or 0)
        except (TypeError, ValueError):
            ver = 0
        items = gwownlists.clean(body.get("items") if isinstance(body.get("items"), list) else [])
        upto = body.get("upto") if isinstance(body.get("upto"), (list, tuple)) else ["", 0]
        self.own_rejected_in(body.get("rej"))
        if not gwguard.lan_mode():
            return self._own_store(digest, None, False, "режим «За шлюзом — без VPN» на шлюзе выключен")
        base = self.own_base()
        local = self.own_local()
        if local is None:
            return self._own_store(digest, None, False, "файлов своих списков нет — раздел не применился")
        # первая синхронизация или откат сервера: весь свой список — событиями
        # init, один раз; когда сервер их разберёт (upto покроет), канон применится.
        # Список пуст — отправлять нечего, канон применяется сразу: иначе такой
        # шлюз после переустановки сервера не применил бы его никогда
        p = self._own_pending_raw()
        first = bool(local) and (not base or gen != base.get("gen") or ver < int(base.get("ver") or 0))
        if first and not any(len(e) > 3 and e[3] for e in p.get("ev") or []):
            self._own_add_events(sorted(local.items()), init=True)
            log.info("свои списки: первая синхронизация — %s доменов уходят серверу", len(local))
            return {"ok": False, "skipped": "init", "hash": digest, "n": 0, "error": ""}
        # свои правки, которых в каноне ещё нет, — канон не применяем
        run, n_up = str(upto[0] or ""), int(upto[1] or 0)
        if any(e for e in p.get("ev") or [] if p.get("run") != run or int(e[0]) > n_up):
            return {"ok": False, "skipped": "pending", "hash": digest, "n": 0, "error": ""}
        if not gwguard.lan_domain_has_sync():
            err = self._defer_for_reassert("скрипт своих списков без sync", self._OWN_DEFER_KEY, body,
                                           "скрипт своих списков старого образца")
            if not gwguard.lan_domain_has_sync():
                return self._own_store(digest, None, False, err)
            self.db.set_state(self._OWN_DEFER_KEY, "")     # реассерт уже положил скрипт
        canon = {"gen": gen, "ver": ver, "hash": digest, "items": items}
        if items == local:
            return self._own_store(digest, canon, True, "")
        try:
            os.makedirs(os.path.dirname(gwguard.OWN_LISTS_NEW), mode=0o700, exist_ok=True)
            with open(gwguard.OWN_LISTS_NEW, "w", encoding="utf-8") as f:
                f.write(gwownlists.render_sync(items))
        except OSError as e:
            return self._own_store(digest, None, False, f"файл для sync не записан: {e}")
        ok, tail = gwguard.run_lan_domain("sync", [gwguard.OWN_LISTS_NEW])
        if ok:
            # база — то, что реально записано: скрипт мог отбросить хост Endpoint
            written = self.own_local()
            canon["items"] = items if written is None else written
        result = self._own_store(digest, canon if ok else None, ok,
                                 "" if ok else (tail or "скрипт своих списков отказал без объяснений"))
        if ok:
            log.info("свои списки: применён канон сервера (%s)", len(canon["items"]))
            # адреса новых доменов «в туннель» — в набор фоном (клиент канала
            # зовёт `fill`): dig по каждому не должен держать ни блокировку, ни канал
            new_vpn = sorted(d for d, k in canon["items"].items() if k == "vpn" and local.get(d) != "vpn")
            if new_vpn:
                try:
                    with open(gwguard.OWN_FILL_NEW, "w", encoding="utf-8") as f:
                        f.write("\n".join(new_vpn) + "\n")
                    result["fill"] = gwguard.OWN_FILL_NEW
                except OSError as e:
                    log.warning("свои списки: файл для fill не записан: %s", e)
        return result

    def own_fill(self, path: str) -> tuple[bool, str]:
        """Адреса новых доменов «в туннель» — в набор lan_vpn4 (режим fill
        скрипта): долго (dig по каждому), поэтому зовётся фоном после own_ack."""
        from awgbot.infra import gwguard
        return gwguard.run_lan_domain("fill", [path], timeout=gwguard.OWN_FILL_TIMEOUT)

    def _defer_for_reassert(self, why: str, key: str, payload, noun: str) -> str:
        """Обвязка старого образца (обновление агента положило новые скрипты, а
        юнит их не запускал): перевыставить обвязку — не чаще раза в 10 минут —
        и отложить присланное под key до повтора на тике; сервер второй раз за
        сессию то же не пришлёт. Возвращает текст ошибки для панели."""
        fixed = self._reassert_throttled(why)
        self.db.set_state(key, json.dumps(payload, ensure_ascii=False))
        return f"{noun} — обвязка перевыставляется" if fixed else f"{noun} — обвязка перевыставится в ближайшие минуты"

    def _own_store(self, digest: str, canon: dict | None, ok: bool, err: str) -> dict:
        with self.db.transaction():
            if ok and canon is not None:
                self.db.set_state(self._OWN_BASE_KEY, json.dumps(canon, ensure_ascii=False))
                p = self._own_pending_raw()
                self._own_pending_set({"run": p.get("run"), "n": p.get("n"), "ev": []})
                self.db.set_state(self._OWN_DEFER_KEY, "")
                self.db.set_state(self._OWN_FP_KEY, self._own_files_fp())
            self.db.set_state(self._OWN_ERR_KEY, err)
        return {"ok": ok, "hash": digest, "n": len((canon or {}).get("items") or {}), "error": err}

    def own_retry(self) -> dict | None:
        """Отложенный канон (обвязка была старого образца): скрипт обновился —
        применить; None — повторять нечего."""
        from awgbot.infra import gwguard
        raw = self.db.get_state(self._OWN_DEFER_KEY) or ""
        if not raw:
            return None
        if not gwguard.lan_domain_has_sync():
            # первый реассерт мог не пройти (юнит был занят, троттлинг) —
            # пробуем снова, пока отложенное ждёт
            self._reassert_throttled("скрипт своих списков без sync")
            if not gwguard.lan_domain_has_sync():
                return None
        body = self.db.get_state_json(self._OWN_DEFER_KEY, {})
        self.db.set_state(self._OWN_DEFER_KEY, "")
        if not body.get("gen"):
            return None                               # мусор или пустая запись — применять нечего
        result = self.apply_own_lists(body)
        return None if result.get("skipped") else result

    def own_status(self) -> tuple[dict, list[GwCheck]]:
        """(блок панели, проверки группы «own»): видны в мониторе, уведомлений не
        шлют. state: synced | pending | no_link | stale_server | failed | rejected | old_script | off."""
        from awgbot.infra import gwguard
        info: dict = {"active": self.own_active()}
        checks: list[GwCheck] = []
        if not info["active"]:
            info["state"] = "off"
            # режим есть, канала нет — экран списков скажет, почему они только этого шлюза
            info["no_channel"] = gwguard.lan_mode() and gwguard.unit_env("LINK_CHANNEL") != "1"
            return info, checks
        p = self._own_pending_raw()
        info["pending"] = len(p.get("ev") or [])
        info["err"] = self.db.get_state(self._OWN_ERR_KEY) or ""
        info["rej"] = self.db.get_state_json(self._OWN_REJ_KEY, [])
        info["synced"] = bool(self.own_base())
        online = bool(getattr(getattr(self, "channel", None), "online", False))
        info["online"] = online
        waited = 0.0
        if p.get("sent_at"):
            try:
                waited = (timeutil.now() - timeutil.parse_iso(p["sent_at"])).total_seconds()
            except ValueError:
                waited = 0.0
        from awgbot.bot.texts.fmt import plural_ru
        n = info["pending"]
        n_word = f"{n} " + plural_ru(n, "правка", "правки", "правок")
        if not gwguard.lan_domain_has_sync():
            info["state"] = "old_script"
            checks.append(GwCheck("свои списки", None, "скрипт старого образца, обвязка перевыставляется"))
        elif info["err"]:
            info["state"] = "failed"
            checks.append(GwCheck("свои списки", False, f"не применились: {info['err']}"))
        elif n and online is False:
            info["state"] = "no_link"
            checks.append(GwCheck("свои списки", None, f"ждут синхронизации ({n_word}): нет связи с сервером AWG"))
        elif n and waited > self._OWN_STALE_SERVER_S:
            info["state"] = "stale_server"
            checks.append(GwCheck("свои списки", None,
                                  f"сервер AWG не отвечает на правки {int(waited // 60)} мин: обнови основной бот"))
        elif n:
            info["state"] = "pending"
            checks.append(GwCheck("свои списки", None, f"ждут синхронизации ({n_word}): сервер AWG ещё не ответил"))
        elif info["rej"]:
            info["state"] = "rejected"
            k = len(info["rej"])
            d, why = info["rej"][0]
            checks.append(GwCheck("свои списки", None,
                                  f"сервер AWG не принял {k} " + plural_ru(k, "домен", "домена", "доменов")
                                  + f": {d} — {why}"))
        else:
            info["state"] = "synced"
            checks.append(GwCheck("свои списки", True, "синхронизированы с сервером AWG"
                                  if info["synced"] else "ждут первой синхронизации с сервером AWG"))
        for c in checks:
            c.group = "own"
        return info, checks

    # ── сервисы соседних сетей ────────────
    # Не настраивается: работает там и тогда, где работает доступ между
    # подсетями (PEER_HOME_NETS в юните) при режиме без VPN и канале линка.
    _SVC_LOCAL_KEY = "gw_svc_local"          # свой список после обзора, JSON
    _SVC_PEER_KEY = "gw_peer_svc"            # применённый чужой: {"hash", "items"}
    _SVC_PEER_ERR_KEY = "gw_peer_svc_err"    # последняя ошибка применения
    _SVC_PENDING_KEY = "gw_peer_svc_pending" # не применённое из-за обвязки: {"hash","items"} — повтор
    _SVC_MISSES = 3                          # обзоров подряд без сервиса — снимаем

    def services_active(self) -> bool:
        from awgbot.infra import gwguard
        return (gwguard.lan_mode() and bool(gwguard.unit_env("PEER_HOME_NETS").split())
                and gwguard.unit_env("LINK_CHANNEL") == "1")

    def services_local(self) -> list[dict]:
        """SMB-серверы этой сети, найденные обзором (после чистки)."""
        return [r for r in self.db.get_state_json(self._SVC_LOCAL_KEY, []) if isinstance(r, dict)]

    def services_scan(self) -> bool:
        """Обзор mDNS своей сети. True — свой список изменился, пора слать серверу.
        Гистерезис: появление — сразу, исчезновение — после _SVC_MISSES обзоров
        подряд (уснувший NAS не роняет кэш DNS соседей рестартами dnsmasq);
        обзор, не удавшийся вовсе, промахом не считается."""
        from awgbot.infra import gwguard
        prev = self.services_local()
        if not self.services_active():
            self.__dict__.pop("_svc_misses", None)
            if prev:
                self.db.set_state(self._SVC_LOCAL_KEY, "[]")
                return True
            return False
        out = gwguard.avahi_browse()
        if out is None:
            return False
        nets = gwguard.unit_env("HOME_SUBNETS").split()
        found = {(r["a"], r["p"]): r for r in gwservices.parse_avahi(out, nets)}
        misses: dict = self.__dict__.setdefault("_svc_misses", {})
        cur: list[dict] = []
        for r in prev:
            key = (r.get("a"), r.get("p"))
            if key in found:
                misses.pop(key, None)
                cur.append(found.pop(key))
            else:
                misses[key] = misses.get(key, 0) + 1
                if misses[key] < self._SVC_MISSES:
                    cur.append(r)
                else:
                    misses.pop(key, None)
        cur += list(found.values())
        cur = gwservices.clean(cur, nets, gwservices.MAX_OWN)
        if cur == gwservices.clean(prev, nets, gwservices.MAX_OWN):
            return False
        self.db.set_state(self._SVC_LOCAL_KEY, json.dumps(cur, ensure_ascii=False))
        log.info("gateway: SMB-серверы этой сети: %s", len(cur))
        return True

    def services_peer(self) -> dict:
        return self.db.get_state_json(self._SVC_PEER_KEY, {})

    def services_applied_hash(self) -> str:
        """Отпечаток применённых записей соседей — в hello канала."""
        return str(self.services_peer().get("hash") or "")

    def apply_peer_services(self, digest: str, items) -> dict:
        """Записи соседей от сервера → dnsmasq: {ok, error, n}. Данные с ВПС —
        недоверенные: чистка по подсетям соседей из ЮНИТА, сборка файла из
        шаблона, построчный белый список в помощнике. Тот же файл, что стоит,
        — без рестарта dnsmasq; пустой список — файл снимается."""
        from awgbot.infra import gwguard
        digest = gwlink.clean_hex(digest)
        if not gwguard.lan_mode():
            return self._svc_store(digest, [], False, "режим «За шлюзом — без VPN» на шлюзе выключен")
        peers = gwguard.unit_env("PEER_HOME_NETS").split()
        clean_items = gwservices.clean(items, peers, gwservices.MAX_PEER) if peers else []
        text = gwservices.render_dnsmasq(clean_items, gwguard.unit_env("HOME_SUBNETS").split(), digest)
        try:
            with open(gwguard.PEER_SERVICES_CONF, encoding="utf-8") as f:
                current = f.read()
        except OSError:
            current = ""
        if text == current:
            return self._svc_store(digest, clean_items, True, "")
        if not os.path.exists(gwguard.LAN_SERVICES_SCRIPT):
            err = self._defer_for_reassert("помощника сервисов соседей нет", self._SVC_PENDING_KEY,
                                           {"hash": digest, "items": clean_items}, "скрипта записей SMB нет")
            if not os.path.exists(gwguard.LAN_SERVICES_SCRIPT):
                return self._svc_store(digest, [], False, err)
            self.db.set_state(self._SVC_PENDING_KEY, "")    # реассерт уже положил помощник
        if not text:
            ok, tail = gwguard.run_lan_services("")
        else:
            # второй рубеж перед помощником: файл собран из шаблона, но данные — с ВПС
            if not gwservices.lines_ok(text):
                return self._svc_store(digest, [], False, "записи SMB не прошли проверку строк")
            try:
                os.makedirs(os.path.dirname(gwguard.PEER_SERVICES_NEW), mode=0o700, exist_ok=True)
                with open(gwguard.PEER_SERVICES_NEW, "w", encoding="utf-8") as f:
                    f.write(text)
            except OSError as e:
                return self._svc_store(digest, [], False, f"файл записей SMB не записан: {e}")
            ok, tail = gwguard.run_lan_services(gwguard.PEER_SERVICES_NEW)
        return self._svc_store(digest, clean_items, ok,
                               "" if ok else (tail or "скрипт записей SMB отказал без объяснений"))

    def _svc_store(self, digest: str, items: list, ok: bool, err: str) -> dict:
        with self.db.transaction():
            if ok:
                self.db.set_state(self._SVC_PEER_KEY,
                                  json.dumps({"hash": digest, "items": items}, ensure_ascii=False))
                self.db.set_state(self._SVC_PENDING_KEY, "")
            self.db.set_state(self._SVC_PEER_ERR_KEY, err)
        return {"ok": ok, "error": err, "n": len(items)}

    def services_retry(self) -> dict | None:
        """Повтор отложенного применения (помощника не было): помощник появился
        — применить то, что прислал сервер; None — повторять нечего. Итог
        уходит серверу как peer_svc_ack, он его ждёт с тем же отпечатком."""
        from awgbot.infra import gwguard
        raw = self.db.get_state(self._SVC_PENDING_KEY) or ""
        if not raw:
            return None
        if not os.path.exists(gwguard.LAN_SERVICES_SCRIPT):
            self._reassert_throttled("помощника сервисов соседей нет")
            if not os.path.exists(gwguard.LAN_SERVICES_SCRIPT):
                return None
        pending = self.db.get_state_json(self._SVC_PENDING_KEY, {})
        if not pending:
            self.db.set_state(self._SVC_PENDING_KEY, "")
            return None
        digest = str(pending.get("hash") or "")
        result = self.apply_peer_services(digest, pending.get("items") or [])
        if not result.get("ok") and (self.db.get_state(self._SVC_PENDING_KEY) or "") == raw:
            # тот же отказ, не про помощника — второй раз не пробуем
            self.db.set_state(self._SVC_PENDING_KEY, "")
        result["hash"] = digest
        return result

    def services_status(self) -> tuple[dict, list[GwCheck]]:
        """(блок панели, проверки группы «svc»): проверки видны в мониторе, но
        уведомлений не шлют — соседи, которых нет, не авария."""
        from awgbot.infra import gwguard
        info: dict = {"active": self.services_active()}
        checks: list[GwCheck] = []
        if not info["active"]:
            return info, checks
        own = self.services_local()
        info["own"] = [r.get("n", "") for r in own]
        info["browse"] = gwguard.avahi_browse_available()
        info["avahi"] = gwguard.avahi_active()
        # сначала демон: без него обвязка avahi-utils не ставит, и совет про
        # мастер восстановления на малине без NAS был бы пустым
        if info["avahi"] is False:
            checks.append(GwCheck("SMB этой подсети", None,
                                  "avahi-daemon не запущен: SMB-серверы этой подсети не видны из подсетей "
                                  "других шлюзов"))
        elif not info["browse"]:
            checks.append(GwCheck("SMB этой подсети", None,
                                  "нет avahi-browse, пакет avahi-utils (🔧 Мастер восстановления)"))
        else:
            checks.append(GwCheck("SMB этой подсети", True, f"{len(own)} SMB"))
        peer = self.services_peer()
        items = [r for r in (peer.get("items") or []) if isinstance(r, dict)]
        info["peer"] = [r.get("n", "") for r in items]
        info["ever"] = bool(peer)
        info["err"] = self.db.get_state(self._SVC_PEER_ERR_KEY) or ""
        if info["err"]:
            checks.append(GwCheck("SMB подсетей других шлюзов", False,
                                  f"записи не применились: {info['err']}"))
        elif items:
            got = gwguard.dns_local(f"_smb._tcp.{gwservices.BROWSE_DOMAIN}", "PTR")
            ok = None if got is None else bool(got)
            checks.append(GwCheck("SMB подсетей других шлюзов", ok,
                                  f"{len(items)} SMB доступны" if ok else
                                  ("резолвер не отдаёт записи: journalctl -u dnsmasq -e"
                                   if ok is False else "не проверено: dig не ответил")))
        for c in checks:
            c.group = "svc"
        return info, checks

    # ── роль слота и диагностика по каналу ──
    _LINK_ROLE_KEY = "gwlink_role"

    def set_link_role(self, active: bool) -> None:
        """Роль этого шлюза, как её видит сервер: несёт он трафик или в резерве.
        Сам агент узнать её не может — решает автомат переключения на ВПС."""
        self.db.set_state(self._LINK_ROLE_KEY, "active" if active else "standby")

    def link_role(self) -> str:
        """active | standby | "" — сервер не сообщал (канала нет)."""
        return (self.db.get_state(self._LINK_ROLE_KEY) or "").strip()

    # ── фиды локальной сети по каналу ────────
    _LAN_CHANNEL_HASH_KEY = "gwlink_lists_hash"
    _LAN_CHANNEL_AT_KEY = "gwlink_lists_at"
    # Своё скачивание молчит, пока канал привозит фиды: сервер обновляет их раз
    # в шесть часов, запас вдвое — чтобы одна пропущенная доставка не вернула
    # адрес квартиры на GitHub раньше, чем канал успеет исправиться.
    _LAN_CHANNEL_FRESH_S = 12 * 3600
    _LAN_FEED_MAX = 8 * 1024 * 1024

    def lan_feeds_applied_hash(self) -> str:
        """Отпечаток фидов, применённых из канала; пусто — не применяли."""
        return (self.db.get_state(self._LAN_CHANNEL_HASH_KEY) or "").strip()

    def lan_feeds_from_channel_fresh(self) -> bool:
        """Фиды возит канал — своя задача никуда не ходит.

        Пока сессия жива и фиды из канала однажды пришли — да: сервер обновляет
        их сам и везёт, когда они меняются; неизменные фиды — это не повод
        лезть на GitHub с адреса квартиры. Канал оборван — запас 12 часов от
        последнего контакта, дальше агент снова качает сам."""
        if self.channel.online and self.lan_feeds_applied_hash():
            return True
        raw = self.db.get_state(self._LAN_CHANNEL_AT_KEY) or ""
        return raw.isdigit() and time.time() - int(raw) < self._LAN_CHANNEL_FRESH_S

    def lan_feeds_touch(self, digest: str = "") -> None:
        """Отметка «канал подтвердил фиды»: сервер назвал тот же отпечаток или
        сессия только что закрылась — отсюда отсчитывается запас."""
        if digest and digest != self.lan_feeds_applied_hash():
            return
        if self.lan_feeds_applied_hash():
            self.db.set_state(self._LAN_CHANNEL_AT_KEY, str(int(time.time())))

    def apply_lan_feeds(self, digest: str, packed_b64: str) -> dict:
        """Применить фиды, привезённые каналом: {ok, error}.

        Принятое — недоверенные данные с чужой машины, поэтому: потолок размера
        до и после распаковки (сжатая бомба не должна съесть память малины),
        сверка отпечатка с присланным, запись во временный каталог, и дальше те
        же проверки, что и для скачанного, — формат, длина, dnsmasq --test с
        откатом. Сами фиды — только данные: скрипт кладёт их в конфиги dnsmasq и
        в набор nft, ничего из них не исполняется.
        """
        import base64
        import zlib
        from awgbot.infra import gwguard
        if not gwguard.lan_mode():
            return {"ok": False, "error": "режим «За шлюзом — без VPN» на шлюзе выключен"}
        if not os.path.exists(gwguard.LAN_LISTS_SCRIPT):
            return {"ok": False, "error": "скрипта списков нет — примени конфигурацию шлюза"}
        if len(packed_b64 or "") > self._LAN_FEED_MAX:
            return {"ok": False, "error": "фиды больше предела"}
        try:
            packed = base64.b64decode(packed_b64 or "", validate=True)
            d = zlib.decompressobj()
            raw = d.decompress(packed, self._LAN_FEED_MAX)
            if d.unconsumed_tail:
                return {"ok": False, "error": "фиды после распаковки больше предела"}
            data = json.loads(raw.decode())
            domains, nets = str(data["domains"]), str(data["nets"])
        except (ValueError, KeyError, TypeError, zlib.error, UnicodeDecodeError) as e:
            return {"ok": False, "error": f"фиды повреждены: {e}"}
        from awgbot.util import gwlink
        got = gwlink.feeds_hash(domains, nets)
        if got != digest:
            return {"ok": False, "error": "отпечаток фидов не сошёлся"}
        if digest == self.lan_feeds_applied_hash():
            self.db.set_state(self._LAN_CHANNEL_AT_KEY, str(int(time.time())))
            return {"ok": True, "error": ""}
        os.makedirs(gwguard.LAN_FEED_DIR, mode=0o700, exist_ok=True)
        for name, text in (("domains.lst", domains), ("nets.lst", nets)):
            path = os.path.join(gwguard.LAN_FEED_DIR, name)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(path + ".tmp", path)
        ok, tail = gwguard.run_lan_lists(from_dir=gwguard.LAN_FEED_DIR)
        if not ok:
            return {"ok": False, "error": tail or "скрипт списков отказал"}
        with self.db.transaction():
            self.db.set_state(self._LAN_CHANNEL_HASH_KEY, digest)
            self.db.set_state(self._LAN_CHANNEL_AT_KEY, str(int(time.time())))
            self.db.set_state(self._LAN_FAILS_KEY, "0")
        return {"ok": True, "error": ""}

    def lan_lists_needed(self) -> bool:
        """Режим без VPN включён, а списков ещё нет: сразу после применения
        бандла, включившего режим, ждать планового обновления (до шести часов)
        значило бы оставить квартиру с пустыми наборами — «заблокированное»
        шло бы напрямую."""
        from awgbot.infra import gwguard
        if not gwguard.lan_mode():
            return False
        ls = gwguard.lists_status()
        return not (int(ls.get("domains") or 0) or int(ls.get("nets") or 0))

    def lan_lists_update(self) -> list[Notification]:
        """Задача планировщика: обновить списки; два провала подряд — замечание.
        Пока фиды привозит канал — не ходит никуда: адрес квартиры за фидами на
        GitHub и в Google не ходит, это и есть смысл этапа 3. Канал замолчал —
        задача сама вернётся к скачиванию."""
        from awgbot.infra import gwguard
        if not gwguard.lan_mode():
            return []
        if self.lan_feeds_from_channel_fresh():
            log.info("gateway: фиды локальной сети привозит канал — сам не качаю")
            return []
        ok, tail = self.lan_lists_now()
        fails = int(self.db.get_state(self._LAN_FAILS_KEY) or 0)
        if ok:
            log.info("gateway: списки локальной сети обновлены")
            return []
        log.warning("gateway: списки локальной сети не обновились: %s", tail)
        if fails == 2:
            return [Notification(config.ADMIN_ID,
                                 "⚠️ Списки локальной сети не обновились дважды подряд: "
                                 + (html.escape(tail, quote=False) or "без подробностей")
                                 + "\nФиды — через аплинк; проверь "
                                 "монитор здоровья.", critical=False)]
        return []

    # ── операции с кнопки (этап 2) ───────────────────────────────────────────

    def restart_link(self) -> tuple[bool, str]:
        """Мягкий рестарт линка: down/up интерфейса без пересборки обвязки.
        Секунды обрыва RF у всех — поэтому только с подтверждения."""
        # Результат down намеренно не проверяем: интерфейс мог быть уже опущен,
        # и это не отказ — важно только, поднялся ли он обратно.
        _run(["awg-quick", "down", config.GW_LINK_IF], timeout=30)
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
        from awgbot.util import bundlecrypt
        try:
            priv = bundlecrypt.read_privkey(pathlib_read(config.GW_LINK_CONF))
            plain = bundlecrypt.decrypt(blob, priv)
        except (OSError, ValueError) as e:
            return False, f"не удалось применить конфигурацию: {e}"
        text = plain.decode(errors="replace")
        if "#__GW_SETUP_BELOW__" not in text or "__LINK_CONF_EOF__" not in text:
            return False, "не удалось применить конфигурацию: внутри нет маркеров контракта линка"
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
        with _APPLY_LOCK:
            return self._apply_bundle_run(plain)

    @staticmethod
    def _apply_bundle_run(plain: bytes) -> tuple[bool, str]:
        import os, tempfile
        fd, path = tempfile.mkstemp(prefix="awg-gw-bundle-", suffix=".sh", dir="/root")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(plain)
            # 600, как у настроек из канала: первое включение режима без VPN
            # ставит dnsmasq через apt, на малине это минуты
            proc = _run(["sh", path, "--apply"], timeout=600)
            out = (_out(proc) + proc.stderr.decode(errors="replace")).strip()
            tail = "\n".join(out.splitlines()[-6:])
            return proc.returncode == 0, tail
        finally:
            try:
                os.unlink(path)               # внутри приватный ключ — не оставляем
            except OSError:
                pass

    # ── шлюзовое устройство: пометка в основном боте ─────────────────────────
    _GW_MARK_KEY = "gw_mark_status"           # unmarked | confirmed | foreign | unconfirmed | ?

    def gateway_mark_outcome(self) -> dict:
        """После применения бандла: он ли помеченный шлюз. Решение принял
        скрипт обвязки (файл статуса), здесь — перевод в действие: unmarked,
        foreign и unconfirmed означают «переслать claim основному боту»."""
        from awgbot.infra import gwguard
        st = gwguard.script_status()
        status = st.get("GW_STATUS", "?")
        iface, pub = gwguard.uplink_pubkey()
        self.db.set_state(self._GW_MARK_KEY, status)
        out = {"status": status, "uplink": iface, "pubkey": pub, "claim": None}
        if status in ("unmarked", "foreign", "unconfirmed") and pub:
            try:
                out["claim"] = self.gateway_claim_message(pub)
            except (OSError, ValueError) as e:
                log.warning("gateway: claim не собран: %s", e)
        return out

    # ── настройки с сервера по каналу ────────
    _SETTINGS_HUMAN = {"ADMIN_IPS": "устройства админа", "HOME_SUBNETS": "локальные подсети",
                       "LAN_MODE": "режим «За шлюзом — без VPN»",
                       "PEER_HOME_NETS": "локальные подсети других шлюзов", "RESOLVER": "резолвер"}

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
        with _APPLY_LOCK:
            try:
                before = gwguard.unit_set_env({k: want[k] for k in changed})
            except (OSError, gwguard.GwGuardError) as e:
                return {"ok": False, "changed": changed, "error": f"юнит не переписан ({e})"}
            # Запас на включение режима без VPN: скрипт ставит dnsmasq через apt,
            # а на малине это минуты, не секунды.
            ok, err = gwguard.reassert(timeout=600)
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

    def link_settings_note(self, result: dict) -> str:
        """Текст уведомления в чат агента о настройках, пришедших по каналу."""
        what = ", ".join(self._SETTINGS_HUMAN.get(k, k) for k in result.get("changed") or [])
        err = html.escape(str(result.get("error") or "ошибка"), quote=False)
        if result.get("ok"):
            return f"⚙️ Сервер AWG прислал новые настройки шлюза — применены: {what}."
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
        priv = bundlecrypt.read_privkey(pathlib_read(config.GW_LINK_CONF))
        return gwsign.sign(priv, "claim", pubkey, host=socket.gethostname())

    def gateway_apply_report(self) -> str:
        """Человеческий отчёт после применения — из статуса скрипта."""
        from awgbot.infra import gwguard
        from awgbot.bot import texts
        return texts.gateway_apply_report(gwguard.script_status())

    def gateway_mark_status(self) -> str:
        return self.db.get_state(self._GW_MARK_KEY) or "?"

    # ── выход наружу через домашний канал ────────────────────────────────────

    def egress_probe(self) -> float | None:
        """TCP-коннект к российскому и к зарубежному адресу через default-маршрут
        (домашний канал, не туннель). Возвращает время первого удачного, мс;
        None — никто не ответил. Две цели: один внешний хост сам по себе точка
        отказа, и его заминка выглядела бы как отвал канала."""
        targets = [str(t) for t in (settings.get("app.gateway.egress_targets", None)
                                    or ["77.88.8.8", "8.8.8.8"])]
        port = int(settings.get("app.gateway.egress_port", 53))
        if len(targets) == 1:
            return self._egress_one(targets[0], port)
        # Цели независимы — ходим ко всем разом, первый ответ и есть результат:
        # последовательно при лежащем канале тик держал поток 2 × 3 с.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        pool = ThreadPoolExecutor(max_workers=len(targets))
        try:
            futs = [pool.submit(self._egress_one, h, port) for h in targets]
            for f in as_completed(futs):
                ms = f.result()
                if ms is not None:
                    return ms
            return None
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # Обратный трафик клиентов в линк — улика сильнее зонда: он доказывает,
    # что канал квартиры дошёл до интернета и ответы вернулись. Подделать его
    # служебным нечем: в линк агент шлёт только ответы клиентам да keepalive в
    # 32 байта, свой трафик к Telegram он гонит аплинком.
    _EGRESS_RETURN_BYTES = 4096

    def _egress_idle_seconds(self) -> float:
        """Секунд между зондами наружу, когда через линк не ходит никто;
        0 — зондировать каждый тик (как было)."""
        mins = settings.get_int("app.gateway.monitor_minutes", 3)
        return mins * 60 * float(settings.get("app.gateway.egress_idle_multiplier", 4) or 0)

    def _minus_channel(self, link_rx: int, link_tx: int) -> tuple[int, int]:
        """Счётчики линка за вычетом собственного канала (снимки, ответы,
        фиды): иначе ответ канала серверу сходил бы за ответы из
        интернета, ушедшие клиентам, и выход наружу доказывался бы трафиком,
        который никуда наружу не ходил."""
        return self.channel.minus(0, link_rx, link_tx)

    def _egress_verdict(self, link_rx: int, link_tx: int) -> tuple[bool, float | None, str]:
        """(есть ли выход наружу, мс последнего замера, чем доказано).

        Зонд — коннект С АДРЕСА КВАРТИРЫ к двум фиксированным целям, тик за
        тиком: четыре с лишним сотни одинаковых коннектов в сутки, то есть
        ровно тот маячок, про который мы сами пишем «строго периодический
        коннект с домашнего адреса — сигнатура для ТСПУ». Поэтому сначала
        улики, и только потом зонд.

        Вырос tx линка — ответы из интернета дошли и ушли клиентам: путь
        доказан даром. Вырос rx без tx — клиенты шлют, а обратно тихо: это и
        есть отказ канала, зондируем немедленно. Не ходит никто — зондируем
        редко и с джиттером: ждать ответа в простое всё равно некому.
        """
        import random
        # Перезапуск линка — только по СЫРЫМ счётчикам: за вычетом канала они
        # могут и убывать (накладные взяты с запасом), и такое убывание
        # сходило бы за перезапуск — зонд каждым тактом, пока идут фиды.
        last_raw = self.__dict__.get("_egress_raw")
        self.__dict__["_egress_raw"] = (link_rx, link_tx)
        restarted = last_raw is not None and (link_rx < last_raw[0] or link_tx < last_raw[1])
        link_rx, link_tx = self._minus_channel(link_rx, link_tx)
        seen = self.__dict__.get("_egress_seen")
        now = time.monotonic()
        every = self._egress_idle_seconds()

        def _probe() -> tuple[bool, float | None, str]:
            ms = self.egress_probe()
            self.__dict__["_egress_seen"] = {
                "rx": link_rx, "tx": link_tx, "ms": ms, "ok": ms is not None,
                "next": now + every * random.uniform(0.6, 1.4)}
            return ms is not None, ms, "проба"

        # Счётчики сбрасывает подъём линка: «ушли вниз» — не отказ канала, а
        # перезапуск awg-quick, и сравнивать больше не с чем.
        if seen is None or restarted:
            return _probe()
        returned = link_tx - seen["tx"] > self._EGRESS_RETURN_BYTES
        demand = link_rx - seen["rx"] > self._EGRESS_RETURN_BYTES
        if returned:
            # Такт зонда отодвигаем, как после зонда: улика — доказательство
            # СИЛЬНЕЕ пробы, и после неё ждать столько же честно. Иначе первый
            # же тик после конца трафика уходил бы зондом, а конец трафика —
            # это обычный вечер, а не отказ.
            seen.update(rx=link_rx, tx=link_tx, ok=True,
                        next=now + every * random.uniform(0.6, 1.4))
            return True, seen["ms"], "трафик"
        if demand or every <= 0 or now >= seen.get("next", 0.0):
            return _probe()
        seen.update(rx=link_rx, tx=link_tx)
        return seen["ok"], seen["ms"], "кэш"

    @staticmethod
    def _egress_one(host: str, port: int) -> float | None:
        t0 = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=3.0):
                return round((time.monotonic() - t0) * 1000, 1)
        except OSError:
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
        # Локальное состояние файервола (порт, адреса снаружи, доверенные из
        # туннеля) — данные человека, бандл их не восстановит.
        from awgbot.infra import gwguard
        try:
            extra.append(("awg-gw/firewall.env", open(gwguard.FW_ENV, "rb").read()))
        except OSError:
            pass
        # Личные списки локальной сети без VPN — тоже данные человека: набраны
        # руками в чате агента, и замена малины без копии теряла бы их.
        for name in ("awg-gw-vpn-user.conf", "awg-gw-ru-user.conf"):
            try:
                extra.append((f"awg-gw/lan/{name}", open(f"/etc/dnsmasq.d/{name}", "rb").read()))
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
                or "сервера AWG")

    # ── потребление за месяц ─────────────────────────────────────────────────

    _TRAFFIC_KEY = "gw_traffic"

    def _account_traffic(self, rx: int, tx: int) -> tuple[int, int]:
        """Счётчики линка живут от подъёма интерфейса и обнуляются каждым
        рестартом. Копим дельты в месячный итог: счётчик меньше прошлого —
        значит, обнулился, и дельта — весь текущий. Новый календарный месяц
        начинает итог заново."""
        month = timeutil.now().strftime("%Y-%m")
        acc = self.db.get_state_json(self._TRAFFIC_KEY, {})
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

    # Статические пробы — modinfo, обход ядер, SMART — меняются руками и редко;
    # по тику берём из кэша, а «Статус»/«Монитор здоровья»/старт снимают
    # живьём (fresh_static). Прежний кэш убирали за то, что после обновления
    # модуля он врал 10 минут; теперь у него есть сброс по кнопке.
    _STATIC_TTL = 45 * 60

    def invalidate_static(self) -> None:
        """Сбросить кэш статических проб: «Статус», «Монитор здоровья», старт."""
        self.__dict__.pop("_static_cache", None)
        self.__dict__.pop("_unit_enabled_cache", None)
        self.invalidate_ssh_static()

    def _static(self) -> tuple[tuple[str, str], tuple[list[str], int], str | None]:
        from awgbot.runtime import hostmetrics
        c = self.__dict__.get("_static_cache")
        if c and time.monotonic() - c[0] < self._STATIC_TTL:
            return c[1], c[2], c[3]
        ver, cov, smart = self.versions(), self.kernel_coverage(), hostmetrics.read_smart_health()
        self.__dict__["_static_cache"] = (time.monotonic(), ver, cov, smart)
        return ver, cov, smart

    def status(self) -> GwStatus:
        """Живой снимок: линк, монитор здоровья, железо. Панель по /start берёт
        снимок тика (cached_status), живьём ходят «Статус», «Монитор здоровья»
        и сам тик; редко меняющееся (модуль, ядра, SMART) — из кэша, который
        кнопки сбрасывают через invalidate_static()."""
        from awgbot.runtime import hostmetrics
        st = GwStatus()
        st.link_up, st.handshake_age, st.rx, st.tx = self.link_status()
        checks = list(self.plumbing_checks())
        missing = self.tg_mark_missing(self._guard_info)      # без второго nft
        st.tg_missing = list(missing)
        checks.append(GwCheck("маршрут к Telegram", not missing,
                              "" if not missing else
                              f"нет в таблице {len(missing)} диапазонов — "
                              f"агент перевыставит таблицу"))
        checks.append(self.gh_route_check(self._guard_info))
        peer_check = self.peer_nets_check(self._guard_info)
        if peer_check is not None:
            checks.append(peer_check)
        try:
            # Порт sshd — один `ss` на тик: реконсайл, проверки и панель берут
            # этот снимок. Реассерт внутри реконсайла — перечитать таблицу.
            fact = self.ssh_port_fact()
            before = self._ssh_last_reassert
            self.__dict__.setdefault("_ssh_pending", []).extend(
                self.ssh_reconcile(self._guard_info, fact))
            if self._ssh_last_reassert != before:
                from awgbot.infra import gwguard
                try:
                    self._guard_info = gwguard.table_info()
                except gwguard.GwGuardError:
                    pass
            checks += self.ssh_checks(self._guard_info, fact)
            scr = self.ssh_screen(self._guard_info, fact, conf=False)
            st.ssh = {"port": scr["port"], "owner": scr["owner"], "filter": scr["filter"],
                      "allow": len(scr["allow"]), "sshd_down": scr["sshd_down"],
                      "new_plumbing": scr["new_plumbing"]}
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway: ssh status: %s", e)
        ok_link = st.link_up and st.handshake_age is not None
        checks.append(GwCheck("линк", ok_link, "" if ok_link else
                              ("интерфейс лежит" if not st.link_up else "хендшейка не было")))
        # Живьём, без кэша: кэш на 10 минут показывал «модуль: ?» и «ядро без
        # модуля» всё время после обновления модуля — снимок середины операции.
        (st.module_version, st.srcversion), (st.kernels_missing, st.kernels_total), smart = \
            self._static()
        checks.append(GwCheck("ядра", not st.kernels_missing,
                              "" if not st.kernels_missing else
                              "без модуля awg: " + ", ".join(st.kernels_missing)))
        st.lan, lan_checks = self.lan_status()
        checks += lan_checks
        if st.lan:
            # сервисы соседних сетей — внутри блока локальной сети, свои проверки
            svc, svc_checks = self.services_status()
            st.lan["svc"] = svc
            checks += svc_checks
            # свои списки, общие для всех шлюзов — там же
            own, own_checks = self.own_status()
            st.lan["own"] = own
            checks += own_checks
        st.egress_ok, st.egress_ms, st.egress_src = self._egress_verdict(st.rx, st.tx)
        checks.append(GwCheck("выход наружу", st.egress_ok,
                              ("по обратному трафику клиентов" if st.egress_src == "трафик" else
                               f"{st.egress_ms:.0f} мс" if st.egress_ok and st.egress_ms is not None
                               else "" if st.egress_ok else
                               "канал не отвечает — РФ-доступ через шлюз не работает")))
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
        st.smart = smart
        st.uptime_seconds = hostmetrics.read_uptime_seconds()
        st.hostname = socket.gethostname()
        st.server_name = self.server_name()
        st.mark_status = self.gateway_mark_status()
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

    def gw_snapshot(self) -> dict:
        """Снимок состояния для канала.

        Берёт то, что тик уже снял: своего `nft` не зовёт, в сеть не ходит
        (инвариант снимка: без сети). Два чтения сверх этого — конфиг линка и
        `install/awg.lock`, оба локальные и оба по несколько сотен байт.
        """
        from awgbot.domain import gwsnapshot
        st = self.cached_status(24 * 3600)
        missing = self.peer_nets_missing(self._guard_info)
        return gwsnapshot.collect(
            mark_status=self.gateway_mark_status(),
            egress_ok=st.egress_ok if st is not None else None,
            guard_info=self._guard_info,
            peer_nets=None if missing is None else (not missing, missing),
            agent_bot={"username": getattr(self, "bot_username", ""),
                       "name": getattr(self, "bot_name", "")})

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
