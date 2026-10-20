#!/usr/bin/env python3
"""
tools/rsvopen.py — процесс открытия: пакет переезда основы → гостевая зона
резерва с её ботом и клоном VPN. Единственный код на резерве, который видит
`M`, `K` и части пакета; бот резерва его только запускает (временным юнитом
с песочницей, см. awg-bot.sh reserve open) и читает ход по
/run/awg-bot/rsv/open.status (шаг, время, причина — без секретов).

    echo '<JSON>' | python -m tools.rsvopen         вход — только stdin

Вход (один из ключей ключа плюс поля зоны):
    {"shares": [[x, "hex"], [x, "hex"]]}     две доли прямой (резерв + шлюз)
    {"share": [x, "hex"], "code": "…"}       доля резерва + аварийный код
    {"k": "hex"}                             ключ пакета (переезд кнопкой, K от основы)
    "target_ip": "203.0.113.7"               адрес этого сервера для записи DNS
    "dns": {provider…}                       настройка провайдера (K-путь: конверт недоступен)
    "set_record": true|false                 менять ли запись (по умолчанию — да, если есть чем)
    "webhook_port": 443, "self_host": "…"    порт вебхука и собственный адрес резерва
    "tg_pool": ["149.154.160.0/20", …]       пул адресов Telegram для слушателя
    "channel_port": 8787                     порт канала шлюзов (пересылка в зону)

Шаги и строки хода (stdout и open.status): `m` → `k` → `parts` → `store`
(env, conf, data, ключ, вебхук) → `clone_up` (--take: клон, veth, dnsmasq,
юнит бота основы) → `record_set` → `record_fact` → `done`; отказ — `fail
<причина>`, код 1. Ждёт факта записи у авторитетного сервера до 30 минут;
по факту кладёт отметку `tg_take` — бот основы на резерве ставит вебхук.
Снимок `rep.bin` новее пакета (перезагрузка резерва во время обслуживания)
— база гостя из него, а не из пакета.

Ничего открытого наружу не возвращает: ни ключей, ни частей; память с `M` и
`K` затирается по выходу насколько это возможно в Python.
"""
from __future__ import annotations

import json
import os
import random
import secrets
import subprocess
import sys
import time

TARGET_WAIT_S = 30 * 60
FACT_POLL_S = (15, 40)
DEFAULT_TG_POOL = ["149.154.160.0/20", "91.108.4.0/22"]


def _p(text: str) -> None:
    print(text, flush=True)


class OpenError(RuntimeError):
    pass


def _step(step: str, **fields) -> None:
    from awgbot.infra import rsvzone
    rsvzone.open_status_write(step, **fields)
    _p(step)


def _fail(reason: str) -> int:
    from awgbot.infra import rsvzone
    rsvzone.open_status_write("fail", reason=reason)
    _p(f"fail {reason}")
    return 1


def _read_input() -> dict:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw or "{}")
    except ValueError as e:
        raise OpenError(f"вход не JSON: {e}") from e
    if not isinstance(data, dict):
        raise OpenError("вход не объект")
    return data


def _point(item) -> tuple[int, bytes]:
    try:
        x, y = int(item[0]), bytes.fromhex(str(item[1]))
    except (TypeError, ValueError, IndexError) as e:
        raise OpenError("доля не того вида") from e
    if not 1 <= x <= 255:
        raise OpenError("доля не того вида")
    return x, y


def recover_keys(data: dict, blob: bytes) -> tuple[bytes | None, bytes]:
    """(M или None, K) по входу: две доли, доля + код, или K."""
    from awgbot.util import rsvkeys
    from awgbot.util.rsvpkg import seal
    if data.get("k"):
        try:
            k = bytes.fromhex(str(data["k"]))
        except ValueError as e:
            raise OpenError("K не hex") from e
        if len(k) != seal.K_LEN:
            raise OpenError("K не той длины")
        return None, k
    if data.get("shares"):
        shares = list(data["shares"])
        if len(shares) < 2:
            raise OpenError("нужны две доли")
        p1, p2 = _point(shares[0]), _point(shares[1])
    elif data.get("share") and data.get("code"):
        p1 = _point(data["share"])
        try:
            p2 = rsvkeys.code_point(str(data["code"]))
        except ValueError as e:
            raise OpenError(f"аварийный код: {e}") from e
    else:
        raise OpenError("нет ни долей, ни кода, ни K")
    if p1[0] == p2[0]:
        raise OpenError("две доли одной точки")
    try:
        m = rsvkeys.recover_m(p1, p2)
    except ValueError as e:
        raise OpenError(f"доли не сошлись: {e}") from e
    try:
        k = seal.open_k(blob, m)
    except seal.PackageError as e:
        raise OpenError(f"ключ пакета: {e}") from e
    return m, k


