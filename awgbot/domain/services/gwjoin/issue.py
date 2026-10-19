"""issue.py — часть пакета gwjoin (см. __init__): команда подключения.

Слот — новый (номер, которого нет ни в `gateways`, ни у другого живого
сопряжения) или слот замены. Линк слота сразу переходит на ключи токена
(`routing-link-setup.sh --join-stage`: ключи и Jc…H4 без PSK и без I1,
штамп «сопряжение» — карантин в файерволе); при замене прежние ключи — в
`.join-prev`. Устройство слота и строка `gateways` — только после верного
кода (accept.py). Новая команда того же слота убивает прежнюю: её сообщение —
«Не актуально».
"""
from __future__ import annotations

import secrets
from typing import Optional

from awgbot.core import config, settings
from awgbot.domain.services.types import ServiceError
from awgbot.infra import routing
from awgbot.util import gwlink, jointoken, timeutil, wgconf

from ..gateway_link.common import log
from .state import JOIN_TTL, OPEN_OR_CLOSING, _LOCK, new_device_name, slot_ns, slot_of


JOIN_FLEET_BUSY = "Идёт обновление набора — подключение и замена шлюзов после его итога"
JOIN_NO_CONTAINER = ("Подключение шлюза командой недоступно: AmneziaWG сервера работает в контейнере, а линк до "
                     "верного кода держит в карантине файервол хоста, а в этом режиме линки он не ведёт")


def _new_id() -> str:
    """id сопряжения: 8 случайных байт, hex."""
    return secrets.token_hex(8)


