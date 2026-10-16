"""mark.py — пометка устройства шлюзом: запасной путь через пересланный токен."""

from __future__ import annotations

import re
from typing import Optional
from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import routing
from awgbot.domain.services.types import ServiceError
from .common import log


class MarkMixin:
    """Пометка устройства шлюзом: запасной путь через пересланный токен."""
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
        'device', 'gateway'}: конфигурацию слоту везёт документ по каналу.
        Слот занят другим устройством — ServiceError: менять машину — через
        карточку шлюза."""
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
            raise ServiceError(f"на этом линке уже назначен шлюз {self._gw_display(slot)}. "
                               "Заменить устройство можно в карточке шлюза: «✏️ Изменить» → «🔁 Заменить»")
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
            raise ServiceError("Такого шлюза нет")
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
            raise ServiceError(f"это устройство уже шлюз {other.id}")
        # Шлюзу РФ-доступ не нужен никогда: его российский трафик на ВПС
        # вернулся бы по линку на тот же шлюз. Снимаем при назначении (парно,
        # с двойником) — дальше устройство в списке РФ-доступа не показывается.
        routing_reset = bool(dev.routing_on)
        if routing_reset:
            self.set_routing_device(dev.id, False)
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
                self.gateway_rekey_abort(gw)          # смена ключей каналом, если шла, — снята
                self._run_link_script("--rekey", self._slot_env(gw))
        gw = self.db.gateway(gw.id)
        if rekey or new_slot:
            routing.invalidate_self_check()
            try:
                self._ensure_gateway_policy()
            except routing.RoutingError as e:
                log.warning("gateway_setup: обвязка слота не доведена: %s", e)
        return {"gateway": gw, "device": self.db.get_device(dev.id), "previous": prev,
                "created": created, "rekeyed": rekey, "routing_reset": routing_reset}

    def _slot_forget(self, slot_id: int) -> None:
        """Всё состояние слота — в БД и в памяти — одним списком. Раньше его
        чистили три ручных перечня, и новый слот с тем же номером наследовал
        хвосты: ложное «снова отвечает», «лежит» до 5 минут, ложное
        «конфигурация актуализирована»."""
        sid = int(slot_id)
        for key in (self._GW_BUNDLE_ISSUED_KEY, self._GW_BUNDLE_SSH_KEY,
                    self._GW_DRIFT_NOTIFIED_KEY, self._GW_BOT_ME_KEY, self._REKEY_KEY, "gw_link_port_hand",
                    self._REKEY_CANCEL_KEY, self._TOKEN_PUSH_KEY):
            self.db.set_state(self._gw_slot_key(key, sid), "")
        for key in self._rt_keys(sid):                     # стрик и «объявлен»
            self.db.set_state(key, "")
        if (self.db.get_state(self._RT_HOLD_KEY) or "") == str(sid):
            self.db.set_state(self._RT_HOLD_KEY, "")
        self._gw_ping_forget(sid)
        self._standby_forget(sid)
        self._rt_window_reset(sid)                         # окно замеров живости
        self.gwlink_forget(sid)                            # снимок и сессия канала
        # токен бота снятого устройства: новый слот с тем же номером иначе увёз
        # бы его в файл первого применения — два агента на одном токене
        self.forget_gw_bot_token(sid)

    def _smb_tunnel_after_remove(self, last: bool) -> None:
        """Снятый шлюз уносит свои SMB из резолвера админа сразу; снят
        последний — «SMB в туннеле» выключается молча: показывать нечего, и
        включённый тумблер без шлюзов только путал бы."""
        sync = getattr(self, "smb_tunnel_sync", None)
        if sync is None:
            return
        try:
            if last and settings.get_bool("app.routing.smb_tunnel", False):
                settings.set_value("app.routing.smb_tunnel", False)
                log.info("gateway_remove: снят последний шлюз — «SMB в туннеле» выключен")
            sync(force=True)
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway_remove: записи резолвера админа не подтянуты: %s", e)

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
        # вычислится как первый оставшийся, и маршрут в ядре не переложится.
        # На живой резерв, если такой есть: мёртвый держался бы удержанием.
        if others and active is not None and active.id == gw.id:
            target = next((g for g in others if not self._rt_unavailable(g.id)), others[0])
            self.gateway_switch(target.id, manual=True)
        self.gateway_rekey_abort(gw)                  # .next/.prev смены ключей — прочь вместе со слотом
        self.db.gateway_delete(gw.id)
        self._slot_forget(gw.id)
        if not others:
            try:
                settings.set_value("app.routing.enabled", False)
            except Exception as e:                            # noqa: BLE001
                log.warning("gateway_remove: маршрутизация не выключена: %s", e)
        self._smb_tunnel_after_remove(last=not others)
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
            self.lan_map_ensure()                          # адреса в туннеле слота — из таблицы
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway_remove: адреса в туннеле: %s", e)
        try:
            self.reconcile_routing()
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway_remove: реконсиляция: %s", e)
        try:
            # снятый шлюз больше не держит обновление сервера: пересчёт без сети;
            # уведомить отсюда нечем — уведомит страховочный пересчёт такта канала
            self.update_regate(notify=False)
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway_remove: пересчёт цели обновления: %s", e)
        return prev

    def gateway_set_preferred(self, slot_id: Optional[int]) -> None:
        if slot_id is not None and self.db.gateway(slot_id) is None:
            raise ServiceError("Такого шлюза нет")
        self.db.gateway_set_preferred(slot_id)

    def gateway_set_label(self, slot_id: int, label: str) -> None:
        label = " ".join(str(label).split())[:20].strip()
        if label in ("-", "—"):                           # «—» — убрать подпись
            label = ""
        self._gw_slot(slot_id)
        self.db.gateway_update(slot_id, label=label)

    _LAN_SUBNETS_MAX = 64                       # подсетей на слот — как _MAX_SUBNETS документа на шлюзе
    _PRIVATE_NETS = tuple(__import__("ipaddress").ip_network(n)
                          for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))

    def gateway_set_lan_subnets(self, slot_id: int, raw: str) -> dict:
        """Разбор пачки подсетей: IPv4, частная, /16–/30, вне служебного
        пространства адресов в туннеле, не пересекается с другой подсетью
        списка ни сама, ни адресом в туннеле, не клиентская, не подсеть
        линка. Возвращает {'kept': [...], 'rejected': [(строка, причина)],
        'twins': [(настоящая, в туннеле)]}. Пусто или «—» — убрать все.
        Подсети других шлюзов не задевают: снаружи шлюза сеть видна по
        адресам в туннеле, и одинаковые подсети за разными шлюзами не спорят."""
        import ipaddress
        from awgbot.util import lanspace
        gw = self._gw_slot(slot_id)
        text = (raw or "").strip()
        if gw.lan_mode and (not text or text in ("-", "—")):
            # без подсети скрипт обвязки не найдёт свой интерфейс — бандл ушёл бы в отказ
            raise ServiceError("включён VPN-транзит: сначала выключи его, потом убирай подсети")
        kept: list[str] = []
        rejected: list[tuple[str, str]] = []
        space = lanspace.space()
        if text and text not in ("-", "—"):
            for tok in re.split(r"[\s,;]+", text):
                if not tok:
                    continue
                if len(kept) >= self._LAN_SUBNETS_MAX:
                    # тот же предел, что проверка документа на шлюзе (gwcfg/check.py)
                    rejected.append((tok, f"у шлюза не больше {self._LAN_SUBNETS_MAX} подсетей"))
                    continue
                try:
                    net = ipaddress.ip_network(tok, strict=False)
                except ValueError:
                    rejected.append((tok, "не похоже на подсеть"))
                    continue
                if net.version != 4:
                    rejected.append((tok, "только IPv4"))
                    continue
                if not any(net.subnet_of(p) for p in self._PRIVATE_NETS):
                    # публичным адресам в локальной сети не место, и документ шлюза
                    # их не примет. Ровно три частных диапазона: is_private считает
                    # частными и служебные (198.18/15, 192.0.2/24).
                    rejected.append((tok, "нужна частная подсеть: 10.0.0.0/8, 172.16.0.0/12 или 192.168.0.0/16"))
                    continue
                if lanspace.too_wide(net):
                    # на шлюз в туннеле приходится /16: шире двойник не влезет
                    rejected.append((tok, "шире /16: в туннеле на шлюз приходится /16, сузь подсеть"))
                    continue
                if net.prefixlen > 30:
                    rejected.append((tok, "это один адрес, а нужна подсеть"))
                    continue
                if lanspace.in_space(net, space):
                    # на шлюзе её адреса совпали бы с адресами соседей в его AllowedIPs
                    rejected.append((tok, f"в служебном пространстве сервера AWG ({space}): выбери другую подсеть"))
                    continue
                if any(net.overlaps(ipaddress.ip_network(k)) for k in kept):
                    # на шлюзе своя сеть одна: nft отвергает пересечения внутри
                    # interval-набора, обвязка не приняла бы такие LAN_SUBNETS
                    rejected.append((tok, "пересекается с другой подсетью в списке"))
                    continue
                other = lanspace.twin_clash(gw.id, net, kept, space)
                if other:
                    rejected.append((tok, self._lan_clash_reason(gw.id, net, other, space)))
                    continue
                if any(net.overlaps(ipaddress.ip_network(s, strict=False))
                       for s, _i in config.routing_client_subnets()):
                    # подсеть клиентов — в AllowedIPs линка и tunnel_nets4 каждого
                    # шлюза, аплинк шлюза — адрес из неё: адресный план сервера
                    rejected.append((tok, "это клиентская подсеть AWG"))
                    continue
                if any(net.overlaps(ipaddress.ip_network(g.link_cidr, strict=False))
                       for g in self.db.gateways()):
                    rejected.append((tok, "это подсеть линка"))
                    continue
                if str(net) not in kept:
                    kept.append(str(net))
            if not kept:
                # ни одной годной подсети — прежние не трогаем: иначе опечатка
                # стирала бы список и обходила запрет при VPN-транзите
                why = "; ".join(f"{raw[:40]} — {reason}" for raw, reason in rejected[:3])
                raise ServiceError(f"не принято: {why}")
        self.db.gateway_update(gw.id, lan_subnets=kept)
        self._channel_touch()                              # LAN_SUBNETS — шлюзу сразу
        try:
            self.lan_map_ensure()                          # адреса в туннеле: убранные — прочь, новые — в таблицу
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway_set_lan_subnets: адреса в туннеле не доведены: %s", e)
        return {"kept": kept, "rejected": rejected, "twins": self.lan_twins(self.db.gateway(gw.id))}

    @staticmethod
    def _lan_clash_reason(slot_id: int, net, other: str, space) -> str:
        """П4: адреса в туннеле двух подсетей одного шлюза пересеклись бы."""
        from awgbot.util import lanspace
        a, b = lanspace.twin_net(slot_id, net, space), lanspace.twin_net(slot_id, other, space)
        if a == b:
            how = f"у обеих {a}"
        elif a.subnet_of(b):
            how = f"{a} внутри {b}"
        else:
            how = f"{b} внутри {a}"
        return (f"в туннеле пересеклась бы с {other} ({how}): адреса в туннеле подсетей одного "
                "шлюза не должны пересекаться")

    def gateway_slots_policy(self) -> list[tuple[int, str]]:
        """[(слот, интерфейс)] для ensure_policy: таблица и правило каждого
        слота, MSS-кламп в его линк. Подсети сюда не идут — в сеть шлюза ведёт
        адрес в туннеле (lan_map_ensure)."""
        return [(g.id, g.link_if) for g in self.db.gateways()]

    def _ensure_gateway_policy(self) -> None:
        active = self.active_gateway()
        if active is None:
            routing.ensure_policy()
            return
        routing.ensure_policy(active.link_if, self.gateway_slots_policy())
