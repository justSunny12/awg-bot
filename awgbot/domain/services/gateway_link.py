"""
gateway_link.py — шлюзы условной маршрутизации (docs/gateway-failover.md):
слоты, назначение и замена машины, бандл и токен агента по слоту,
переключение трафика, пинг со шлюза, предпочтительный слот.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.infra import routing
from awgbot.domain import configgen
from awgbot.domain.services.types import Notification, ServiceError


log = logging.getLogger("awgbot.services")


class GatewayLinkMixin:
    # ── скрипт линка ─────────────────────────────────────────────────────────
    def _link_script(self) -> str:
        return str(config.BASE_DIR / "install" / "routing-link-setup.sh")

    def _run_link_script(self, mode: str, env: dict | None = None) -> None:
        import subprocess
        proc = subprocess.run(["sh", self._link_script(), mode], capture_output=True,
                              timeout=120, env={**os.environ, **(env or {})})
        if proc.returncode != 0:
            raise ServiceError(f"скрипт линка ({mode}) не отработал: "
                               + proc.stderr.decode(errors="replace").strip()[-200:])

    _LEGACY_LINK_UNIT = "/etc/systemd/system/awg-link.service"

    def gateway_units_migrate(self) -> bool:
        """Юнит первого линка — на шаблон awg-link@ (docs/gateway-failover.md
        §13.3). Сам по себе переезд случился бы на первом ребуте ВПС (реассерт
        зовёт юнит); ждать его незачем — зовём --reassert явно один раз.
        Возвращает, был ли переезд."""
        if not config.ROUTING_GW_INTERFACE or not os.path.exists(self._LEGACY_LINK_UNIT):
            return False
        first = self.gateway_first_slot()
        env = self._slot_env(first) if first is not None else {"LINK_IF": config.ROUTING_GW_INTERFACE}
        self._run_link_script("--reassert", env)
        return True

    def _gw_firewall_refresh(self) -> None:
        """Порт нового линка (и снятие старого) — в файервол хоста сразу, а не
        при следующей плановой перерисовке: иначе второй шлюз не достучится до
        ВПС до неё."""
        try:
            from awgbot.infra import nftguard
            if nftguard.enabled():
                self._firewall_apply(rollback=False)
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway: файервол хоста не перерисован: %s", e)

    @staticmethod
    def _slot_env(gw) -> dict:
        """Окружение скрипта линка для слота: имя интерфейса, порт, /30."""
        return {"LINK_IF": gw.link_if, "LINK_PORT": str(gw.link_port), "LINK_CIDR": gw.link_cidr}

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

    def _gw_display(self, gw) -> str:
        """«Имя» (подпись) — для текстов и уведомлений."""
        dev = self.db.get_device(gw.device_id)
        name = dev.name if dev is not None else f"слот {gw.id}"
        return f"«{name}»" + (f" ({gw.label})" if gw.label else "")

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

    # ── бандл шлюза: скрипт линка и сборка архива ────────────────────────────
    _GW_BUNDLE_ISSUED_KEY = "gw_bundle_issued_at"

    def _link_privkey(self, gw=None) -> str:
        from awgbot.util import bundlecrypt
        link_if = gw.link_if if gw is not None else (config.ROUTING_GW_INTERFACE or "awglink")
        try:
            with open(f"/root/gw-{link_if}.conf", encoding="utf-8") as f:
                return bundlecrypt.read_privkey(f.read())
        except (OSError, ValueError) as e:
            raise ServiceError(f"ключ линка не прочитан: {e}")

    def _gw_bundle_env(self, gw) -> tuple[dict, list[str]]:
        """Окружение сборки бандла слота: устройства админа, ключ и конфиг
        аплинка устройства слота (в окне переезда — двойника, старый ключ
        отдельно), параметры линка слота."""
        admin_ips = self._gw_ssh_allow()
        env = {"ADMIN_IPS": " ".join(admin_ips), **self._slot_env(gw)}
        dev = self.db.get_device(gw.device_id) if gw.device_id else None
        if dev is not None:
            import base64
            twin = self.db.twin_of_device(dev.id)
            target = twin or dev
            env["GATEWAY_PUBKEY"] = target.public_key
            env["GATEWAY_PREV_PUBKEY"] = dev.public_key if twin else ""
            try:
                env["UPLINK_B64"] = base64.b64encode(self.gateway_uplink_conf(target).encode()).decode()
            except ServiceError as e:
                log.warning("bundle: конфиг аплинка шлюза не собран: %s", e)
        return env, admin_ips

    @staticmethod
    def _bundle_path(gw) -> str:
        return "/root/awg-gw-bundle.sh" if gw.link_if == "awglink" else f"/root/awg-gw-bundle-{gw.link_if}.sh"

    @staticmethod
    def bundle_name(gw) -> str:
        """Имя файла первого применения для слота — как его кладёт скрипт."""
        return "awg-gw-bundle.sh" if gw.link_if == "awglink" else f"awg-gw-bundle-{gw.link_if}.sh"

    def _gw_bundle_build(self, gw) -> tuple[bytes, str]:
        """Собрать бандл слота скриптом линка (ключи не меняются) и дополнить
        почтой, фразой бэкапов, токеном агента. Возвращает (открытый текст,
        приватный ключ линка слота)."""
        from awgbot.util import bundlecrypt
        env, admin_ips = self._gw_bundle_env(gw)
        self._run_link_script("--bundle", env)
        with open(f"/root/gw-{gw.link_if}.conf", encoding="utf-8") as f:
            priv = bundlecrypt.read_privkey(f.read())
        with open(self._bundle_path(gw), "rb") as f:
            plain = f.read()
        plain = self._bundle_with_mail(plain)
        plain = self._bundle_with_agent(plain, gw.id)
        self.db.set_state(self._gw_slot_key(self._GW_BUNDLE_SSH_KEY, gw.id), " ".join(admin_ips))
        self.db.set_state(self._gw_slot_key(self._GW_BUNDLE_SSH_NOTIFIED_KEY, gw.id), "")
        self.db.set_state(self._gw_slot_key(self._GW_BUNDLE_ISSUED_KEY, gw.id),
                          timeutil.to_iso(timeutil.now()))
        return plain, priv

    def gw_bundle_encrypted(self, slot_id: Optional[int] = None) -> tuple[bytes, str]:
        """Бандл для доставки чатом: шифрован ключом линка СВОЕГО слота, он есть
        только у уже настроенной машины этого слота. Открытый бандл на диске
        ВПС остаётся под 600."""
        from awgbot.util import bundlecrypt
        gw = self._gw_slot(slot_id)
        plain, priv = self._gw_bundle_build(gw)
        name = "awg-gw-bundle.enc" if gw.link_if == "awglink" else f"awg-gw-bundle-{gw.link_if}.enc"
        return bundlecrypt.encrypt(plain, priv), name

    def gw_bundle_plain(self, slot_id: Optional[int] = None) -> tuple[bytes, str]:
        """Открытый бандл — для ПЕРВОГО применения на машине, у которой ключа
        линка ещё нет (новая машина или новые ключи). Внутри приватные ключи:
        тот же уровень доверия, что у ссылок vpn:// с ключами устройств."""
        gw = self._gw_slot(slot_id)
        plain, _ = self._gw_bundle_build(gw)
        return plain, self.bundle_name(gw)

    # ── пометка устройства: запасной путь через пересланный токен ────────────
    _GW_NONCES_KEY = "gw_claim_nonces"

    def _gw_nonce_seen(self, nonce: str) -> bool:
        seen = (self.db.get_state(self._GW_NONCES_KEY) or "").split()
        if nonce in seen:
            return True
        self.db.set_state(self._GW_NONCES_KEY, " ".join((seen + [nonce])[-50:]))
        return False

    def gateway_claim(self, text: str) -> dict:
        """Пересланное от агента сообщение с токеном `claim`. Подпись — ключом
        линка одного из слотов (перебираем все: по ключу и находится слот),
        устройство — по ключу аплинка. Возвращает {'status': 'marked'|'already',
        'device', 'gateway'}. Слот занят другим устройством — ServiceError:
        менять машину — через карточку шлюза."""
        from awgbot.util import gwsign
        slots = self.db.gateways()
        data = None
        slot = None
        last_err: Exception | None = None
        for gw in slots or [None]:
            try:
                data = gwsign.verify(self._link_privkey(gw), text)
                slot = gw
                break
            except (ValueError, ServiceError) as e:
                last_err = e
        if data is None:
            raise ValueError(str(last_err) if last_err else "ключ линка не прочитан")
        if self._gw_nonce_seen(data["nonce"]):
            raise ServiceError("это сообщение уже принимали — пусть шлюз выдаст новое")
        dev = self.db.get_device_by_pubkey(data["pub"])
        if dev is None:
            raise ServiceError("устройства с таким ключом нет: аплинк шлюза должен быть "
                               "устройством админа, выпущенным этим ботом")
        admin = self.admin_client()
        if admin is None or dev.client_id != admin.id:
            raise ServiceError("шлюзом может быть только устройство профиля админа")
        mine = self.db.gateway_by_device(dev.id)
        if mine is not None:
            return {"status": "already", "device": dev, "gateway": mine}
        if slot is not None and slot.device_id != dev.id:
            raise ServiceError(f"в слоте этого линка уже назначен {self._gw_display(slot)}. "
                               "Заменить машину можно в карточке шлюза (🔁 Заменить машину)")
        if slot is None:
            # слотов нет вовсе (линк поднят обвязкой, шлюз ещё не назначали):
            # заводим первый слот на этом устройстве, ключи линка уже общие
            n, iface, port, cidr = self.gateway_next_slot()
            slot = self.db.gateway_add(dev.id, iface, port, cidr, slot_id=n)
        return {"status": "marked", "device": self.db.get_device(dev.id), "gateway": slot}

    def gateway_candidates(self) -> list:
        """Устройства админа, выпущенные ботом и не занятые слотами."""
        admin = self.admin_client()
        if admin is None:
            return []
        return [d for d in self.db.list_devices(admin.id) if d.private_key and not d.is_gateway]

    _GW_NEW_NAME = "Шлюз"

    def gateway_setup(self, device_id: Optional[int] = None, *, rekey: bool = False,
                      slot_id: Optional[int] = None) -> dict:
        """Назначить машину в слот. slot_id None — новый слот (первый — на
        линке обвязки; следующие поднимают свой линк); заданный — замена машины
        в нём. device_id — существующее устройство админа; None — создать
        новое устройство «Шлюз»/«Шлюз N» в профиле админа (новая машина).
        rekey — новые ключи линка слота: прежняя машина теряет линк по
        построению, а первый бандл для новой едет открытым.
        Возвращает {'gateway', 'device', 'previous', 'created', 'rekeyed'}."""
        admin = self.admin_client()
        if admin is None:
            raise ServiceError("профиль админа ещё не создан")
        gw = self.db.gateway(slot_id) if slot_id else None
        if slot_id and gw is None:
            raise ServiceError("такого слота шлюза нет")
        new_slot = gw is None
        if new_slot:
            n, iface, port, cidr = self.gateway_next_slot()
        created = False
        if device_id is None:
            # Лимит шлюзу не делают исключением: профиль админа безлимитный по
            # построению, а счётчик, который врёт на одну строку, хуже лимита.
            name = self._GW_NEW_NAME if (new_slot and n == 1) or (gw is not None and gw.id == 1) \
                else f"{self._GW_NEW_NAME} {n if new_slot else gw.id}"
            dc = self.add_device(admin.id, name)
            device_id = dc.device_id
            created = True
            rekey = True                       # новая машина без ключа линка
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        if dev.client_id != admin.id:
            raise ServiceError("шлюзом может быть только устройство профиля админа")
        if not dev.private_key:
            raise ServiceError("это устройство создавал не бот — его конфиг в бандл не собрать")
        other = self.db.gateway_by_device(dev.id)
        if other is not None and (gw is None or other.id != gw.id):
            raise ServiceError(f"это устройство уже шлюз в слоте {other.id}")
        prev = None
        if new_slot:
            if n > 1:
                # второй и дальше: свой линк, свои ключи — первый бандл только руками
                self._run_link_script("--apply", {"LINK_IF": iface, "LINK_PORT": str(port),
                                                  "LINK_CIDR": cidr})
                self._gw_firewall_refresh()
            else:
                # первый слот: линк обвязки, но ключи — новые: назначение всегда
                # полным путём, устройство линка не знает
                self._run_link_script("--rekey", {"LINK_IF": iface, "LINK_PORT": str(port),
                                                  "LINK_CIDR": cidr})
            rekey = True
            gw = self.db.gateway_add(dev.id, iface, port, cidr, slot_id=n)
        else:
            if gw.device_id != dev.id:
                prev = self.db.get_device(gw.device_id)
                self.db.gateway_update(gw.id, device_id=dev.id)
                # Замена машины — всегда полный путь: новые ключи линка, файл
                # первого применения руками. Устройство могло никогда не быть
                # шлюзом, и линка у него нет по построению.
                rekey = True
            if rekey:
                self._run_link_script("--rekey", self._slot_env(gw))
        gw = self.db.gateway(gw.id)
        if rekey or new_slot:
            routing.invalidate_self_check()
            try:
                self._ensure_gateway_policy()
            except routing.RoutingError as e:
                log.warning("gateway_setup: обвязка слота не доведена: %s", e)
        return {"gateway": gw, "device": self.db.get_device(dev.id), "previous": prev,
                "created": created, "rekeyed": rekey}

    def gateway_remove(self, slot_id: Optional[int] = None) -> Optional[object]:
        """Убрать слот: активный при живом другом слоте — трафик на него;
        последний — условную маршрутизацию выключить. Линк слота: первый —
        смена ключей (обвязка остаётся), прочие — снимается целиком.
        Возвращает бывшее устройство слота (None — слота не было)."""
        gw = self.db.gateway(slot_id) if slot_id else self.gateway_first_slot()
        if gw is None:
            return None
        prev = self.db.get_device(gw.device_id)
        others = [g for g in self.db.gateways() if g.id != gw.id]
        active = self.active_gateway()
        # трафик — на другой слот ДО удаления строки: иначе «активный» уже
        # вычислится как первый оставшийся, и маршрут в ядре не переложится
        if others and active is not None and active.id == gw.id:
            self.gateway_switch(others[0].id, manual=True)
        self.db.gateway_delete(gw.id)
        for key in (self._GW_BUNDLE_ISSUED_KEY, self._GW_BUNDLE_SSH_KEY, self._GW_BUNDLE_SSH_NOTIFIED_KEY):
            self.db.set_state(self._gw_slot_key(key, gw.id), "")
        if (self.db.get_state(self._RT_HOLD_KEY) or "") == str(gw.id):
            self.db.set_state(self._RT_HOLD_KEY, "")
        self._gw_ping_forget(gw.id)
        self._standby_forget(gw.id)
        if not others:
            try:
                settings.set_value("app.routing.enabled", False)
            except Exception as e:                            # noqa: BLE001
                log.warning("gateway_remove: маршрутизация не выключена: %s", e)
        try:
            if gw.id == 1:
                self._run_link_script("--rekey", self._slot_env(gw))
            else:
                self._run_link_script("--rollback", self._slot_env(gw))
                routing.drop_slot_policy(gw.id, gw.link_if)
                self._gw_firewall_refresh()
            routing.invalidate_self_check()
        except (ServiceError, routing.RoutingError) as e:
            log.warning("gateway_remove: линк слота %s не снят: %s", gw.id, e)
        try:
            self.reconcile_routing()
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway_remove: реконсиляция: %s", e)
        return prev

    def gateway_set_preferred(self, slot_id: Optional[int]) -> None:
        if slot_id is not None and self.db.gateway(slot_id) is None:
            raise ServiceError("такого слота шлюза нет")
        self.db.gateway_set_preferred(slot_id)

    def gateway_set_label(self, slot_id: int, label: str) -> None:
        label = " ".join(str(label).split())[:20].strip()
        self._gw_slot(slot_id)
        self.db.gateway_update(slot_id, label=label)

    def gateway_set_home_subnets(self, slot_id: int, raw: str) -> dict:
        """Разбор пачки подсетей: IPv4, не клиентская, не подсеть линка.
        Возвращает {'kept': [...], 'rejected': [(строка, причина)], 'conflict':
        слот, у которого та же подсеть, или None}. Пусто или «—» — убрать все."""
        import ipaddress
        gw = self._gw_slot(slot_id)
        text = (raw or "").strip()
        kept: list[str] = []
        rejected: list[tuple[str, str]] = []
        if text and text not in ("-", "—"):
            for tok in re.split(r"[\s,;]+", text):
                if not tok:
                    continue
                try:
                    net = ipaddress.ip_network(tok, strict=False)
                except ValueError:
                    rejected.append((tok, "не похоже на подсеть"))
                    continue
                if net.version != 4:
                    rejected.append((tok, "только IPv4"))
                    continue
                if any(net.overlaps(ipaddress.ip_network(s, strict=False))
                       for s, _i in config.routing_client_subnets()):
                    rejected.append((tok, "это клиентская подсеть AWG"))
                    continue
                if any(net.overlaps(ipaddress.ip_network(g.link_cidr, strict=False))
                       for g in self.db.gateways()):
                    rejected.append((tok, "это подсеть линка"))
                    continue
                if str(net) not in kept:
                    kept.append(str(net))
        self.db.gateway_update(gw.id, home_subnets=kept)
        conflict = None
        for g in self.db.gateways():
            if g.id != gw.id and set(g.home_subnets) & set(kept):
                conflict = g
                break
        try:
            self._ensure_gateway_policy()
        except routing.RoutingError as e:
            log.warning("gateway_set_home_subnets: маршруты не доведены: %s", e)
        return {"kept": kept, "rejected": rejected, "conflict": conflict}

    def gateway_slots_policy(self) -> list[tuple[int, str, list[str]]]:
        """[(слот, интерфейс, подсети)] для ensure_policy: домашняя подсеть,
        заданная двум слотам, достаётся первому по порядку."""
        seen: set[str] = set()
        out = []
        for g in self.db.gateways():
            nets = [n for n in g.home_subnets if n not in seen]
            seen.update(nets)
            out.append((g.id, g.link_if, nets))
        return out

    def _ensure_gateway_policy(self) -> None:
        active = self.active_gateway()
        if active is None:
            routing.ensure_policy()
            return
        routing.ensure_policy(active.link_if, self.gateway_slots_policy())

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
        with self.db.transaction():
            self.db.set_state(self._RT_ACTIVE_KEY, str(gw.id))
            if manual:
                # Переключили руками на лежащий шлюз — значит, так надо: автомат
                # его не перекладывает обратно, пока он не оживёт. На живой —
                # удержания нет: упадёт, автомат переключит на живой резерв.
                down = int(self.db.get_state(f"routing_gw_{gw.id}_down_streak") or 0)
                up = int(self.db.get_state(f"routing_gw_{gw.id}_up_streak") or 0)
                dead = down >= self._RT_DOWN_STREAK or (up == 0 and down > 0)
                self.db.set_state(self._RT_HOLD_KEY, str(gw.id) if dead else "")
            else:
                self.db.set_state(self._RT_SWITCHED_KEY, timeutil.to_iso(timeutil.now()))
        log.info("routing: %s переключение на слот %s (%s)",
                 "ручное" if manual else "автоматическое", gw.id, gw.link_if)
        return gw

    # ── пинг до шлюза и его внешний IP: кэш на сутки, лениво ─────────────────
    _GW_PING_KEY = "routing_gw_ping"
    _GW_EXTIP_KEY = "routing_gw_extip"
    _GW_PING_TTL = 24 * 3600

    def _gw_ping_forget(self, slot_id: int) -> None:
        self.db.set_state(f"{self._GW_PING_KEY}_{int(slot_id)}", "")
        self.db.set_state(f"{self._GW_EXTIP_KEY}_{int(slot_id)}", "")

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

    def gateway_external_ip_cached(self, slot_id: int) -> Optional[str]:
        c = self._gw_cached(self._GW_EXTIP_KEY, slot_id)
        return c[0] if c else None

    def gateway_external_ip(self, slot_id: int) -> Optional[str]:
        """Внешний адрес дома шлюза — через таблицу слота; в кэш на сутки."""
        gw = self._gw_slot(slot_id)
        try:
            # без правила по метке запрос ушёл бы через основную таблицу ВПС и
            # вернул бы адрес ВПС — проверяем обвязку слота перед замером
            routing.ensure_slot_policy(gw.id, gw.link_if)
        except routing.RoutingError as e:
            log.warning("gateway: обвязка слота %s не доведена: %s", gw.id, e)
            return None
        ip = routing.external_ip(gw.mark)
        if ip:
            self.db.set_state(f"{self._GW_EXTIP_KEY}_{gw.id}", f"{ip} {timeutil.to_iso(timeutil.now())}")
        else:
            self.db.set_state(f"{self._GW_EXTIP_KEY}_{gw.id}", "")
        return ip

    def gateway_external_ip_lazy(self, slot_id: int) -> Optional[str]:
        return self.gateway_external_ip_cached(slot_id) or self.gateway_external_ip(slot_id)

    def gateway_ping_cached(self, slot_id: int) -> Optional[tuple[int, str]]:
        """(мс, когда) из кэша, если ему меньше суток."""
        c = self._gw_cached(self._GW_PING_KEY, slot_id)
        return (int(c[0]), c[1]) if c else None

    def gateway_ping(self, slot_id: int) -> Optional[int]:
        """Пинг с ВПС ДО шлюза: ICMP по линку на адрес шлюза, медиана трёх.
        Успех — в кэш; отказ — кэш снят, None."""
        gw = self._gw_slot(slot_id)
        ms = routing.ping_peer(gw.link_if)
        if ms is None:
            self.db.set_state(f"{self._GW_PING_KEY}_{gw.id}", "")
            return None
        self.db.set_state(f"{self._GW_PING_KEY}_{gw.id}",
                          f"{int(ms)} {timeutil.to_iso(timeutil.now())}")
        return int(ms)

    def gateway_ping_lazy(self, slot_id: int) -> Optional[int]:
        """Для экранов: из кэша, а без него — замер (первое открытие экрана)."""
        cached = self.gateway_ping_cached(slot_id)
        if cached is not None:
            return cached[0]
        return self.gateway_ping(slot_id)

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
            down = int(self.db.get_state(f"routing_gw_{g.id}_down_streak") or 0)
            up = int(self.db.get_state(f"routing_gw_{g.id}_up_streak") or 0)
            is_active = active is not None and active.id == g.id
            link_ok = (self.routing_link_ok() if is_active
                       else up >= self._RT_UP_STREAK and down == 0)
            out.append({
                "gateway": g, "device": dev, "active": is_active, "preferred": bool(g.preferred),
                "link_ok": link_ok, "handshake_age": age, "down_ticks": down, "up_ticks": up,
                "issued_at": self.db.get_state(self._gw_slot_key(self._GW_BUNDLE_ISSUED_KEY, g.id)) or "",
                "display": self._gw_display(g),
                "ping": self.gateway_ping_cached(g.id),
            })
        return out

    def gateway_state(self, slot_id: Optional[int] = None) -> dict:
        """Состояние одного слота (по умолчанию первого) — экраны раздела.
        Без слотов: {'device': None, 'gateway': None}."""
        states = self.gateway_states()
        if not states:
            return {"device": None, "gateway": None, "issued_at": "", "link_ok": False,
                    "handshake_age": None, "active": False, "preferred": False, "ping": None,
                    "down_ticks": 0, "up_ticks": 0, "display": ""}
        if slot_id:
            for st in states:
                if st["gateway"].id == slot_id:
                    return st
            raise ServiceError("такого слота шлюза нет")
        first = self.gateway_first_slot()
        return next(st for st in states if st["gateway"].id == first.id)

    def gateway_screen_state(self, slot_id: int, *, lazy_ping: bool = True) -> dict:
        """Состояние слота для карточек: к gateway_state — пинг (лениво: пустой
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
        elif lazy_ping:
            st["ping_ms"] = self.gateway_ping(slot_id)
        else:
            st["ping_ms"] = None
        st["ext_ip"] = self.gateway_external_ip_cached(slot_id)
        if st["ext_ip"] is None and lazy_ping:
            st["ext_ip"] = self.gateway_external_ip(slot_id)
        return st

    def gateway_state_for_device(self, device_id: int) -> Optional[dict]:
        gw = self.db.gateway_by_device(device_id)
        if gw is None:
            return None
        return self.gateway_screen_state(gw.id)

    def gateway_uplink_conf(self, dev) -> str:
        """Конфиг аплинка шлюза для бандла: обычный клиентский .conf устройства
        в форме для машины-шлюза (без DNS, Table = off)."""
        cfg = self.generate_config(dev.id, for_bundle=True)
        return configgen.gateway_uplink_conf(cfg["conf"])

    _GW_BUNDLE_SSH_KEY = "gw_bundle_ssh_allow"
    _GW_BUNDLE_SSH_NOTIFIED_KEY = "gw_bundle_ssh_allow_notified"

    def _gw_ssh_allow(self) -> list[str]:
        return sorted(set(self.db.admin_device_addresses(config.ADMIN_ID)))

    def gw_bundle_drift_notes(self) -> list[Notification]:
        """Состав устройств админа разошёлся с тем, что уехало в бандл слота:
        напомнить один раз на каждое новое расхождение, по слоту. Пока бандл
        слота не собирали — молчим: напоминать не о чем."""
        if not config.ROUTING_ENABLED:
            return []
        cur = " ".join(self._gw_ssh_allow())
        notes = []
        # без слотов — слот-заглушка линка обвязки, но только при включённой
        # функции: после снятия последнего шлюза напоминать некому
        for g in self.db.gateways() or ([self._gw_slot(None)]
                                        if settings.get_bool("app.routing.enabled", False) else []):
            sent = self.db.get_state(self._gw_slot_key(self._GW_BUNDLE_SSH_KEY, g.id))
            if sent is None:                    # бандл слота не собирали — напоминать не о чем
                continue
            if cur == sent or self.db.get_state(self._gw_slot_key(self._GW_BUNDLE_SSH_NOTIFIED_KEY, g.id)) == cur:
                continue
            self.db.set_state(self._gw_slot_key(self._GW_BUNDLE_SSH_NOTIFIED_KEY, g.id), cur)
            notes.append(Notification(
                config.ADMIN_ID,
                f"🛰 Список твоих устройств изменился, а файервол шлюза {self._gw_display(g)} "
                "знает прежний: новые устройства не достанут до шлюза и его домашней сети "
                "через туннель. Перевыпусти конфигурацию шлюза (Условная маршрутизация → "
                f"{self._gw_display(g)} → Конфигурация шлюза) и примени её на шлюзе."))
        return notes

    # Маркер контракта как ОТДЕЛЬНАЯ СТРОКА. Тот же текст встречается в бандле и
    # внутри sed-выражения, которым он вырезает скрипт обвязки; вставка туда
    # ломала sed, и на шлюз ложился пустой скрипт (наступили: 09.09.2026).
    _MAIL_MARK_LINE = re.compile(rb"^#__GW_SETUP_BELOW__$", re.M)

    # ── токен бота-агента: свой у каждого слота (два агента на одном токене
    # перехватывали бы апдейты друг у друга), спрашивается один раз на слот ──
    _GW_TOKEN_ENV = "GW_BOT_TOKEN"

    def _gw_token_env(self, slot_id: Optional[int]) -> str:
        return self._GW_TOKEN_ENV if not slot_id or int(slot_id) == 1 else f"{self._GW_TOKEN_ENV}_{int(slot_id)}"

    @staticmethod
    def _env_path() -> str:
        return os.environ.get("AWG_BOT_ENV", "/etc/awg-bot/env")

    def gw_bot_token(self, slot_id: Optional[int] = None) -> str:
        """Токен бота шлюза слота из env. Пусто — ещё не спрашивали."""
        key = self._gw_token_env(slot_id)
        try:
            with open(self._env_path(), encoding="utf-8") as f:
                for line in f:
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    def set_gw_bot_token(self, token: str, slot_id: Optional[int] = None) -> None:
        """Запомнить токен агента. Хранение осознанное: без него перевыпуск
        файла первого применения (переустановили машину-шлюз, сменили её)
        снова требовал бы идти в BotFather. Уровень доверия тот же, что у
        приватных ключей, которые в этом файле и так лежат."""
        token = str(token).strip()
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ServiceError("это не похоже на токен бота — жду строку вида 123456789:AA…")
        path = self._env_path()
        key = self._gw_token_env(slot_id)
        try:
            lines = []
            try:
                with open(path, encoding="utf-8") as f:
                    lines = [ln for ln in f.read().splitlines()
                             if not ln.startswith(key + "=")]
            except FileNotFoundError:
                pass
            lines.append(f"{key}={token}")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(path, 0o600)
        except OSError as e:
            raise ServiceError(f"не записать {path}: {e}")

    def _bundle_with_agent(self, plain: bytes, slot_id: Optional[int] = None) -> bytes:
        """Токен агента и ADMIN_ID — в файл первого применения, чтобы установка
        на шлюзе не задавала ВООБЩЕ ни одного вопроса.

        Строки кладутся ПОСЛЕ `exit` в теле бандла (перед маркером скрипта
        обвязки), то есть при запуске бандла не исполняются — это данные для
        установщика, а не команды.
        """
        token = self.gw_bot_token(slot_id)
        if not token:
            return plain
        m = self._MAIL_MARK_LINE.search(plain)
        if m is None:
            return plain
        lines = (f'AGENT_BOT_TOKEN="{token}"\n'
                 f'AGENT_ADMIN_ID="{config.ADMIN_ID}"\n').encode()
        return plain[:m.start()] + lines + plain[m.start():]

    def _bundle_with_mail(self, plain: bytes) -> bytes:
        """Настройки почты и парольная фраза бэкапов — в бандл, чтобы не вводить
        их дважды: строки MAIL_B64 / BACKUP_B64 (JSON в base64) перед строкой
        маркера контракта. Бандл шифрован ключом линка. Чего нет на ВПС — не
        добавляется, агент оставляет своё как есть."""
        m = self._MAIL_MARK_LINE.search(plain)
        if m is None:
            return plain
        import base64
        import json
        lines = b""
        acc = self.email_account()
        if acc is not None:
            payload = json.dumps({"login": acc.login, "password": acc.password,
                                  "imap_host": acc.imap_host, "imap_port": acc.imap_port,
                                  "smtp_host": acc.smtp_host, "smtp_port": acc.smtp_port},
                                 ensure_ascii=False).encode()
            lines += b'MAIL_B64="' + base64.b64encode(payload) + b'"\n'
        if self.backup_encryption_mode() == "passphrase":
            phrase = self.db.get_state(self._BK_PASSPHRASE_KEY) or ""
            lines += b'BACKUP_B64="' + base64.b64encode(json.dumps({"passphrase": phrase}).encode()) + b'"\n'
        if not lines:
            return plain
        return plain[:m.start()] + lines + plain[m.start():]
