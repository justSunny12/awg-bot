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
from awgbot.domain import gwchecks as gc
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
        # шлюза это три fsync каждые три минуты; стал один.
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
        # перепроверка — exec'ы nft и ip: только когда реассерт вообще не под троттлингом
        if self.__dict__.pop("_router_masq_stale", False) and self._reassert_due() and self._router_masq_stale_now():
            self._reassert_throttled("маскарад подсетей других шлюзов к роутеру разошёлся с маршрутом по умолчанию")
        try:
            self.keep_fw_finish()                       # файервол с сервера ждал перезапуска обвязки
        except Exception as e:                          # noqa: BLE001
            log.warning("gateway: keep_fw_finish: %s", e)
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
                "<code>systemd-networkd</code> — установщик шлюза запрещает ему трогать "
                "чужие правила; повторится — «🚑 Восстановить»",
                critical=False))

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

        # Лежащий канал шлюза для РФ-доступа равносилен лежащему линку —
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
        # локальной сети или упавший резолвер — не «РФ-доступ у всех лёг»
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
        """Месячный итог трафика линка: накопитель дельт — `util/monthacc`
        (там же — почему счётчик меньше прошлого значит «обнулился»)."""
        from awgbot.util import monthacc
        acc = monthacc.account(self.db.get_state_json(self._TRAFFIC_KEY, {}), rx, tx,
                               timeutil.now().strftime("%Y-%m"))
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

    @staticmethod
    def module_label(modinfo_version: str) -> str:
        """Что назвать модулем на экранах: ТЕГ собранного модуля, как на экране
        сервера ВПС — version.h апстрим бампает не на каждый выпуск
        (0827…0906 несут «3.1.20260812»); тега нет — строка modinfo.
        srcversion рядом остаётся: он различает сборки."""
        from awgbot.infra import awglock
        return awglock.built_module_tag() or awglock.module_tag() or modinfo_version

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
        checks.append(GwCheck(gc.CHECK_TG_ROUTE, not missing,
                              "" if not missing else
                              f"нет в таблице {len(missing)} диапазонов; {gc.SELF_HEAL_TAIL}"))
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
        checks.append(GwCheck(gc.CHECK_LINK, ok_link, f"хендшейк {timeutil.age_short(st.handshake_age)}" if ok_link else
                              ("интерфейс лежит" if not st.link_up else "хендшейка не было")))
        try:
            st.rsv = self.reserve_panel_view() if hasattr(self, "reserve_panel_view") else {}
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway: резерв в снимке: %s", e)
            st.rsv = {}
        if st.rsv:
            # Т-РА2: «резервный линк — «Beta» на связи» / «не отвечает с 13:58»
            name = f"«{st.rsv.get('name') or 'резерв'}»"
            down = st.rsv.get("seen") in ("down", "absent")
            since = str(st.rsv.get("since") or "")
            checks.append(GwCheck(gc.CHECK_RSV_LINK, not down,
                                  f"{name} на связи" if not down else f"{name} не отвечает" + (f" с {since[11:16]}" if len(since) >= 16 else ""),
                                  group="rsv"))
        # Живьём, без кэша: кэш на 10 минут показывал «модуль: ?» и «ядро без
        # модуля» всё время после обновления модуля — снимок середины операции.
        (st.module_version, st.srcversion), (st.kernels_missing, st.kernels_total), smart = \
            self._static()
        st.module_version = self.module_label(st.module_version)
        checks.append(GwCheck(gc.CHECK_MODULE, not st.kernels_missing,
                              f"собран для всех ядер ({st.kernels_total})" if not st.kernels_missing else
                              f"нет для {len(st.kernels_missing)} из {st.kernels_total} ядер "
                              f"({', '.join(st.kernels_missing)}): перезагрузка в такое ядро оставит шлюз без туннелей"))
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
        checks.append(GwCheck(gc.CHECK_EGRESS, st.egress_ok,
                              ("по обратному трафику клиентов" if st.egress_src == "трафик" else
                               f"{st.egress_ms:.0f} мс через провайдера" if st.egress_ok and st.egress_ms is not None
                               else "" if st.egress_ok else
                               "провайдер не отвечает — РФ-доступ через шлюз не работает")))
        st.checks = checks
        st.temp = hostmetrics.read_soc_temp()
        st.throttled = hostmetrics.read_pi_throttled()
        st.cpu = hostmetrics.read_cpu_percent()
        ram = hostmetrics.read_ram()
        if ram is not None:
            st.ram, st.ram_free_mb = ram
        ram_mb = hostmetrics.read_ram_mb()
        if ram_mb is not None:
            st.ram_used_mb, st.ram_total_mb = ram_mb
        disk = hostmetrics.read_disk()
        if disk is not None:
            st.disk, st.disk_free_gb = disk
        st.disks = [[d["used_gb"], d["total_gb"], d["mount"]] for d in hostmetrics.read_disks()]
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
        from awgbot.infra import gwguard
        st = self.cached_status(24 * 3600)
        missing = self.peer_nets_missing(self._guard_info)
        snap = gwsnapshot.collect(
            mark_status=self.gateway_mark_status(),
            egress_ok=st.egress_ok if st is not None else None,
            peer_nets=None if missing is None else (not missing, missing),
            agent_bot={"username": getattr(self, "bot_username", ""),
                       "name": getattr(self, "bot_name", "")},
            cfg=self.cfg_fingerprints())
        # адрес шлюза в локальной сети — его знает только агент (LAN_ADDR из
        # статуса обвязки): сервер подставляет его в настройку роутера; транзит
        # выключен — пусто. Ключ верхнего уровня снимка, сервер разбирает его сам.
        snap["lan_addr"] = (gwguard.script_status().get("LAN_ADDR", "") or "") if gwguard.lan_mode() else ""
        # резерв глазами шлюза: связь по awgrsv, доля, пересылка — только из локального
        try:
            snap["rsv"] = self.reserve_snapshot() if hasattr(self, "reserve_snapshot") else None
        except Exception:                                     # noqa: BLE001
            snap["rsv"] = None
        return snap

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
