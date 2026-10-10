"""slots.py — слоты шлюзов, переключение трафика, пинг, состояние для экранов."""

from __future__ import annotations

import re
import time
from typing import Optional
from awgbot.core import config
from awgbot.util import timeutil
from awgbot.infra import routing
from awgbot.domain import configgen
from awgbot.domain.services.types import Notification, ServiceError
from .common import log


class SlotsMixin:
    """Слоты шлюзов, переключение трафика, пинг, состояние для экранов."""
    # ── слоты ────────────────────────────────────────────────────────────────
    _RT_ACTIVE_KEY = "routing_active_gateway"

    def active_gateway(self):
        """Слот, который несёт трафик: из состояния; ключа нет или слот убран —
        первый по порядку (предпочтительный, затем номер)."""
        slots = self.db.gateways()
        if not slots:
            return None
        raw = self.db.get_state(self._RT_ACTIVE_KEY)
        if raw:
            for g in slots:
                if str(g.id) == raw:
                    return g
        return slots[0]

    def preferred_gateway(self):
        return next((g for g in self.db.gateways() if g.preferred), None)

    def gateway_first_slot(self):
        """Слот 1 — тот, что достался из прежней схемы с одним шлюзом."""
        slots = self.db.gateways()
        return next((g for g in slots if g.id == 1), slots[0] if slots else None)

    def _gw_slot(self, slot_id: Optional[int]):
        """Слот по номеру (без номера — первый). Слотов нет вовсе — заглушка на
        линке обвязки: бандл без назначенного устройства собирался и до слотов
        (ключи линка есть, устройства ещё нет)."""
        gw = self.db.gateway(slot_id) if slot_id else self.gateway_first_slot()
        if gw is None:
            if slot_id:
                raise ServiceError("такого слота шлюза нет")
            from awgbot.core.models import Gateway
            return Gateway(id=1, device_id=0, link_if=config.ROUTING_GW_INTERFACE or "awglink",
                           link_port=config.ROUTING_LINK_PORTS[0], link_cidr=config.ROUTING_LINK_CIDRS[0],
                           preferred=1)
        return gw

    def _gw_slot_key(self, key: str, slot_id: int) -> str:
        return f"{key}_{int(slot_id)}"

    def gateway_trusted_ips(self) -> set[str]:
        """Адреса из последнего выданного ADMIN_IPS каждого слота: шлюзы доверяют
        адресу, а не устройству (admin4 в таблице). Освободившийся адрес админа
        нельзя отдать следующему устройству любого клиента, пока список у шлюзов
        не сменится — иначе чужой клиент получит доступ к шлюзу и его сети."""
        out: set[str] = set()
        for g in self.db.gateways():
            raw = self.db.get_state(self._gw_slot_key(self._GW_BUNDLE_SSH_KEY, g.id)) or ""
            out.update(x for x in raw.split() if x)
            # и то, что реально стоит в обвязке по снимку канала: файл мог быть
            # выдан и не применён, а канал — доставить список без файла
            snap = self.gwlink_snapshot(g.id) if hasattr(self, "gwlink_snapshot") else {}
            out.update(x for x in str(snap.get("admin_ips") or "").split() if x)
        return out

    def _gw_name(self, gw) -> str:
        dev = self.db.get_device(gw.device_id)
        return dev.name if dev is not None else f"слот {gw.id}"

    def _gw_display(self, gw) -> str:
        """«Pi4 (ЕК-22П10)» — имя и подпись, сырое: экранирует слой текстов."""
        name = self._gw_name(gw)
        return name + (f" ({gw.label})" if gw.label else "")

    def _gw_display_h(self, gw) -> str:
        """То же для текста, который уходит сообщением как HTML прямо отсюда.
        Имя и подпись задаёт человек: `<` или `&` в них без экранирования
        Telegram отвергает, и уведомление не доходит вовсе — ровно тогда,
        когда шлюз лёг."""
        import html
        return html.escape(self._gw_display(gw), quote=False)

    def _gw_reissue_link(self, gw, label: str = "Перевыпусти") -> str:
        """«Перевыпусти» — ссылка, по которой бот сразу отдаёт файл
        конфигурации слота (gwcfg-<слот>); без username — простой текст."""
        slot = int(getattr(gw, "id", 0) or 0)
        return self._link(f"gwcfg-{slot}", label) if slot else label

    def _gw_card_link(self, gw, label: str = "в карточке шлюза") -> str:
        """Ссылка на карточку слота с произвольной подписью."""
        slot = int(getattr(gw, "id", 0) or 0)
        return self._link(f"gw-{slot}", label) if slot else label

    def _gw_link(self, gw) -> str:
        """Имя шлюза в уведомлении — ссылка на карточку слота (gw-<слот>)."""
        slot = int(getattr(gw, "id", 0) or 0)
        if not slot or not self.bot_username:
            return self._gw_display_h(gw)
        return f'<a href="https://t.me/{self.bot_username}?start=gw-{slot}">{self._gw_display_h(gw)}</a>'

    def gateway_next_slot(self) -> tuple[int, str, int, str]:
        """(номер, интерфейс, порт, /30) для нового слота. Отказ при потолке
        или когда конфиг не знает порта/подсети для такого номера."""
        slots = self.db.gateways()
        if len(slots) >= config.ROUTING_GATEWAYS_MAX:
            raise ServiceError(f"слотов шлюзов уже {len(slots)} — потолок routing.gateways_max")
        used_ids = {g.id for g in slots}
        used_if = {g.link_if for g in slots}
        used_port = {g.link_port for g in slots}
        used_cidr = {g.link_cidr for g in slots}
        for n in range(1, config.ROUTING_GATEWAYS_MAX + 1):
            if n in used_ids:
                continue
            iface = (config.ROUTING_GW_INTERFACE or "awglink") if n == 1 else f"awglink{n}"
            if n > len(config.ROUTING_LINK_PORTS) or n > len(config.ROUTING_LINK_CIDRS):
                raise ServiceError(f"для слота {n} нет порта или подсети линка в routing.link_ports / link_cidrs")
            port, cidr = config.ROUTING_LINK_PORTS[n - 1], config.ROUTING_LINK_CIDRS[n - 1]
            if iface in used_if or port in used_port or cidr in used_cidr:
                raise ServiceError(f"слот {n}: интерфейс, порт или подсеть линка уже заняты")
            return n, iface, port, cidr
        raise ServiceError("свободного слота нет")


    # ── переключение трафика ─────────────────────────────────────────────────
    _RT_SWITCHED_KEY = "routing_switched_at"

    def gateway_switch(self, slot_id: int, *, manual: bool):
        """Переложить трафик на слот: один маршрут в таблице фичи, маркировка
        не снимается. Стрики нового активного — его слотовые. manual — рукой:
        интервал автомата не трогается (он защищает от автомата, не от
        человека)."""
        gw = self._gw_slot(slot_id)
        active = self.active_gateway()
        if active is not None and active.id == gw.id:
            return gw
        routing.switch_active(gw.link_if)
        # «лежащий» — недоступен по окну и не набрал трёх хороших подряд (ожил —
        # уже живой, хоть окно и помнит провал); считаем до сброса окна
        up = int(self.db.get_state(f"routing_gw_{gw.id}_up_streak") or 0)
        dead = self._rt_unavailable(gw.id) and up < self._RT_UP_STREAK
        # окно замеров нового активного — с чистого листа: неудачи со времён
        # резерва не должны тут же тянуть трафик обратно
        self._rt_window_reset(gw.id)
        # и память зондов обоих — как при автоматическом переключении: роли
        # поменялись, а база счётчиков и кэш вердикта сняты для прежней. Рост
        # rx, накопленный слотом в резерве, иначе сошёл бы за улику прохождения
        # в первый же такт новой роли.
        self._standby_forget(gw.id)
        if active is not None:
            self._standby_forget(active.id)
        with self.db.transaction():
            self.db.set_state(self._RT_ACTIVE_KEY, str(gw.id))
            if manual:
                # Переключили руками на лежащий шлюз — значит, так надо: автомат
                # его не перекладывает обратно, пока он не оживёт. На живой —
                # удержания нет: упадёт, автомат переключит на живой резерв.
                self.db.set_state(self._RT_HOLD_KEY, str(gw.id) if dead else "")
            else:
                self.db.set_state(self._RT_SWITCHED_KEY, timeutil.to_iso(timeutil.now()))
        log.info("routing: %s переключение на слот %s (%s)",
                 "ручное" if manual else "автоматическое", gw.id, gw.link_if)
        return gw


    # ── пинг до шлюза и его внешний IP: кэш на сутки, лениво ─────────────────
    _GW_PING_KEY = "routing_gw_ping"
    _GW_PING_TTL = 24 * 3600

    def _gw_ping_forget(self, slot_id: int) -> None:
        self.db.set_state(f"{self._GW_PING_KEY}_{int(slot_id)}", "")
        self.__dict__.get("_gw_ping_failed", {}).pop(int(slot_id), None)

    def _gw_cached(self, key: str, slot_id: int) -> Optional[tuple[str, str]]:
        raw = self.db.get_state(f"{key}_{int(slot_id)}") or ""
        if not raw or " " not in raw:
            return None
        val, at = raw.split(" ", 1)
        try:
            age = (timeutil.now() - timeutil.parse_iso(at)).total_seconds()
        except ValueError:
            return None
        return None if age > self._GW_PING_TTL else (val, at)

    def gateway_external_ip(self, slot_id: int) -> Optional[str]:
        """Внешний адрес шлюза — эндпоинт пира линка, как его видит ВПС с
        последнего хендшейка. Один локальный exec, наружу ничего не уходит."""
        gw = self._gw_slot(slot_id)
        try:
            return routing.link_peer_endpoint(gw.link_if)
        except routing.RoutingError as e:
            log.warning("gateway: эндпоинт линка слота %s не прочитан: %s", gw.id, e)
            return None

    def gateway_ping_cached(self, slot_id: int) -> Optional[tuple[int, str]]:
        """(мс, когда) из кэша, если ему меньше суток."""
        c = self._gw_cached(self._GW_PING_KEY, slot_id)
        return (int(c[0]), c[1]) if c else None

    _GW_PING_FAIL_TTL = 300                   # с — отказ пинга помним: карточка не ждёт ping -c 3 на каждом открытии

    def gateway_ping(self, slot_id: int) -> Optional[int]:
        """Пинг с ВПС ДО шлюза: ICMP по линку на адрес шлюза, медиана трёх.
        Успех — в кэш; отказ — кэш снят, None, и отказ помнится
        (_GW_PING_FAIL_TTL): при лежащем шлюзе каждое открытие карточки иначе
        стоило ip addr и ping -c 3 -W 2 — секунды спиннера."""
        gw = self._gw_slot(slot_id)
        ms = routing.ping_peer(gw.link_if)
        if ms is None:
            self.db.set_state(f"{self._GW_PING_KEY}_{gw.id}", "")
            self.__dict__.setdefault("_gw_ping_failed", {})[int(gw.id)] = time.monotonic()
            return None
        self.__dict__.setdefault("_gw_ping_failed", {}).pop(int(gw.id), None)
        self.db.set_state(f"{self._GW_PING_KEY}_{gw.id}",
                          f"{int(ms)} {timeutil.to_iso(timeutil.now())}")
        return int(ms)

    def gateway_ping_failed_recently(self, slot_id: int) -> bool:
        at = self.__dict__.get("_gw_ping_failed", {}).get(int(slot_id))
        return at is not None and time.monotonic() - at < self._GW_PING_FAIL_TTL


    # ── состояние для экранов ────────────────────────────────────────────────
    def gateway_states(self) -> list[dict]:
        """По слоту: gateway, device, active, preferred, link_ok, handshake_age,
        issued_at, down_ticks, display. Порядок — предпочтительный, затем номер.
        Сетевых вызовов нет, кроме возраста хендшейка (один exec на слот)."""
        active = self.active_gateway()
        out = []
        for g in self.db.gateways():
            dev = self.db.get_device(g.device_id)
            age = None
            if config.ROUTING_GW_INTERFACE:
                try:
                    age = routing.link_handshake_age(g.link_if)
                except Exception:                             # noqa: BLE001
                    age = None
            up = int(self.db.get_state(f"routing_gw_{g.id}_up_streak") or 0)
            unavailable = self._rt_unavailable(g.id)
            is_active = active is not None and active.id == g.id
            link_ok = (self.routing_link_ok() if is_active
                       else up >= self._RT_UP_STREAK and not unavailable)
            out.append({
                "gateway": g, "device": dev, "active": is_active, "preferred": bool(g.preferred),
                "link_ok": link_ok, "handshake_age": age, "unavailable": unavailable,
                "down_ticks": self._rt_bad_ticks(g.id), "up_ticks": up,
                "down_since": int(self.db.get_state(f"routing_gw_{g.id}_down_since") or 0),
                "issued_at": self.db.get_state(self._gw_slot_key(self._GW_BUNDLE_ISSUED_KEY, g.id)) or "",
                "display": self._gw_display(g),
                "ping": self.gateway_ping_cached(g.id),
            })
        return out

    def routing_admin_status(self) -> Optional[dict]:
        """Сводка для строки РФ-доступа в шапке админа: работает ли, кто несёт
        трафик, что с резервом. Без единого exec: шапка рисуется из кэша, а
        возраст хендшейка ей не нужен — живость слотов уже посчитал тик.
        None — слотов нет."""
        slots = self.db.gateways()
        if not slots:
            return None
        active = self.active_gateway()

        def _name(g) -> str:
            dev = self.db.get_device(g.device_id)
            name = dev.name if dev is not None else f"слот {g.id}"
            return f"{name} ({g.label})" if g.label else name

        standby = []
        for g in slots:
            if active is not None and g.id == active.id:
                continue
            unavailable = self._rt_unavailable(g.id)
            up = int(self.db.get_state(f"routing_gw_{g.id}_up_streak") or 0)
            alive = up >= self._RT_UP_STREAK and not unavailable
            standby.append({"name": _name(g), "slot": g.id,
                            "state": "alive" if alive else ("dead" if unavailable else "unknown")})
        return {"ok": self.routing_link_ok(),
                "active": _name(active) if active is not None else "",
                "active_slot": active.id if active is not None else 0,
                "standby": standby}

    def gateway_screen_state(self, slot_id: int, *, lazy_ping: bool = True) -> dict:
        """Состояние слота для карточек: к строке gateway_states — пинг (лениво: пустой
        кэш заполняется замером при первом открытии экрана), подпись активного
        и состояния всех слотов для соседних строк."""
        states = self.gateway_states()
        st = next((x for x in states if x["gateway"].id == int(slot_id)), None)
        if st is None:
            raise ServiceError("такого слота шлюза нет")
        active = next((x for x in states if x.get("active")), None)
        st["active_display"] = active["display"] if active else ""
        st["states"] = states
        cached = st.get("ping")
        if cached is not None:
            st["ping_ms"] = cached[0]
        elif lazy_ping and not self.gateway_ping_failed_recently(slot_id):
            st["ping_ms"] = self.gateway_ping(slot_id)
        else:
            st["ping_ms"] = None
        st["ext_ip"] = self.gateway_external_ip(slot_id)
        # функция B: пускают ли сюда из-за других шлюзов
        st["peer_nets_enabled"] = self.peer_nets_enabled()
        st["peer_nets"] = self.gateway_peer_nets(int(slot_id))
        # канал линка: только из state, без запросов
        st["channel"] = self.gwlink_card(st["gateway"], st.get("handshake_age"))
        # бот шлюза — ссылкой в чат с ним: username и имя из кэша getMe
        st["agent_bot"] = self.gw_bot_identity(int(slot_id))
        # сервисы соседних сетей — числами, без имён
        st["services"] = self.gwlink_services_card(st["gateway"])
        st["own_lists"] = self.gwlink_own_card(st["gateway"])
        return st

    def gateway_uplink_conf(self, dev) -> str:
        """Конфиг аплинка шлюза для бандла: обычный клиентский .conf устройства
        в форме для машины-шлюза (без DNS, Table = off)."""
        cfg = self.generate_config(dev.id, for_bundle=True)
        return configgen.gateway_uplink_conf(cfg["conf"])

    _GW_BUNDLE_SSH_KEY = "gw_bundle_ssh_allow"   # адреса админа, вшитые в последний файл слота

    def _gw_ssh_allow(self) -> list[str]:
        return sorted(set(self.db.admin_device_addresses(config.ADMIN_ID)))

    # ── расхождение конфигурации по снимку канала ────────────────────────────
    _GW_DRIFT_NOTIFIED_KEY = "gw_drift_notified"
    _GW_DRIFT_MAX_LINES = 20                  # строк на группу устройств в уведомлении; дальше «…и ещё N»

    def _gw_drift_pending(self, g, items: list) -> list:
        """Что из расхождения по снимку канал сам не закроет.

        Канал обязателен, и о доставке решает он: ключи обвязки (SETTINGS_KEYS)
        уходят слоту при каждом подключении. Поэтому пока шлюз не на связи —
        молчим, сколько бы он ни молчал (расхождение всё это время видно в
        карточке), а на связи ждём итога доставки. Остаются два случая: шлюз
        ответил отказом на текущий набор (ответ несёт отпечаток набора — иначе
        старый отказ сошёл бы за отказ на новое) и то, что канал не везёт
        вовсе — подсети других шлюзов (BUNDLE_ONLY_KEYS: они и в конфиге линка)."""
        from awgbot.util import gwlink
        if not self.gwlink_session(g.id):
            return []
        if "cfg1" in self.gwlink_agent_caps(g.id):
            # агент принимает документ: подсети соседей тоже едут каналом; остаётся
            # только то, что шлюз отверг в ответе на текущий документ
            ack = self.gwcfg_ack(g.id)
            if not ack or ack.get("hash") != self.gwcfg_doc(g)["hash"] or not ack.get("failed"):
                return []
            failed = ack["failed"]
            out = [it for it in items if "env" in failed or "*" in failed]
            if "link" in failed:
                out.append(("link", "конфиг линка", "", ""))
            return out
        ack = self.gwlink_ack(g.id)
        refused = (bool(ack) and not ack.get("ok")
                   and ack.get("hash") == gwlink.settings_hash(self.gwlink_settings_want(g)))
        return [it for it in items
                if it[0] in gwlink.BUNDLE_ONLY_KEYS or (refused and it[0] in gwlink.SETTINGS_KEYS)]

    def _gw_drift_note(self, g) -> Optional[Notification]:
        """Одно уведомление на набор расхождений, которые канал сам не закроет;
        сошлось после уведомления — один тихий отбой. Снимка нет — молчим:
        судить не по чему."""
        snap = self.gwlink_snapshot(g.id)
        if not isinstance(snap.get("bundle"), dict):
            return None
        items = self.gwlink_config_drift_items(g)
        key = self._gw_slot_key(self._GW_DRIFT_NOTIFIED_KEY, g.id)
        told = self.db.get_state(key) or ""
        if not items:
            if told:
                self.db.set_state(key, "")
                return Notification(config.ADMIN_ID, f"✅ {self._gw_link(g)}: конфигурация актуализирована")
            return None
        pending = self._gw_drift_pending(g, items)
        if not pending:
            return None
        # подпись — по полным значениям обеих сторон и итогу отказа: settings_hash
        # знает только ключи канала (смена подсетей соседей не дала бы нового
        # уведомления), а строки расхождения обрезаны и слились бы у длинных списков
        import hashlib
        from awgbot.util import gwlink
        env, got = self.gwlink_issued_env(g), snap["bundle"]
        src = [f"{k}={env.get(k, '')}|{' '.join(str(got.get(gwlink.snap_field(k), '')).split())}"
               for k, _h, _m, _t in pending] + [str(self.gwlink_ack(g.id).get("hash") or "")]
        sig = hashlib.sha256("\n".join(src).encode()).hexdigest()
        if told == sig:
            return None
        self.db.set_state(key, sig)
        # кнопка «📤 Конфигурация» — файл довезёт и то, что канал не везёт
        return Notification(config.ADMIN_ID, self._gw_drift_text(g, pending), action=("gwcfg", int(g.id)))

    def _gw_drift_text(self, g, pending: list) -> str:
        """Текст уведомления: что разошлось — именами; отказ шлюза — второй
        строкой; устройства админа — группами по именам, прочее — «у сервера …,
        на шлюзе …». Без хвоста «перевыпусти»: канал обязателен."""
        import html
        from awgbot.util import gwlink
        from awgbot.domain.services.gwchannel import drift_lines
        names = ", ".join(html.escape(human, quote=False) for _k, human, _m, _t in pending)
        lines = [f"🛰 {self._gw_link(g)}: конфигурация неактуальна — {names}"]
        if "cfg1" in self.gwlink_agent_caps(g.id):
            failed = self.gwcfg_ack(g.id).get("failed") or {}
            why = "; ".join(failed.values())
        else:
            ack = self.gwlink_ack(g.id)
            why = (ack.get("error") or "ошибка") if ack and not ack.get("ok") and any(
                k in gwlink.SETTINGS_KEYS for k, _h, _m, _t in pending) else ""
        if why:
            lines.append(f"Шлюз не применил: {html.escape(why, quote=False)}")
        for key, human, mine, theirs in pending:
            if key == "ADMIN_IPS":
                lines += self._gw_admin_drift_lines(g)
            elif key == "link":
                continue                                  # конфиг линка назван в заголовке, значений у него нет
            else:
                lines.append(drift_lines([(key, human, mine, theirs)], html=True)[0])
        return "\n".join(lines)

    def _gw_admin_drift_lines(self, g) -> list[str]:
        """Разница по устройствам админа — строка на устройство: «у сервера, нет на
        шлюзе» — имена из devices по адресу; «на шлюзе, нет у сервера» — либо
        живое устройство другого профиля (адрес передан), либо удалённое — имя из
        истории, нет имени — только адрес. Адреса — из самого снимка, не из
        обрезанной строки расхождения; группа длиннее предела — «…и ещё N»."""
        import html
        from awgbot.util import gwlink
        snap = self.gwlink_snapshot(g.id)
        got = snap.get("bundle") if isinstance(snap.get("bundle"), dict) else {}
        want = set(self.gwlink_issued_env(g)["ADMIN_IPS"].split())
        have = {t for t in str(got.get(gwlink.snap_field("ADMIN_IPS"), "")).split()[:64] if gwlink._ipv4(t.split("/")[0])}

        def ip_key(a: str) -> tuple:
            import ipaddress
            try:
                return (0, int(ipaddress.ip_address(a.split("/")[0])))
            except ValueError:
                return (1, 0)

        def line(addr: str, name: str) -> str:
            code = f"<code>{html.escape(addr, quote=False)}</code>"
            return f" · {html.escape(name, quote=False)} ({code})" if name else f" · {code}"

        def group(addrs: list[str], names: dict, one: str, many: str) -> list[str]:
            if not addrs:
                return []
            rows = [one if len(addrs) == 1 else many]
            rows += [line(a, names.get(a, "")) for a in addrs[:self._GW_DRIFT_MAX_LINES]]
            if len(addrs) > self._GW_DRIFT_MAX_LINES:
                rows.append(f" · …и ещё {len(addrs) - self._GW_DRIFT_MAX_LINES}")
            return rows

        missing = sorted(want - have, key=ip_key)
        out = group(missing, self.db.device_names_by_address(missing),
                    "На шлюзе не помечено админским устройство:", "На шлюзе не помечены админскими устройства:")
        extra = sorted(have - want, key=ip_key)
        alive = self.db.device_names_by_address(extra)      # адрес живёт у устройства другого профиля
        out += group([a for a in extra if a in alive], alive,
                     "На шлюзе помечено админским устройство другого профиля:",
                     "На шлюзе помечены админскими устройства других профилей:")
        gone = [a for a in extra if a not in alive]
        out += group(gone, self.db.archived_device_names_by_address(gone),
                     "На шлюзе помечено админским удалённое устройство:",
                     "На шлюзе помечены админскими удалённые устройства:")
        return out

    def gw_bundle_drift_notes(self) -> list[Notification]:
        """Уведомления о расхождении конфигурации шлюзов — только по снимку канала:
        что реально стоит на шлюзе против того, что ВПС выдал бы сейчас. Памяти о
        выдаче больше нет: канал обязателен, и настройки обвязки он довозит сам."""
        if not config.ROUTING_ENABLED:
            return []
        notes = []
        for g in self.db.gateways():
            note = self._gw_drift_note(g)
            if note is not None:
                notes.append(note)
        return notes

    # Маркер контракта как ОТДЕЛЬНАЯ СТРОКА. Тот же текст встречается в бандле и
    # внутри sed-выражения, которым он вырезает скрипт обвязки; вставка туда
    # ломала sed, и на шлюз ложился пустой скрипт (наступили: 09.09.2026).
    _MAIL_MARK_LINE = re.compile(rb"^#__GW_SETUP_BELOW__$", re.M)