def _db_from_snapshot(k: bytes, pkg_gen: int) -> tuple[bytes | None, str]:
    """`rep.bin` новее пакета → (его база, время снимка); нет или старее — (None, "")."""
    from awgbot.infra import rsvzone
    from awgbot.util.rsvpkg import seal
    try:
        blob = rsvzone.REP_PATH.read_bytes()
    except OSError:
        return None, ""
    try:
        snap = seal.snapshot_open(k, blob)
    except seal.PackageError:
        return None, ""
    if not isinstance(snap, dict) or int(snap.get("pkg_gen") or 0) < int(pkg_gen):
        return None, ""
    try:
        return bytes.fromhex(str(snap.get("db") or "")), str(snap.get("at") or "")
    except ValueError:
        return None, ""


def _yaml_dump(obj) -> str:
    import yaml
    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _guest_app_yaml(bot_conf: dict, app_guest: dict, iface: str, tz: str) -> str:
    """app.yaml гостя: разделы основы из белого списка + ключи для гостя + свои неизменяемые."""
    from awgbot.infra import rsvzone
    app = dict(bot_conf.get("app") or {})
    for sec, vals in (app_guest or {}).items():
        if isinstance(vals, dict):
            app.setdefault(sec, {})
            if isinstance(app[sec], dict):
                app[sec].update(vals)
    app["guest"] = True
    app["role"] = "client"
    app.setdefault("awg", {})
    app["awg"].update({"interface": iface, "conf_dir": str(rsvzone.RUN_DIR / "awg")})
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


def fill_store(data: dict, k: bytes, parts: dict, db_bytes: bytes, iface: str) -> None:
    from awgbot.infra import rsvzone
    bot = parts.get("bot") or {}
    env = bot.get("env") or {}
    token, admin_id = str(env.get("BOT_TOKEN") or ""), int(env.get("ADMIN_ID") or 0)
    if not token or not admin_id:
        raise OpenError("в части бота нет токена или ADMIN_ID")
    run = rsvzone.RUN_DIR
    rsvzone.write_private(run / "env", f"BOT_TOKEN={token}\nADMIN_ID={admin_id}\n")
    conf = bot.get("conf") or {}
    for name, vals in conf.items():
        if name == "app" or not isinstance(vals, dict):
            continue
        rsvzone.write_private(run / "conf" / f"{name}.yaml", _yaml_dump(vals))
    tz = str((parts.get("vpn") or {}).get("tz") or "")
    rsvzone.write_private(run / "conf" / "app.yaml", _guest_app_yaml(conf, bot.get("app_guest") or {}, iface, tz))
    rsvzone.write_private(run / "data" / "bot.db", db_bytes)
    for kind, lines in (bot.get("lists") or {}).items():          # кэш списков РФ-доступа — гостю
        if isinstance(kind, str) and kind.replace("_", "").isalnum() and isinstance(lines, list):
            rsvzone.write_private(run / "data" / f"routing-{kind}.lst", "\n".join(str(x) for x in lines) + "\n")
    rsvzone.write_private(rsvzone.K_FILE, k.hex())
    from awgbot.util import timeutil
    rsvzone.write_private(run / "pkg.json", json.dumps({"pkg_gen": int(data.get("_pkg_gen") or 0),
                                                        "opened_at": timeutil.now_iso(),
                                                        "db_at": str(data.get("_db_at") or "")}))
    key_p, crt_p = _make_cert(str(data.get("self_host") or ""), str(run))
    webhook = {"port": int(data.get("webhook_port") or 0), "path": secrets.token_urlsafe(24),
               "secret": secrets.token_urlsafe(32), "key": key_p, "cert": crt_p,
               "host": str(data.get("self_host") or "")}
    rsvzone.write_private(rsvzone.WEBHOOK_FILE, json.dumps(webhook))
    pool = [str(p) for p in (data.get("tg_pool") or _host_pool())]
    rsvzone.write_private(rsvzone.TG_POOL_FILE, json.dumps({"pool": pool}))
    try:
        rsvzone.TG_TAKE_FILE.unlink()
    except OSError:
        pass


