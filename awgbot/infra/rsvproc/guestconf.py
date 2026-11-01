"""guestconf.py — часть пакета rsvproc (см. __init__): app.yaml гостя (разделы
основы + ключи гостя + свои неизменяемые), самоподписанный сертификат вебхука
на адрес резерва, пул адресов Telegram файервола хоста."""
from __future__ import annotations

import os
import subprocess

from awgbot.infra.nftguard.common import DEFAULT_TG_POOL   # тот же список, что у файервола хоста
from awgbot.infra.rsvproc.status import OpenError


def _yaml_dump(obj) -> str:
    import yaml
    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _guest_app_yaml(bot_conf: dict, app_guest: dict, iface: str, tz: str, extra: list | None = None) -> str:
    """app.yaml гостя: разделы основы из белого списка + ключи для гостя + свои
    неизменяемые; второй интерфейс основы (окно переезда профилей) — как
    интерфейс переезда гостя, чтобы бот вёл устройства на обоих."""
    from awgbot.infra import rsvzone
    app = dict(bot_conf.get("app") or {})
    for sec, vals in (app_guest or {}).items():
        if sec == "reserve":                              # цифры кворума основы — в reserve.yaml гостя (rsvopen)
            continue
        if isinstance(vals, dict):
            app.setdefault(sec, {})
            if isinstance(app[sec], dict):
                app[sec].update(vals)
    app["guest"] = True
    app["role"] = "client"
    app.setdefault("awg", {})
    app["awg"].update({"interface": iface, "conf_dir": str(rsvzone.RUN_DIR / "awg")})
    for x in (extra or [])[:1]:
        prefix = ".".join(str(x.get("addr") or "").split("/")[0].split(".")[:3])
        if x.get("name") and prefix:
            app["awg"].update({"migration_interface": str(x["name"]), "migration_subnet_prefix": prefix})
    app.setdefault("routing", {})
    if isinstance(app["routing"], dict):
        app["routing"].update({"dnsmasq_conf": str(rsvzone.RUN_DIR / "dnsmasq-rf.conf"),
                               "dnsmasq_service": "awg-bot-guest-dns.service"})
    if tz:
        app["timezone"] = tz
    return _yaml_dump(app)


def _make_cert(self_host: str, out_dir) -> tuple[str, str]:
    """Самоподписанный сертификат вебхука на адрес резерва (IP или имя в SAN)."""
    import ipaddress
    key_p, crt_p = os.path.join(out_dir, "webhook.key"), os.path.join(out_dir, "webhook.crt")
    host = self_host or "awg-bot"
    try:
        ipaddress.ip_address(host)
        san = f"IP:{host}"
    except ValueError:
        san = f"DNS:{host}"
    proc = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                           "-nodes", "-keyout", key_p, "-out", crt_p, "-days", "3650", "-subj", f"/CN={host}",
                           "-addext", f"subjectAltName={san}"], capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise OpenError("сертификат вебхука не создался: " + proc.stderr.decode(errors="replace").strip()[-200:])
    os.chmod(key_p, 0o600)
    return key_p, crt_p


def _host_pool() -> list[str]:
    """Пул адресов Telegram файервола хоста резерва — тот же, что в DNAT."""
    try:
        from awgbot.infra import nftguard
        return list(nftguard.tg_pool())
    except Exception:                                          # noqa: BLE001
        return list(DEFAULT_TG_POOL)