class GwJoinIssueMixin:
    """Выпуск команды подключения."""

    def _gwjoin_next_slot(self) -> tuple[int, str, int, str]:
        """(номер, интерфейс, порт, /30) нового слота: как gateway_next_slot,
        но мимо слотов, занятых другими живыми сопряжениями (строки у них нет)."""
        from awgbot.util import lanspace
        slots = self.db.gateways()
        # новые слоты в сопряжении — без строки до кода; после кода строка есть,
        # и запись (accepted / linked) не должна занимать номер второй раз
        pending = [st for st in self.gwjoin_all() if st.get("kind") == "new"
                   and st.get("state") in OPEN_OR_CLOSING
                   and not self.db.gateway(int(st["slot"]))]
        if len(slots) + len(pending) >= config.ROUTING_GATEWAYS_MAX:
            from awgbot.domain.notices.routing import gateways_full
            raise ServiceError(gateways_full(len(slots), config.ROUTING_GATEWAYS_MAX))
        used_ids = {g.id for g in slots} | {int(st["slot"]) for st in pending}
        used_if = {g.link_if for g in slots} | {st["link_if"] for st in pending}
        used_port = {g.link_port for g in slots} | {int(st["port"]) for st in pending}
        used_cidr = {g.link_cidr for g in slots} | {st["cidr"] for st in pending}
        for n in range(1, config.ROUTING_GATEWAYS_MAX + 1):
            if n in used_ids:
                continue
            if n > lanspace.MAX_SLOTS:
                raise ServiceError(f"шлюзов не больше {lanspace.MAX_SLOTS}: на каждый — свои адреса в туннеле")
            iface = (config.ROUTING_GW_INTERFACE or "awglink") if n == 1 else f"awglink{n}"
            if n > len(config.ROUTING_LINK_PORTS) or n > len(config.ROUTING_LINK_CIDRS):
                raise ServiceError(f"для шлюза {n} нет порта или подсети линка в routing.link_ports / link_cidrs")
            port, cidr = config.ROUTING_LINK_PORTS[n - 1], config.ROUTING_LINK_CIDRS[n - 1]
            if iface in used_if or port in used_port or cidr in used_cidr:
                continue
            return n, iface, port, cidr
        raise ServiceError("свободного слота нет")

    def _gwjoin_version(self) -> str:
        """Выпуск для шлюза: версия агента в выпуске набора; набора нет
        (сервер не на сборке из выпуска) — версия сервера."""
        try:
            want = self.fleet_gw_target()
        except Exception:                                 # noqa: BLE001
            want = None
        if want is not None:
            return str(want.tag).lstrip("v")
        return str(config.INSTALLED_VERSION).lstrip("v")

    def _gwjoin_kit(self) -> bool:
        """Выпуска токена на GitHub нет (flow-12): сервер не на сборке из
        выпуска (своя сборка будущего выпуска) — шлюз ставится только китом.
        Выпуски не проверялись — неизвестно: как обычно."""
        try:
            if self.fleet_releases() is None:
                return False
            return not self.fleet_server_release_build() and self.fleet_gw_target() is None
        except Exception:                                 # noqa: BLE001
            return False

    def gwjoin_command(self, token: str) -> str:
        repo = str(config.UPDATES_REPO or "justSunny12/awg-bot")
        return (f"curl -fsSL https://raw.githubusercontent.com/{repo}/main/install/awg-bot-install.sh "
                f"| sudo bash -s -- --join {token}")

    def _gwjoin_check_device(self, device_id: Optional[int], slot_id: Optional[int]) -> None:
        if device_id is None:
            return
        admin = self.admin_client()
        dev = self.db.get_device(device_id)
        if admin is None or dev is None:
            raise ServiceError("Устройство не найдено")
        if dev.client_id != admin.id:
            raise ServiceError("шлюзом может быть только устройство профиля админа")
        if not dev.private_key:
            raise ServiceError("это устройство создавал не бот — шлюзом его не сделать")
        other = self.db.gateway_by_device(dev.id)
        if other is not None and other.id != slot_id:
            raise ServiceError(f"это устройство уже шлюз {other.id}")

    def gwjoin_issue(self, *, device_id: Optional[int] = None, slot_id: Optional[int] = None,
                     same: bool = False) -> dict:
        """«🛰 Подключить шлюз» (slot_id нет: новый слот; device_id — своё
        устройство, None — новое «Шлюз N») или замена в слоте slot_id
        (device_id — своё / None — новое; same — «🔁 Настроить заново»: то же
        устройство). {"state", "token", "command", "stale": сообщение прежней
        команды слота или None}."""
        if self.admin_client() is None:
            raise ServiceError("профиль админа ещё не создан")
        self._gwjoin_check_runtime()
        self._gwjoin_check_fleet()
        with _LOCK:
            gw = self.db.gateway(slot_id) if slot_id else None
            if slot_id and gw is None:
                raise ServiceError("Такого шлюза нет")
            if same and gw is not None:
                device_id = gw.device_id
            self._gwjoin_check_device(device_id, slot_id)
            if gw is None:
                n, iface, port, cidr = self._gwjoin_next_slot()
                kind = "new"
            else:
                n, iface, port, cidr = gw.id, gw.link_if, gw.link_port, gw.link_cidr
                kind = "same" if same else "replace"
            old = self.gwjoin(n)
            stale = old.get("msg") if old.get("msg") else None
            jid = _new_id()
            slot = gw if gw is not None else slot_ns(n, iface, port, cidr)
            env = {**self._slot_env(slot), "JOIN_ID": jid}
            if kind != "new":
                env["JOIN_PREV"] = "1"
                self.gateway_rekey_abort(gw)              # смена ключей каналом, если шла, — снята
                try:
                    routing.drop_slot_policy(gw.id, gw.link_if)   # слот в замене трафика не несёт
                except routing.RoutingError as e:
                    log.warning("сопряжение: политика слота %s не снята: %s", gw.id, e)
            self._gwjoin_live_set(n, None)                # сессия прежней команды больше ничего не значит
            try:
                self._run_link_script("--join-stage", env)
            except ServiceError:
                # скрипт отказал на полпути — линк назад, как при отмене (quar-4)
                self._gwjoin_unstage(kind, slot)
                raise
            now = timeutil.now()
            st = {"id": jid, "kind": kind, "slot": n, "link_if": iface, "port": int(port), "cidr": cidr,
                  "device": device_id if device_id is not None else "new",
                  "prev_device": gw.device_id if gw is not None else 0,
                  "name": self._gwjoin_name(n, gw, device_id), "version": self._gwjoin_version(),
                  "kit": self._gwjoin_kit(),
                  "issued": timeutil.to_iso(now), "expires": timeutil.to_iso(timeutil.shift(now, JOIN_TTL)),
                  "sessions": 0, "attempts": 0, "endpoints": [], "hosts": {}, "msg": [], "state": "waiting"}
            # выпуск в токене — тот же, что в записи (его покажет и сообщение подключения)
            token = self._gwjoin_token(slot, jid, version=st["version"], expires=st["expires"])
            self._gwjoin_save(n, st)
        try:
            self._gw_firewall_refresh(strict=True)        # карантин линка и порт канала — сразу
        except ServiceError as e:
            # без карантина команду не выдаём (quar-6): интерфейс под токен — прочь
            self.gwjoin_revert(n, "firewall")
            raise ServiceError(f"команда не выдана — {e}") from None
        if kind != "new":
            self._channel_touch()                         # подсети слота уходят из PEER_LAN_NETS соседей
        log.info("сопряжение: слот %s — команда выпущена (%s, id %s…)", n, kind, jid[:4])
        return {"state": st, "token": token, "command": self.gwjoin_command(token), "stale": stale}

    def gwjoin_peek_slot(self) -> int:
        """Номер слота, который получит следующая команда «🛰 Подключить шлюз»."""
        self._gwjoin_check_runtime()                      # до вопроса о токене, а не после
        self._gwjoin_check_fleet()
        return self._gwjoin_next_slot()[0]

    def _gwjoin_check_fleet(self) -> None:
        """Ход набора обновлений идёт (или откатывается): устройство слота в
        это время не меняется — иначе новое устройство унаследовало бы шаг хода
        прежнего, а откат ждал бы ответа, которого не будет."""
        if self.fleet_running():
            raise ServiceError(JOIN_FLEET_BUSY)

    @staticmethod
    def _gwjoin_check_runtime() -> None:
        """AmneziaWG сервера в контейнере (прежний режим runtime: docker):
        линки, порт канала и пересылку таблица хоста в нём не ведёт вовсе —
        карантин линка до кода встать не может, команда не выдаётся."""
        from awgbot.infra import awg
        if awg.in_container():
            raise ServiceError(JOIN_NO_CONTAINER)

    def gwjoin_command_for(self, st: dict) -> str:
        """Та же команда для сообщения подключения (правка по событию хода):
        токен собирается заново из конфигов линка и записи — совпадает с
        выданным; не собрался — пусто."""
        try:
            slot = slot_of(st)
            return self.gwjoin_command(self._gwjoin_token(slot, st["id"], version=str(st.get("version") or ""),
                                                          expires=str(st.get("expires") or "")))
        except (ServiceError, KeyError, ValueError, TypeError):
            return ""

    def _gwjoin_unstage(self, kind: str, slot) -> None:
        """Линк слота — назад после отказа --join-stage: замена — прежние ключи
        (.join-prev), новый первый слот — новые случайные, новый следующий —
        снят. Отказ и здесь — в журнал: запись не заводилась, повтора нет."""
        mode = "--join-revert" if kind != "new" else ("--rekey" if int(slot.id) == 1 else "--rollback")
        try:
            self._run_link_script(mode, self._slot_env(slot))
        except ServiceError as e:
            log.warning("сопряжение: слот %s — линк после отказа не возвращён (%s): %s", slot.id, mode, e)
        if kind != "new":
            try:
                self._ensure_gateway_policy()
            except routing.RoutingError as e:
                log.warning("сопряжение: политика слота %s не вернулась: %s", slot.id, e)

    def _gwjoin_name(self, slot: int, gw, device_id) -> str:
        if gw is not None:
            return self._gw_name(gw)
        if device_id is not None:
            dev = self.db.get_device(device_id)
            if dev is not None:
                return dev.name
        return new_device_name(slot)

    def _gwjoin_token(self, slot, jid: str, *, version: str = "", expires: str = "") -> str:
        """Токен из конфигов линка на ключах токена: закрытый ключ и Jc…H4 —
        из шлюзовой стороны (/root/gw-<линк>.conf), открытый ключ ВПС — оттуда
        же (пир), порт линка — из живого ListenPort стороны ВПС."""
        import base64
        try:
            with open(f"/root/gw-{slot.link_if}.conf", encoding="utf-8") as f:
                gconf = wgconf.parse(f.read())
            with open(f"{config.AWG_DIR}/{slot.link_if}.conf", encoding="utf-8") as f:
                vconf = wgconf.parse(f.read())
        except (OSError, wgconf.WgConfError) as e:
            raise ServiceError(f"конфиги линка на ключах токена не прочитаны: {e}")
        obf = {k: gconf.get("Interface", k) for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
                                                     "H1", "H2", "H3", "H4")}
        try:
            h = tuple(tuple(int(x) for x in str(obf[f"H{i}"]).split("-", 1)) for i in range(1, 5))
            h = tuple(x if len(x) == 2 else (x[0], x[0]) for x in h)
            tok = jointoken.JoinToken(
                slot=int(slot.id), version=version or self._gwjoin_version(), join_id=jid,
                expires=int(timeutil.parse_iso(expires).timestamp()) if expires else 0,
                host=self._gwjoin_host(), link_port=int(vconf.get("Interface", "ListenPort") or slot.link_port),
                channel_port=int(gwlink.channel_port()), gw_addr=gwlink.link_hosts(slot.link_cidr)[1],
                vps_pub=base64.b64decode(gconf.get("Peer", "PublicKey")),
                gw_priv=base64.b64decode(gconf.get("Interface", "PrivateKey")),
                jc=int(obf["Jc"]), jmin=int(obf["Jmin"]), jmax=int(obf["Jmax"]),
                s=tuple(int(obf[f"S{i}"]) for i in range(1, 5)), h=h)
            return jointoken.encode(tok)
        except (ValueError, TypeError, KeyError, jointoken.TokenError) as e:
            raise ServiceError(f"токен подключения не собрался: {e}")

    @staticmethod
    def _gwjoin_host() -> str:
        host = str(settings.get("app.network.server_host", config.SERVER_HOST) or "").strip()
        if not host:
            raise ServiceError("не задан адрес сервера AWG (network.server_host) — шлюзу некуда подключаться")
        return host
