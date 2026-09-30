"""monitor.py — гистерезис алертов, тик монитора, сводка, имя ВПС, потребление."""

from __future__ import annotations

import html
import json
import socket
import time
from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.domain.services import Notification
from awgbot.domain.gwchecks import CHECK_GROUPS_SOFT
from awgbot.domain.gateway.base import GwCheck, GwStatus, log


class MonitorMixin:
    """Гистерезис алертов, тик монитора, сводка, имя ВПС, потребление."""
    # ── гистерезис ───────────────────────────────────────────────────────────

    def _armer(self, key: str, value: str):
        """Отложенная отметка «алерт показан» — по факту доставки (domain/alerts)."""
        from awgbot.domain import alerts
        return alerts.armer(self.db, f"gwst_armed_{key}", value)

    def _streak_alert(self, key: str, bad: bool | None, streak: int,
                      on_text: str, off_text: str, loud: bool = True,
                      critical: bool = True) -> list[Notification]:
        """Гистерезис по стрикам — общий с основным ботом (domain/alerts):
        алерт после N плохих замеров подряд, отбой после N хороших, «взведён»
        — по факту доставки."""
        from awgbot.domain import alerts
        return alerts.streak_alert(self.db, (f"gwst_hi_{key}", f"gwst_lo_{key}", f"gwst_armed_{key}"),
                                   bad, streak, on_text, off_text, loud=loud, critical=critical)


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
        if self.__dict__.pop("_masq_stale", False):
            self._reassert_throttled("маскарад клиентов не на интерфейсе маршрута по умолчанию")
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
            "🚨 Линк до сервера AWG мёртв: хендшейка нет дольше допустимого"
            + self._rf_note(". РФ-доступ у клиентов не работает"),
            "✅ Линк до сервера AWG ожил, хендшейк свежий",
            loud=settings.get_bool("app.gateway.link_alert_loud", True))

        # Лежащий домашний канал для РФ-доступа равносилен лежащему линку —
        # тот же короткий стрик, а не общий на пять тиков.
        host = html.escape(socket.gethostname(), quote=False)
        notes += self._streak_alert(
            "egress", st.egress_ok is False,
            settings.get_int("app.gateway.egress_alert_streak", 2),
            "⚠️ Шлюз не выходит наружу: канал не отвечает"
            + self._rf_note(". РФ-доступ через шлюз не работает"),
            f"✅ Шлюз {host} снова выходит наружу")

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
            "⚠️ VPN-транзит: "
            + "; ".join(f"{html.escape(c.name, quote=False)} — {html.escape(c.detail, quote=False)}"
                        for c in lan_broken[:3]),
            f"✅ VPN-транзит ({host}" + (f", <code>{subnet}</code>" if subnet else "")
            + ") снова в порядке", critical=False)

        notes += self._streak_alert(              # не критично: стреляет только на ребуте
            "kernels", bool(st.kernels_missing), streak,
            "⚠️ Ядра без модуля awg: " + ", ".join(html.escape(k, quote=False) for k in st.kernels_missing[:4]) +
            ". Ребут в такое ядро оставит шлюз без туннелей",
            "✅ Все установленные ядра покрыты модулем awg", critical=False)

        under_now = bool(st.throttled and st.throttled.get("now"))
        notes += self._streak_alert(
            "power", under_now, 2,
            "⚠️ Питание шлюза: " + "; ".join((st.throttled or {}).get("now", [])) +
            ". Классика тихой смерти — проверь блок питания",
            "✅ Питание шлюза в норме")

        temp_bad = None if st.temp is None else \
            st.temp >= settings.get_int("app.gateway.temp_alert_c", 75)
        notes += self._streak_alert(
            "temp", temp_bad, streak,
            f"🌡 Процессор {st.temp:.0f} °C — перегрев" if st.temp is not None else "",
            "✅ Температура процессора в норме")

        # алерты хоста — общим тумблером и порогами с основным ботом
        if settings.get_bool("resource_alerts.enabled", True):
            for name, val, label, unit in (("cpu", st.cpu, "CPU", "%"), ("ram", st.ram, "RAM", "%"),
                                           ("disk", st.disk, "Диск", "%")):
                bad = None if val is None else \
                    val >= settings.get_int(f"resource_alerts.thresholds_percent.{name}", 80)
                notes += self._streak_alert(
                    name, bad, streak,
                    f"📈 {label} шлюза: {val:.0f}{unit} — выше порога" if val is not None else "",
                    f"✅ {label} шлюза снова в норме")

        return notes


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
        from awgbot.infra import hostmetrics
        c = self.__dict__.get("_static_cache")
        if c and time.monotonic() - c[0] < self._STATIC_TTL:
            return c[1], c[2], c[3]
        ver, cov, smart = self.versions(), self.kernel_coverage(), hostmetrics.read_smart_health()
        self.__dict__["_static_cache"] = (time.monotonic(), ver, cov, smart)
        return ver, cov, smart

    def status(self) -> GwStatus:
        """Живой снимок: линк, монитор здоровья, железо — сбор без сохранения.
        Снаружи (кнопки «Статус», «Монитор здоровья», тик) ходят через
        snapshot(): он же сохраняет снимок для панели, иначе панель и здоровье
        показывали разные моменты. Редко меняющееся (модуль, ядра, SMART) — из
        кэша, который кнопки сбрасывают через invalidate_static()."""
        from awgbot.infra import hostmetrics
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
            # Порт sshd — один `ss` на тик, и снимается ПОД замком SSH внутри
            # реконсайла: снятый до замка факт при смене порта из чата в тот же
            # момент откатывал бы порт и врал «правили мимо бота». Проверки и
            # панель берут тот же факт. Реассерт внутри реконсайла — перечитать таблицу.
            before = self._ssh_last_reassert
            self.__dict__.pop("_ssh_fact", None)
            self.__dict__.setdefault("_ssh_pending", []).extend(
                self.ssh_reconcile(self._guard_info))
            fact = self.__dict__.pop("_ssh_fact", None) or self.ssh_port_fact()
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
        checks.append(GwCheck("линк", ok_link, f"хендшейк {timeutil.age_short(st.handshake_age)}" if ok_link else
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
