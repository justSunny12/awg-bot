"""mark.py — пометка устройства шлюзом: запасной путь через пересланный токен."""

from __future__ import annotations

import re
from typing import Optional
from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import routing
from awgbot.domain.services.types import ServiceError
from . import slotkeys
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

    def _slot_forget(self, slot_id: int) -> None:
        """Всё состояние слота — в БД и в памяти. Ключи БД — по реестру
        slotkeys: без него их чистили три ручных перечня, и новый слот с тем
        же номером наследовал хвосты: ложное «снова отвечает», «лежит» до 5
        минут, ложное «конфигурация актуализирована». Здесь — помеченные
        «со слотом», ключи устройства снимает gwlink_forget."""
        sid = int(slot_id)
        slotkeys.forget(self.db, sid, slotkeys.names(when=slotkeys.SLOT))
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

    _REMOVED_KEY = "gw_removed"
    _REMOVED_KEEP_DAYS = 183
    _REMOVED_MAX = 32

    def _removed_journal_add(self, gw, dev) -> None:
        """Журнал снятых слотов — для одного различения при восстановлении:
        «слотов нет, потому что сервер новый» против «потому что все сняли».
        Полгода, не больше 32 записей."""
        import json
        from awgbot.util import timeutil
        from awgbot.domain.backup import meta as _meta
        now = timeutil.now()
        raw = self.db.get_state_json(self._REMOVED_KEY, [])
        items = []
        for r in raw if isinstance(raw, list) else []:
            try:
                if isinstance(r, dict) and timeutil.elapsed(timeutil.parse_iso(str(r.get("at")))) \
                        < self._REMOVED_KEEP_DAYS * 86400:
                    items.append(r)
            except ValueError:
                continue
        link_gw_pub, _vps = _meta.link_pubs(gw.link_if)
        items.append({"slot": gw.id, "link_gw_pub": link_gw_pub,
                      "device_pub": dev.public_key if dev is not None else "", "at": timeutil.to_iso(now)})
        self.db.set_state(self._REMOVED_KEY, json.dumps(items[-self._REMOVED_MAX:], ensure_ascii=False))

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
        self._backup_event(gw.id, "remove")           # имя — пока слот есть
        self._removed_journal_add(gw, prev)           # до смены ключей и снятия линка
        self._reserve_after_gateway_change("после снятия шлюза")   # доля ключа переезда у него была
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
        return [(g.id, g.link_if) for g in self.rt_slots()]

    def _ensure_gateway_policy(self) -> None:
        active = self.active_gateway()
        if active is None:
            routing.ensure_policy()
            return
        routing.ensure_policy(active.link_if, self.gateway_slots_policy())
