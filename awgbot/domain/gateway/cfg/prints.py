"""prints.py — отпечатки того, что стоит на шлюзе: разделы документа для снимка канала."""
from __future__ import annotations

import ipaddress

from awgbot.core import config
from awgbot.domain import gwcfg
from awgbot.util import wgconf

from ..base import pathlib_read


class CfgPrintsMixin:
    _CFG_GEN_KEY = "gwcfg_gen"               # поколение последнего применённого документа
    _CFG_HASH_KEY = "gwcfg_hash"             # его отпечаток

    # ── что стоит ────────────────────────────────────────────────────────────

    def cfg_env_current(self) -> dict:
        """Раздел env по юниту обвязки. Юнит — то, с чем
        скрипт стартует при каждой загрузке; память агента о применении тут ни
        при чём."""
        from awgbot.infra import gwguard
        return {k: " ".join(gwguard.unit_env(k).split()) for k in gwcfg.ENV_KEYS}

    def cfg_link_text(self) -> str:
        try:
            return pathlib_read(config.GW_LINK_CONF)
        except OSError:
            return ""

    def cfg_link_conf(self):
        """Разобранный установленный конфиг линка или None (нет файла, не разбирается)."""
        text = self.cfg_link_text()
        if not text:
            return None
        try:
            return wgconf.parse(text)
        except wgconf.WgConfError:
            return None

    def cfg_link_address(self) -> str:
        """Адрес линка шлюзовой стороны из установленного конфига («10.99.99.2/30»)."""
        conf = self.cfg_link_conf()
        return conf.get("Interface", "Address") if conf is not None else ""

    def cfg_link_cidr(self) -> str:
        """/30 линка по установленному конфигу."""
        addr = self.cfg_link_address()
        try:
            return str(ipaddress.ip_interface(addr).network) if addr else ""
        except ValueError:
            return ""

    def cfg_link_fixed(self) -> dict:
        """Ключи, PSK и обфускация стоящего линка — то, что документ менять не
        вправе (смена ключей — только переходом). Набор — из белого списка
        линка (gw-wg-allow.list): всё [Interface], кроме Address, и ключи [Peer]."""
        conf = self.cfg_link_conf()
        if conf is None:
            return {}
        keys = wgconf.allow().keys["link"]
        fixed = {("Interface", k) for k in keys["Interface"] if k != "Address"}
        fixed |= {("Peer", k) for k in ("PublicKey", "PresharedKey") if k in keys["Peer"]}
        out = {}
        for section, items in conf.sections:
            for key, val in items:
                if (section, key) in fixed:
                    out[key] = val
        return out

    def cfg_mail_print(self) -> str:
        acc = self.email_account()
        if acc is None:
            return ""
        return gwcfg.mail_print({"login": acc.login, "password": acc.password, "imap_host": acc.imap_host,
                                 "imap_port": acc.imap_port, "smtp_host": acc.smtp_host, "smtp_port": acc.smtp_port})

    def cfg_fingerprints(self) -> dict:
        """Поле `cfg` снимка: поколение последнего применённого документа и
        отпечатки разделов по стоящему. Всё из локального — инвариант снимка."""
        return {
            "gen": int(self.db.get_state(self._CFG_GEN_KEY) or 0),
            "env": gwcfg.env_print(self.cfg_env_current()),
            "link": gwcfg.link_print(self.cfg_link_text()),
            "uplink": self.cfg_uplink_print(cached=True),   # без команд: снимок только из локального
            "mail": self.cfg_mail_print(),
            "token": self.cfg_token_print(),              # отпечаток, не сам токен
            "server": gwcfg.server_print(self.db.get_state(self._SERVER_NAME_KEY) or ""),
            "reserve": self.reserve_print() if hasattr(self, "reserve_print") else "",
            "quorum": self.quorum_print() if hasattr(self, "quorum_print") else "",
        }