def _host_pool() -> list[str]:
    """Пул адресов Telegram файервола хоста резерва — тот же, что в DNAT."""
    try:
        from awgbot.infra import nftguard
        return list(nftguard.tg_pool())
    except Exception:                                          # noqa: BLE001
        return list(DEFAULT_TG_POOL)


def set_record(dns: dict, target_ip: str) -> None:
    from awgbot.util import dnsapi
    prov = dnsapi.provider_for(dns)
    prov.set(dns, target_ip)


def wait_fact(name: str, target_ip: str, budget_s: float = TARGET_WAIT_S) -> bool:
    from awgbot.util import dnsapi
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        try:
            if target_ip in dnsapi.read_authoritative(name):
                return True
        except dnsapi.DnsError:
            pass
        time.sleep(random.uniform(*FACT_POLL_S))
    return False


def run(data: dict) -> int:
    from awgbot.core import config
    from awgbot.infra import rsvzone
    from awgbot.util.rsvpkg import seal, vpn as pkgvpn
    pkg_path = os.path.join(str(config.DATA_DIR), "reserve", "guest.pkg")
    try:
        blob = open(pkg_path, "rb").read()
    except OSError as e:
        return _fail(f"пакета нет: {e}")
    try:
        head = seal.header(blob)
    except seal.PackageError as e:
        return _fail(f"пакет: {e}")
    try:
        m, k = recover_keys(data, blob)
        _step("m" if m else "k_given", pkg_gen=head.pkg_gen, m_gen=head.m_gen)
        if m:
            _step("k", pkg_gen=head.pkg_gen)
        parts = seal.open_parts(blob, k)
        vpn = pkgvpn.check(parts.get("vpn"))
        if not isinstance(parts.get("bot"), dict):
            raise OpenError("в пакете нет части бота")
        _step("parts", ifaces=",".join(i["name"] for i in vpn["ifaces"]))
        dns = dict(data.get("dns") or {})
        if m and not dns:
            dns = seal.open_dns(blob, m) or {}
    except (OpenError, seal.PackageError, pkgvpn.VpnPartError) as e:
        return _fail(str(e))
    iface = vpn["ifaces"][0]
    db_bytes, db_at = _db_from_snapshot(k, head.pkg_gen)
    from_snapshot = db_bytes is not None
    data["_db_at"] = db_at
    if db_bytes is None:
        try:
            db_bytes = bytes.fromhex(str(parts["bot"].get("db") or ""))
        except ValueError:
            return _fail("реплика в пакете не hex")
    if not db_bytes:
        return _fail("реплика пуста")
    try:
        rsvzone.store()
        data["_pkg_gen"] = head.pkg_gen
        fill_store(data, k, parts, db_bytes, iface["name"])
        _step("store", from_snapshot=int(from_snapshot))
        rsvzone.take(pkgvpn.clone_conf(iface), iface=iface["name"], addr=iface["addr"], dns=list(vpn.get("dns") or []),
                     upstreams=list(vpn.get("upstreams") or []), admin=list(vpn.get("admin") or []),
                     channel_port=int(data.get("channel_port") or 0))
        _step("clone_up", iface=iface["name"], port=iface["port"])
    except (rsvzone.ZoneError, OpenError, OSError) as e:
        return _fail(f"зона: {e}")
    finally:
        del parts, blob
    target = str(data.get("target_ip") or "")
    want_record = bool(data.get("set_record", True)) and bool(target) and bool(dns.get("provider"))
    if not want_record:
        _step("done", record="skipped")
        if data.get("tg_take"):
            rsvzone.write_private(rsvzone.TG_TAKE_FILE, "1")
        return 0
    try:
        set_record(dns, target)
    except Exception as e:                                     # noqa: BLE001 — DnsError, NotSupported, сеть
        return _fail(f"запись DNS: {e}")
    _step("record_set", name=str(dns.get("name") or ""), ip=target)
    if not wait_fact(str(dns.get("name") or ""), target):
        return _fail("факт записи не подтвердился за 30 минут")
    _step("record_fact", name=str(dns.get("name") or ""), ip=target)
    rsvzone.write_private(rsvzone.TG_TAKE_FILE, "1")
    _step("done")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        data = _read_input()
    except OpenError as e:
        return _fail(str(e))
    try:
        return run(data)
    except Exception as e:                                     # noqa: BLE001 — любой сбой: отказ строкой, не трассой
        return _fail(f"{type(e).__name__}: {str(e)[:160]}")


if __name__ == "__main__":
    sys.exit(main())
