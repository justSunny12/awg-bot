"""peernets.py — VPN-транзит слота и доступ между подсетями за шлюзами."""

from __future__ import annotations

from typing import Optional
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.domain import configgen
from awgbot.domain.services.types import ServiceError
from awgbot.util import nets


class PeerNetsMixin:
    """VPN-транзит слота и доступ между подсетями за шлюзами."""
    # ── «за шлюзом — без VPN» ───────────────
    def gateway_resolver_addr(self, gw) -> str:
        """Апстрим резолвера малины — свой резолвер ВПС из DNS устройства слота;
        пусто — резолвера нет, малина возьмёт запасной через аплинк."""
        from awgbot.infra import resolver
        dev = self.db.get_device(gw.device_id) if gw.device_id else None
        if dev is None:
            return ""
        dns1, _dns2 = configgen.dns_for(getattr(dev, "iface", "") or "")
        return dns1 if resolver.is_private(dns1) and resolver.installed() else ""

    def _lan_env(self, gw) -> dict:
        """Переменные функций A и B для скрипта обвязки: LAN_MODE, HOME_SUBNETS
        (первая подсеть даёт LAN-интерфейс и адрес резолвера), RESOLVER,
        PEER_HOME_NETS (подсети за другими шлюзами — в AllowedIPs и файервол)."""
        return {"LAN_MODE": "1" if gw.lan_mode else "0",
                "HOME_SUBNETS": " ".join(gw.home_subnets),
                "RESOLVER": self.gateway_resolver_addr(gw) if gw.lan_mode else "",
                "PEER_HOME_NETS": " ".join(self.gateway_peer_nets(gw.id))}


    # ── доступ между подсетями за шлюзами ───
    _PEER_NETS_KEY = "app.routing.peer_nets.enabled"

    def peer_nets_enabled(self) -> bool:
        return settings.get_bool(self._PEER_NETS_KEY, False)

    def gateway_peer_nets(self, slot_id: int) -> list[str]:
        """Подсети за другими шлюзами для слота: тумблер включён, у обоих слотов
        включено «за шлюзом — без VPN» (ровно оно гарантирует, что ответ найдёт
        дорогу назад), без дублей, без пересекающихся с собственными."""
        if not self.peer_nets_enabled():
            return []
        me = self.db.gateway(slot_id)
        if me is None or not me.lan_mode:
            return []
        out: list[str] = []
        for g in self.db.gateways():
            if g.id == me.id or not g.lan_mode:
                continue
            bad = set(nets.overlap(g.home_subnets, me.home_subnets))
            for n in g.home_subnets:
                if n not in bad and n not in out:
                    out.append(n)
        return out

    def gateway_peer_nets_info(self) -> dict:
        """Состояние функции B для экрана «Шлюзы»: {'enabled', 'state', …}.
        state: off | no_lan (слоты без режима без VPN) | no_nets (без подсетей) |
        overlap (пересечение) | ok (pairs — [(display, nets, display, nets)])."""
        slots = self.db.gateways()
        info: dict = {"enabled": self.peer_nets_enabled(), "state": "off", "slots": len(slots)}
        if not info["enabled"]:
            return info
        no_lan = [g for g in slots if not g.lan_mode]
        if len(slots) - len(no_lan) < 2:
            info["state"] = "no_lan"
            info["who"] = [self._gw_display(g) for g in no_lan]
            return info
        lan = [g for g in slots if g.lan_mode]
        no_nets = [g for g in lan if not g.home_subnets]
        if no_nets:
            info["state"] = "no_nets"
            info["who"] = [self._gw_display(g) for g in no_nets]
            return info
        for i, a in enumerate(lan):
            for b in lan[i + 1:]:
                ov = nets.overlap(a.home_subnets, b.home_subnets)
                if ov:
                    info["state"] = "overlap"
                    info["who"] = [self._gw_display(a), self._gw_display(b)]
                    info["nets"] = ov
                    return info
        info["state"] = "ok"
        info["pairs"] = [(self._gw_display(g), list(g.home_subnets)) for g in lan]
        info["pairs_named"] = [(self._gw_name(g), g.label, list(g.home_subnets)) for g in lan]
        return info

    def set_peer_nets(self, on: bool) -> None:
        """Тумблер функции B. Таблицу ВПС перевыставит хук настроек (форвардинг
        линк ↔ линк действует сразу); конфигурации шлюзов — перевыпуск, о нём
        напомнит снимок зависимостей."""
        settings.set_value(self._PEER_NETS_KEY, on)
        self._channel_touch()                              # PEER_HOME_NETS — шлюзам сразу

    def gateway_set_lan_mode(self, slot_id: int, on: bool) -> dict:
        """Включить/выключить «за шлюзом — без VPN» у слота. Включение требует
        локальной подсети: по ней малина находит свой адрес. Возвращает
        {'gateway', 'resolver': адрес или '' (запасной апстрим)}."""
        gw = self._gw_slot(slot_id)
        if on and not gw.home_subnets:
            raise ServiceError("сначала задай локальную подсеть шлюза: по ней шлюз находит свой адрес")
        if on and (not gw.device_id or self.db.get_device(gw.device_id) is None):
            raise ServiceError("у слота нет устройства — без аплинка режим без VPN не работает")
        self.db.gateway_update(gw.id, lan_mode=1 if on else 0)
        gw = self.db.gateway(gw.id)
        self._channel_touch()                              # LAN_MODE — шлюзу сразу
        return {"gateway": gw, "resolver": self.gateway_resolver_addr(gw) if on else ""}

    @staticmethod
    def _bundle_path(gw) -> str:
        return "/root/awg-gw-bundle.sh" if gw.link_if == "awglink" else f"/root/awg-gw-bundle-{gw.link_if}.sh"

    @staticmethod
    def bundle_name(gw) -> str:
        """Имя файла первого применения для слота — как его кладёт скрипт."""
        return "awg-gw-bundle.sh" if gw.link_if == "awglink" else f"awg-gw-bundle-{gw.link_if}.sh"

    def _gw_bundle_build(self, gw, with_mail: bool = True) -> tuple[bytes, str]:
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
        if with_mail:
            # почту и фразу бэкапов везёт только шифрованный бандл: открытому
            # файлу первого применения они не нужны (--install их не читает),
            # а утечка файла отдавала бы ящик и ключ ко всем копиям сервера
            plain = self._bundle_with_mail(plain)
        plain = self._bundle_with_agent(plain, gw.id)
        self.db.set_state(self._gw_slot_key(self._GW_BUNDLE_SSH_KEY, gw.id), " ".join(admin_ips))
        self.db.set_state(self._gw_slot_key(self._GW_BUNDLE_SSH_NOTIFIED_KEY, gw.id), "")
        self.db.set_state(self._gw_slot_key(self._GW_BUNDLE_DEPS_KEY, gw.id), self._gw_bundle_deps(gw))
        self.db.set_state(self._gw_slot_key(self._GW_BUNDLE_DEPS_NOTIFIED_KEY, gw.id), "")
        self.db.set_state(self._gw_slot_key(self._GW_BUNDLE_ISSUED_KEY, gw.id),
                          timeutil.to_iso(timeutil.now()))
        return plain, priv

    def gw_slot_id(self, slot_id: Optional[int] = None) -> int:
        """Номер слота, для которого выпускается файл (без номера — первый; 0 —
        слотов нет, файл на заглушке)."""
        return int(getattr(self._gw_slot(slot_id), "id", 0) or 0)

    def gw_bundle_target(self, slot_id: Optional[int] = None) -> tuple[str, dict]:
        """Кому уходит файл: «имя» (подпись) слота, сырое, и бот шлюза из кэша
        getMe ({'username', 'name'} или пусто) — для подписи под файлом."""
        gw = self._gw_slot(slot_id)
        return self._gw_display(gw), (self.gw_bot_identity(gw.id) if gw.id else {})

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
        тот же уровень доверия, что у ссылок vpn:// с ключами устройств.

        Везёт с собой ПОСТАВКУ: шлюз стоит в России, GitHub там без туннеля
        недоступен, а туннель как раз и ставится этим файлом — качать агента
        с шлюза неоткуда (наступили на чистой машине 19.09.2026). Шифрованный
        бандл для чата поставку не везёт: у той машины агент уже стоит."""
        gw = self._gw_slot(slot_id)
        plain, _ = self._gw_bundle_build(gw, with_mail=False)
        return self._bundle_with_dist(plain), self.bundle_name(gw)

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
