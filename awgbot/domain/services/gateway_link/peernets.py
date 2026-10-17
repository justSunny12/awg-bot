"""peernets.py — VPN-транзит слота и связь подсетей за шлюзами (её часть: слоты с
VPN-транзитом связаны всегда, подсети соседей — их префиксы в туннеле)."""

from __future__ import annotations

from typing import Optional
from awgbot.util import timeutil
from awgbot.domain import configgen
from awgbot.domain.services.types import ServiceError
from .common import log



def transit_slots(gateways) -> list:
    """Кто считается шлюзом с VPN-транзитом: транзит включён и подсети заданы
    (без них адресов в туннеле нет, а включение транзита их и так требует).
    Одно правило на бота и CLI `awg-bot firewall` (tools/firewall.py) — иначе
    форвардинг линк ↔ линк у них разошёлся бы."""
    return [g for g in gateways if g.lan_mode and g.lan_subnets]


def bundle_file(link_if: str) -> str:
    """Имя файла первого применения слота — одно правило с routing-link-setup.sh
    (GW_BUNDLE_OUT): у первого линка прежнее имя, у остальных — с интерфейсом."""
    return "awg-gw-bundle.sh" if link_if in ("", "awglink") else f"awg-gw-bundle-{link_if}.sh"

class PeerNetsMixin:
    """VPN-транзит слота и связь подсетей за шлюзами."""
    # ── «за шлюзом — без VPN» ───────────────
    def gateway_resolver_addr(self, gw) -> str:
        """Апстрим резолвера шлюза — свой резолвер ВПС из DNS устройства слота;
        пусто — резолвера нет, шлюз возьмёт запасной через аплинк."""
        from awgbot.infra import resolver
        dev = self.db.get_device(gw.device_id) if gw.device_id else None
        if dev is None:
            return ""
        dns1, _dns2 = configgen.dns_for(getattr(dev, "iface", "") or "")
        return dns1 if resolver.is_private(dns1) and resolver.installed() else ""

    def _lan_env(self, gw) -> dict:
        """Переменные функций A и B для скрипта обвязки: LAN_MODE, LAN_SUBNETS
        (первая подсеть даёт LAN-интерфейс и адрес резолвера), RESOLVER,
        PEER_LAN_NETS (префиксы в туннеле других слотов с VPN-транзитом — в
        AllowedIPs и файервол)."""
        return {"LAN_MODE": "1" if gw.lan_mode else "0",
                "LAN_SUBNETS": " ".join(gw.lan_subnets),
                "RESOLVER": self.gateway_resolver_addr(gw) if gw.lan_mode else "",
                "PEER_LAN_NETS": " ".join(self.gateway_peer_nets(gw.id))}


    # ── связь подсетей: часть VPN-транзита ───
    def gateway_transit_slots(self) -> list:
        """Слоты с VPN-транзитом — по порядку номеров (правило — transit_slots)."""
        return transit_slots(self.db.gateways())

    def gateway_transit_links(self) -> list[str]:
        """Линки слотов с VPN-транзитом — файерволу ВПС для форвардинга
        линк ↔ линк (открыт только между ними и только при двух и больше)."""
        return [g.link_if for g in self.gateway_transit_slots()]

    def gateway_peer_nets(self, slot_id: int) -> list[str]:
        """Подсети соседей для слота (PEER_LAN_NETS): у слота VPN-транзит —
        префиксы в туннеле (/16) других слотов с VPN-транзитом; иначе пусто.
        От подсетей соседей не зависят: правка подсетей слота документы
        других не меняет, меняет только включение и выключение транзита.
        Пространство неисправно — пусто (адреса в туннеле выключены)."""
        from awgbot.util import lanspace
        me = self.db.gateway(slot_id)
        if me is None or not me.lan_mode or self.lan_space_problem():
            return []
        return [str(lanspace.slot_prefix(g.id)) for g in self.gateway_transit_slots()
                if g.id != me.id and g.id <= lanspace.MAX_SLOTS]

    def gateway_peer_nets_info(self) -> dict:
        """Строка «🔗 Связь подсетей» экрана «Шлюзы»: {'state', …}.
        state: off — адреса в туннеле выключены (пространство или метка, см.
        карточку); few — слотов с VPN-транзитом меньше двух; ok — pairs_named:
        [(имя, подпись, адреса в туннеле)]."""
        if self.lan_space_problem():
            return {"state": "off"}
        transit = self.gateway_transit_slots()
        if len(transit) < 2:
            return {"state": "few"}
        return {"state": "ok",
                "pairs_named": [(self._gw_name(g), g.label, [t for _r, t in self.lan_twins(g)]) for g in transit]}

    def gateway_set_lan_mode(self, slot_id: int, on: bool) -> dict:
        """Включить/выключить «за шлюзом — без VPN» у слота. Включение требует
        локальной подсети: по ней шлюз находит свой адрес. Возвращает
        {'gateway', 'resolver': адрес или '' (запасной апстрим)}. Связь
        подсетей — часть транзита: меняются LAN_MODE слота и PEER_LAN_NETS
        остальных слотов с транзитом (канал), и форвардинг линк ↔ линк на ВПС."""
        gw = self._gw_slot(slot_id)
        if on and not gw.lan_subnets:
            raise ServiceError("сначала задай локальную подсеть шлюза: по ней шлюз находит свой адрес")
        if on and (not gw.device_id or self.db.get_device(gw.device_id) is None):
            raise ServiceError("у слота нет устройства — без аплинка VPN-транзит не работает")
        self.db.gateway_update(gw.id, lan_mode=1 if on else 0)
        gw = self.db.gateway(gw.id)
        self._channel_touch()                              # LAN_MODE и подсети соседей — шлюзам сразу
        try:
            self.reconcile_ssh_access()                    # линки транзита — в таблицу файервола ВПС
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway_set_lan_mode: файервол ВПС не перерисован: %s", e)
        return {"gateway": gw, "resolver": self.gateway_resolver_addr(gw) if on else ""}

    @staticmethod
    def _bundle_path(gw) -> str:
        return f"/root/{bundle_file(gw.link_if)}"

    @staticmethod
    def bundle_name(gw) -> str:
        """Имя файла первого применения для слота — как его кладёт скрипт."""
        return bundle_file(gw.link_if)

    def _gw_bundle_build(self, gw) -> bytes:
        """Собрать файл первого применения слота скриптом линка (ключи не
        меняются) и дополнить токеном агента. Почты и фразы бэкапов в нём нет:
        их везёт документ конфигурации по каналу, а утечка открытого файла
        отдавала бы ящик и ключ ко всем копиям сервера."""
        env, admin_ips = self._gw_bundle_env(gw)
        self._run_link_script("--bundle", env)
        with open(self._bundle_path(gw), "rb") as f:
            plain = f.read()
        plain = self._bundle_with_agent(plain, gw.id)
        self.db.set_state(self._gw_slot_key(self._GW_BUNDLE_SSH_KEY, gw.id), " ".join(admin_ips))
        self.db.set_state(self._gw_slot_key(self._GW_BUNDLE_ISSUED_KEY, gw.id),
                          timeutil.to_iso(timeutil.now()))
        return plain

    def gw_bundle_target(self, slot_id: Optional[int] = None) -> tuple[str, dict]:
        """Кому уходит файл: «имя» (подпись) слота, сырое, и бот шлюза из кэша
        getMe ({'username', 'name'} или пусто) — для подписи под файлом."""
        gw = self._gw_slot(slot_id)
        return self._gw_display(gw), (self.gw_bot_identity(gw.id) if gw.id else {})

    def gw_bundle_plain(self, slot_id: Optional[int] = None) -> tuple[bytes, str]:
        """Открытый бандл — для ПЕРВОГО применения на машине, у которой ключа
        линка ещё нет (новая машина или новые ключи). Внутри приватные ключи:
        тот же уровень доверия, что у ссылок vpn:// с ключами устройств.

        Везёт с собой ПОСТАВКУ: шлюз стоит в России, GitHub там без туннеля
        недоступен, а туннель как раз и ставится этим файлом — качать агента
        с шлюза неоткуда (наступили на чистой машине 19.09.2026)."""
        gw = self._gw_slot(slot_id)
        return self._bundle_with_dist(self._gw_bundle_build(gw)), self.bundle_name(gw)

    _DIST_BEGIN = b"#__AWG_BOT_TGZ_BELOW__\n"
    _DIST_END = b"#__AWG_BOT_TGZ_END__\n"

    def _bundle_with_dist(self, plain: bytes) -> bytes:
        """Поставка base64-блоком между маркерами — ПОСЛЕ `exit` тела бандла и
        ДО маркера скрипта обвязки: при запуске не исполняется, в скрипт
        обвязки не попадает. Режим `--install` бандла вырезает её и передаёт
        управление установщику из неё."""
        import base64
        from awgbot.util import dist
        m = self._MAIL_MARK_LINE.search(plain)
        if m is None:
            return plain
        try:
            blob = dist.archive()
        except dist.DistError as e:
            raise ServiceError(str(e))
        body = base64.encodebytes(blob)                 # строками по 76 символов
        return plain[:m.start()] + self._DIST_BEGIN + body + self._DIST_END + plain[m.start():]
