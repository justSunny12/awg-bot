"""
status.py — снимки экранов, онлайн, статус сервера, обновление статуса,
бэкап и вьюхелперы: всё, что экраны читают у сервисов.
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.infra import awg
from awgbot.domain.services.types import Notification


log = logging.getLogger("awgbot.services")


class StatusMixin:
    # ── потребление за месяц: по профилям и по устройствам ───────────────────

    # ── снимки экранов: всё, что экрану нужно, одним вызовом из одного потока ─
    # Панель админа собиралась 12–15 отдельными хопами в поток и ~20 SQL, с
    # дублями (admin_client дважды, видимость маршрутизации дважды).

    def admin_panel_snapshot(self) -> dict:
        st = self.server_status_cached()
        tot = self.db.get_total_month_traffic()
        st = {**st, "traffic_rx": tot["rx"], "traffic_tx": tot["tx"]}
        ac = self.admin_client()
        rt_visible = bool(ac and self.routing_client_visible(ac))
        routing_ok = self.routing_health_for_client(ac) if (ac and rt_visible) else None
        routing_info = self.routing_admin_status() if routing_ok is not None else None
        mig = self.migration_progress() if self.migration_running() else None
        n_dev = self.db.count_devices(ac.id) if ac else 0
        # ряд «Ссылка/QR/Файл» — только когда есть что выдавать: шлюз в
        # «Моих устройствах» есть, а ссылки у него нет
        n_gw = 0
        if n_dev and ac:
            for g in self.db.gateways():
                d = self.db.get_device(g.device_id)
                if d is not None and d.client_id == ac.id:
                    n_gw += 1
        n_issuable = n_dev - n_gw
        rf = self.rf_month_total()
        rf["show"] = self.rf_line_visible()
        return {
            "st": st, "ac": ac, "routing_ok": routing_ok, "routing_info": routing_info, "mig": mig,
            "rf": rf,
            "expiring": len(self.expiring_subscriptions()),
            "unassigned": self.count_unassigned_devices(),
            "has_dev": n_dev > 0,
            "can_issue": n_issuable > 0,
            "rt_visible": rt_visible,
            "rt_on": bool(ac and rt_visible and self.routing_profile_on(ac.id)),
        }

    def online_ref(self) -> datetime.datetime:
        """Момент, на который считать «онлайн»: последний опрос пиров.

        Хендшейки в БД свежее опроса не бывают, а опрос идёт раз в тик. Сравнивать
        их с текущим временем значило бы гасить тех, кто был на связи в момент
        опроса, с каждой минутой после него: счётчик в панели считался на момент
        опроса, а список по ссылке через три минуты показывал вдвое меньше.
        Опрос старше порога (опросчик встал) — данные протухли, берём «сейчас»:
        все оффлайн, и это честно."""
        raw = self.db.get_state("online_polled_at") or ""
        now = timeutil.now()
        thr = settings.get_int("app.online_handshake_seconds", 300)
        if raw.isdigit() and 0 <= now.timestamp() - int(raw) <= thr:
            return datetime.datetime.fromtimestamp(int(raw), tz=timeutil.TZ)
        return now

    def _devices_online(self, devices) -> bool:
        thr = settings.get_int("app.online_handshake_seconds", 300)
        ref = self.online_ref()
        return any(timeutil.handshake_is_online(d.traffic.last_handshake, ref, threshold=thr)
                   for d in devices)

    def client_card_data(self, client_id: int) -> Optional[dict]:
        """Карточка профиля у админа: 8 хопов и тройной list_devices → одно."""
        client = self.db.get_client(client_id)
        if client is None:
            return None
        devices = self.db.list_devices(client_id)
        progress = self.migration_client_progress(client_id) if self.migration_running() else None
        rt_visible = self.routing_client_visible(client)
        return {
            "client": client, "devices": devices,
            "traffic": self.db.get_client_traffic(client_id),
            "online": self._devices_online(devices),
            "progress": progress, "rt_visible": rt_visible,
            "rt_on": self.routing_profile_on(client_id) if rt_visible else False,
        }

    def client_info_data(self, client_id: int) -> Optional[dict]:
        """«Управлять подпиской» у клиента — то же одним вызовом."""
        client = self.db.get_client(client_id)
        if client is None:
            return None
        devices = self.db.list_devices(client_id)
        return {"client": client, "devices": devices,
                "traffic": self.db.get_client_traffic(client_id),
                "online": self._devices_online(devices),
                "routing": self.routing_client_visible(client)}

    def svc_screen_data(self) -> dict:
        state = self.migration_state()
        return {"state": state, "available": self.migration_available(),
                "progress": self.migration_progress() if state else None,
                "orphans": 0 if state else len(self.migration_orphan_twins())}

    def migration_orphan_rows(self) -> list[tuple[str, str]]:
        """[(имя профиля, имя устройства)] — имена одним проходом, не по одному."""
        names = {c.id: c.name for c in self.db.list_clients(include_service=True)}
        return [(names.get(d.client_id, "?"), d.name) for d in self.migration_orphan_twins()]

    # ── онлайн: кто подключён прямо сейчас ───────────────────────────────────

    def online_devices(self) -> list[tuple]:
        """[(устройство, имя профиля)] с живым хендшейком — по ВСЕМ профилям,
        включая админа. Порядок: по имени профиля, внутри — по имени устройства."""
        ref = self.online_ref()
        devs = [d for d in self.db.list_all_devices()
                if timeutil.handshake_is_online(d.traffic.last_handshake, ref)]
        names = {c.id: c.name for c in self.db.list_clients(include_service=True)}
        devs.sort(key=lambda d: (0 if d.is_gateway else 1,
                                 names.get(d.client_id, "").lower(), d.name.lower()))
        return [(d, names.get(d.client_id, "")) for d in devs]

    def online_client_ids(self) -> set[int]:
        """Профили, у которых онлайн хотя бы одно устройство."""
        return {d.client_id for d, _ in self.online_devices()}

    def traffic_by_profile(self) -> list[tuple]:
        """[(client, rx, tx)] за календарный месяц. Админ первым, остальные по
        имени — тот же порядок, что в списке клиентов."""
        out = []
        for c in self.db.list_clients(admin_first_tg=config.ADMIN_ID):
            t = self.db.get_client_traffic(c.id)
            out.append((c, int(t["rx_month"]), int(t["tx_month"])))
        return out

    def traffic_by_device(self, client_id: int) -> list[tuple]:
        """[(device, rx, tx)] за месяц по устройствам профиля — по одной строке
        на устройство (list_devices сам решает, какую из пары показать)."""
        return [(d, int(d.traffic.rx_month), int(d.traffic.tx_month))
                for d in self.db.list_devices(client_id)]

    # ── экран «Сервер» и подготовка переезда ────────────────────────────────

    def _live_listen_port(self) -> int:
        """ListenPort основного интерфейса, как его видит сервер. 0 — не
        прочитали (интерфейс лежит, конфиг недоступен)."""
        try:
            return int(awg.read_server_params(iface=config.AWG_INTERFACE)["listen_port"])
        except Exception as e:                            # noqa: BLE001
            log.debug("server_screen: порт интерфейса не прочитан: %s", e)
            return 0

    def server_screen(self) -> dict:
        """Значения раздела «Сервер». Живые (settings), а не константы старта:
        экран обязан показывать то, что уедет в следующую выданную ссылку."""
        from awgbot.infra import awglock
        g = settings.get
        import subprocess
        kernel = ""
        try:
            cp = subprocess.run(["modinfo", "-F", "version", "amneziawg"],
                                capture_output=True, timeout=10)
            kernel = cp.stdout.decode(errors="replace").strip() if cp.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            kernel = ""
        tag = awglock.built_module_tag() or awglock.module_tag()
        if tag:
            # Строка версии у разных тегов апстрима одинакова, поэтому в UI
            # показываем тег: только он отвечает на вопрос «что собрано».
            kernel = f"{tag} ({kernel})" if kernel else tag
        dns1 = g("app.client_config.dns1", config.DNS1)
        dns2 = g("app.client_config.dns2", config.DNS2)
        return {
            "host": g("app.network.server_host", config.SERVER_HOST),
            "name": g("app.client_config.server_name", config.SERVER_NAME),
            "dns": dns1 if dns1 == dns2 else f"{dns1}, {dns2}",
            "mtu": g("app.client_config.mtu", config.MTU),
            "keepalive": g("app.client_config.keepalive_seconds", config.KEEPALIVE_SECONDS),
            "iface": config.AWG_INTERFACE,
            # Порт берём У ЖИВОГО интерфейса — именно он уезжает в ссылки
            # (configgen читает listen_port оттуда же). Значение из конфига
            # показываем только при расхождении: экран, говорящий одно, пока
            # ссылки несут другое, хуже отсутствующего экрана.
            "port": self._live_listen_port() or g("app.network.server_port", config.SERVER_PORT),
            "port_conf": g("app.network.server_port", config.SERVER_PORT),
            "subnet": g("app.network.subnet_cidr", f"{config.SUBNET_PREFIX}.0/24"),
            "private_dns": self.private_dns_info(),
            "kernel": kernel,
            "generation": awglock.applied_generation(),
            # Почему смена порта/подсети сейчас невозможна — или пусто
            "migration_blocked": self.migration_blocked_reason(),
        }

    def migration_prepare_data(self, want_port: int = 0) -> dict:
        """Что показать на экране подготовки переезда: текущая топология и
        размер когорты. Когорта считается ровно так же, как её заморозит старт."""
        clients, devices, _to_birth = self.migration_start_preview()
        return {"iface": config.AWG_INTERFACE,
                "port": self._live_listen_port() or settings.get("app.network.server_port",
                                                                 config.SERVER_PORT),
                "subnet": settings.get("app.network.subnet_cidr",
                                       f"{config.SUBNET_PREFIX}.0/24"),
                "clients": clients, "devices": devices,
                "want_port": want_port,
                "private_dns": self.private_dns_for_migration(),
                "blocked": self.migration_blocked_reason()}

    # ── Вьюхелперы для отображения ───────────────────────────────────────────

    def count_unassigned_devices(self) -> int:
        service_id = self.db.get_service_client_id()
        return self.db.count_devices(service_id)

    def profile_traffic_limit(self, client_id: int) -> int:
        """Лимит трафика профиля-владельца (байты, 0 = безлимит) — для подсказки
        при задании лимита устройства."""
        c = self.db.get_client(client_id)
        return int(c.traffic_limit) if c else 0

    def client_is_online(self, client_id: int) -> bool:
        """Онлайн ли хоть одно устройство профиля — одним индексным запросом."""
        return self.db.client_has_online_device(
            client_id, settings.get_int("app.online_handshake_seconds", 300),
            ref_ts=int(self.online_ref().timestamp()))

    def device_slots(self, client_id: int) -> tuple[int, int]:
        """(добавлено, лимит) — для подсветки «M из N»."""
        client = self.db.get_client(client_id)
        if client is None:
            return (0, 0)
        return (self.db.count_devices(client_id), client.device_limit)

    def is_only_device(self, device_id: int) -> bool:
        """True, если это единственное устройство своего клиента (удаление =
        потеря доступа к VPN и, возможно, к боту). Для устройств «без профиля»
        (служебный клиент) — всегда False: у них нет tg_id-владельца, который
        «потеряет доступ через этот VPN», предупреждение неприменимо."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return False
        if dev.client_id == self.db.get_service_client_id():
            return False
        return self.db.count_devices(dev.client_id) <= 1

    # ── Бэкап ────────────────────────────────────────────────────────────────

    def make_backup(self) -> list[str]:
        """Одна резервная копия — один архив: БД, все conf/*.yaml, env и
        серверный конфиг awg-интерфейса (единственная копия этого файла вне
        сервера). Задан секрет — файл шифруется целиком (*.tgz.enc), иначе
        уходит открытым (в разделе это видно красным). Разворачивается
        `awg-bot restore <файл>` на любом хосте."""
        extra: list[tuple[str, bytes]] = []
        try:
            conf = awg.read_file(config.CONF_PATH)
            extra.append((f"awg/{config.AWG_INTERFACE}.conf", conf.encode("utf-8")))
        except awg.AwgError:
            pass
        return self.write_backup_archive("main", extra)

    # ── Статус сервера (мониторинг) ──────────────────────────────────────────

    def check_resource_alerts(self, metrics: dict) -> list["Notification"]:
        """Гистерезис загрузки хоста по СТРИКАМ (замерам подряд). Вызывается на
        каждом тике монитора с локальным снимком {cpu, ram, disk} (% или None).

        На каждый ресурс держим два счётчика в state: подряд-превышений и
        подряд-нормы. Значение ≥ порога двигает превышения (+1) и обнуляет норму;
        < порога — наоборот. Алерт «высокая загрузка» — когда превышения достигают
        RESOURCE_ALERT_STREAK и алерт ещё не активен; «отбой» — когда норма
        достигает того же порога и алерт активен. Симметрично вверх/вниз.

        При streak=5 и тике монитора 3 мин реакция ≤ 15 мин. Обычные Notification
        (force_sound=False) → в тихие часы без звука. None-метрика не двигает
        счётчики (нет данных ≠ норма)."""
        if not settings.get_bool("resource_alerts.enabled", True):
            return []
        streak_n = settings.get_int("app.monitoring.alert_streak", 5)
        thresholds = {
            "cpu": (settings.get_int("resource_alerts.thresholds_percent.cpu", 80), "CPU", "🖥"),
            "ram": (settings.get_int("resource_alerts.thresholds_percent.ram", 80), "RAM", "🧠"),
            "disk": (settings.get_int("resource_alerts.thresholds_percent.disk", 80), "Диск", "💽"),
        }
        notes: list[Notification] = []
        # Одна транзакция на тик и счётчики с ПОТОЛКОМ: стрик выше порога
        # ничего не решает (сравнения только «>= порога»), а без потолка
        # счётчик нормы рос бы вечно и каждый тик был бы записью на диск.
        # В спокойном состоянии (норма, стрик набран) тик не пишет ничего.
        with self.db.transaction():
          for key, (threshold, label, icon) in thresholds.items():
            value = metrics.get(key)
            if value is None:
                continue                       # нет данных — счётчики не трогаем
            hi_key = f"res_hi_{key}"           # подряд-превышений
            lo_key = f"res_lo_{key}"           # подряд-нормы
            armed_key = f"res_alert_{key}"     # "1" ⇔ алерт активен
            hi = int(self.db.get_state(hi_key) or 0)
            lo = int(self.db.get_state(lo_key) or 0)
            armed = self.db.get_state(armed_key) == "1"
            if value >= threshold:
                hi, lo = min(hi + 1, streak_n), 0
                if hi >= streak_n and not armed:
                    self.db.set_state(armed_key, "1")
                    notes.append(Notification(
                        config.ADMIN_ID,
                        f"⚠️ {icon} Высокая загрузка: {label} {value:.0f}% "
                        f"(порог {threshold}%, держится ≥{streak_n} замеров).",
                        critical=True))
            else:
                lo, hi = min(lo + 1, streak_n), 0
                if lo >= streak_n and armed:
                    self.db.set_state(armed_key, "0")
                    notes.append(Notification(
                        config.ADMIN_ID,
                        f"✅ {icon} {label} вернулся в норму: {value:.0f}% "
                        f"(ниже порога {threshold}%)."))
            self.db.set_state(hi_key, str(hi))
            self.db.set_state(lo_key, str(lo))
        return notes

    def server_status_cached(self) -> dict:
        """Статусный блок из state — ноль docker exec. Живость awg пишет монитор,
        started_at — детект рестарта, online_count — опросчик трафика, а метрики
        железа (CPU/RAM/диск) монитор снимает ЛОКАЛЬНО (co-located) — hostmetrics. Возраст
        метрик показываем в инфобоксе (обновляет монитор каждый тик, локально)."""
        from awgbot.runtime import hostmetrics
        ok_raw = self.db.get_state("last_server_ok")
        ok = None if ok_raw is None else (ok_raw == "1")
        started = timeutil.parse_docker_time(self.db.get_state("container_started_at") or "")
        uptime = timeutil.fmt_uptime(started) if started else None
        online_raw = self.db.get_state("online_count")
        online = int(online_raw) if online_raw is not None else None
        cpu = ram = disk = age_seconds = None
        snap = hostmetrics.get_host_metrics(self.db)
        if snap:
            cpu, ram, disk = snap.get("cpu"), snap.get("ram"), snap.get("disk")
            age_seconds = snap.get("age_seconds")
        return {"ok": ok, "uptime": uptime, "online_count": online,
                "cpu": cpu, "ram": ram, "disk": disk, "age_seconds": age_seconds}

    def server_ok(self) -> bool:
        """Живая проверка: если awg внутри контейнера ответил — контейнер
        заведомо запущен, отдельный container_running не нужен (1 exec, не 2)."""
        return awg.awg_responding()

    def server_ok_cached(self) -> bool:
        """Статус из state (пишет monitor-задача каждые MONITOR_MINUTES и старт).
        Для приветствий/меню: 0 docker exec, свежесть ≤3 мин — для строки
        «сервер работает» более чем достаточно."""
        cached = self.db.get_state("last_server_ok")
        if cached is not None:
            return cached == "1"
        return self.server_ok()          # state ещё не прогрет (первый старт)

    def refresh_status_now(self) -> list:
        """Внеплановое обновление статусного блока по требованию (кнопка админа):
        живой снимок awg-статуса, опрос пиров (счётчик онлайн и хендшейки — без
        него цифра оставалась бы от прошлого тика) и локальные метрики железа
        прямо в state, минуя ожидание следующего тика монитора. Блокирующий
        (docker exec + /proc) — вызывать через asyncio.to_thread. Меню/инфобокс
        потом читают из state как обычно (0 docker exec на показ). Возвращает
        уведомления опроса (поздравления переезда) — отправить их обязан
        вызывающий."""
        from awgbot.runtime import hostmetrics
        ok = self.server_ok()
        self.db.set_state("last_server_ok", "1" if ok else "0")
        started = awg.service_started_at()
        if started:
            self.db.set_state("container_started_at", started)
        hostmetrics.collect_and_store(self.db)
        return self.poll_traffic() if ok else []

    def restart_service(self) -> None:
        """Перезапуск AmneziaWG по кнопке админа. На хосте это awg-quick
        down/up — метка старта не меняется, блокировки не слетают, и
        reconcile_blocks ниже лишь подтверждает картину. В docker-режиме
        меняется StartedAt контейнера, и detect_and_handle_restart переналагает
        и блокировки, и SSH-фильтр."""
        awg.restart_server()
        self.detect_and_handle_restart()
        self.reconcile_blocks()
