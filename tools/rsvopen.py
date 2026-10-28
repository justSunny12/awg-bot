#!/usr/bin/env python3
"""
tools/rsvopen.py — процесс открытия: пакет переезда основы → гостевая зона
резерва с её ботом и клоном VPN. Единственный код на резерве, который видит
`M`, `K` и части пакета; бот резерва его только запускает (временным юнитом
awg-bot-rsvopen с песочницей — свойства в одном месте,
domain/services/reserve/openproc.py `rsvopen_sandbox_props`: MemoryMax=192M
без swap и дампов, PrivateTmp, ProtectHome, ProtectSystem=strict,
NoNewPrivileges, запись — только хранилище гостя, /var/lib/awg-bot-guest,
/run/awg-bot/rsv, /run/netns и /etc/systemd; каталог данных резерва подменён
пустым (TemporaryFileSystem), виден только reserve/guest.pkg
(BindReadOnlyPaths); /etc/awg-bot/env недоступен, в окружении нет
BOT_TOKEN/ADMIN_ID резерва, AWG_BOT_ENV=/dev/null). Хранилище гостя бот
монтирует сам до запуска юнита (в пространстве хоста: смонтированное внутри
песочницы хосту и юнитам гостя не видно) и создаёт каталоги записи; шаг
`store` здесь монтирует его, только если этого ещё нет (ручной запуск). Ход
бот читает по /run/awg-bot/rsv/open.status (шаг, время, причина — без
секретов; до запуска бот пишет туда `launching`, после — `launched`), stderr
процесса — в /run/awg-bot/rsv/rsvopen.err: кончился без `done`/`fail` —
бот срывает открытие сразу, хвостом stderr в причине. Руками — `awg-bot
reserve open` (root, без этой песочницы).

    echo '<JSON>' | python -m tools.rsvopen         вход — только stdin

Вход (один из ключей ключа плюс поля зоны):
    {"shares": [[x, "hex"], [x, "hex"]]}     две доли прямой (резерв + шлюз)
    {"share": [x, "hex"], "code": "…"}       доля резерва + аварийный код
    {"k": "hex"}                             ключ пакета (ручной запуск: K открытым)
    {"await_k": true}                        переезд кнопкой: процесс выпускает разовую
                                             пару X25519, пишет её публичную часть и
                                             идентификатор открытия в open.status (шаг
                                             `eph`) и ждёт второй строки stdin
                                             {"sealed": "hex"} — конверт с K от основы
                                             (не дольше K_WAIT_S); закрытая часть живёт
                                             только в памяти процесса
    "target_ip": "203.0.113.7"               адрес этого сервера для записи DNS
    "dns": {provider…}                       настройка провайдера (K-путь: конверт недоступен)
    "set_record": true|false                 менять ли запись (по умолчанию — да, если есть чем)
    "webhook_port": 443, "self_host": "…"    порт вебхука и собственный адрес резерва
    "tg_pool": ["149.154.160.0/20", …]       пул адресов Telegram для слушателя (нет — пул
                                             файервола хоста: ядро и выученное)
    "channel_port": 8787                     порт канала шлюзов (пересылка в зону)

Шаги и строки хода (stdout и open.status): `m` → `k` → `parts` → `store`
(env, conf, data — база и кэш списков РФ-доступа `routing-<вид>.lst` из части
бота: источники списков гостю недоступны, — ключ, вебхук, пул) → `clone_up`
(--take: клон, veth, dnsmasq, юнит бота основы) → `record_set` → `record_fact` → `done`; отказ — `fail
<причина>`, код 1. При готовом `k` вместо `m` → `k` — шаг `k_given`; по конверту — `eph` → `k_sealed`. Ждёт
факта записи у авторитетного сервера до 30 минут; по факту кладёт отметку
`tg_take` — бот основы на резерве ставит вебхук. Без записи (`set_record`
false, нет `target_ip` или провайдера — переезд кнопкой и по согласию основы:
запись меняет она) — `done record=skipped` после `clone_up`, отметку тогда
ставит поле `tg_take: true` во входе (иначе её даёт бот резерва по локальному
каналу). Клоны — всех интерфейсов пакета: первый (основной) идёт конфигом
в stdin скрипта зоны, конфиги остальных (интерфейс окна переезда профилей)
процесс кладёт в хранилище (`awg/<имя>.conf`) после шага `store` и называет
скрипту в GUEST_EXTRA («имя=cidr …»); `clone_up` пишет их список в поле
`ifaces`. В app.yaml гостя второй интерфейс — `awg.migration_interface` с
`migration_subnet_prefix` из его адреса: бот основы на резерве ведёт
устройства на обоих.
Снимок `rep.bin` новее пакета (перезагрузка резерва во время обслуживания)
— база гостя из него, а не из пакета. Пакет-снимок журнала (`pkg3`, часть
`jrn`) — база гостя по журналу (util/rsvjrn/opener): снимок + события основы
из `reserve/jrn/` (виден только на чтение) + журнал данных гостя + пачки
счётчиков, строка эпохи (переезд кнопкой — гость следует за основой до
передачи бота; аварийный — держатель); `rep.bin` на этом пути не читается. Наборы настроек из части бота ложатся в
хранилище только с именами из белого списка пакета (util/rsvpkg/bot.py
CONF_FILES): имя из пакета второй стороны путём не становится. `M` и
настройка провайдера DNS (с токеном) отбрасываются, как только сделали своё;
TTL записи — в `pkg.json` хранилища (гость держит по нему паузу перед
«переподключи VPN»): из конверта DNS, а на пути с `k` — из поля `dns.ttl`
входа (бот резерва кладёт туда `ttl` из `go` основы).

Токен бота основы из части бота — одна строка без пробелов, не длиннее 128
знаков (он ложится строкой в env юнита гостя); иначе отказ «токен бота в
пакете не того вида».

Ничего открытого наружу не возвращает: ни ключей, ни частей; память с `M` и
`K` затирается по выходу насколько это возможно в Python.
"""
from __future__ import annotations

import json
import os
import random
import secrets
import select
import subprocess
import sys
import time

from awgbot.infra.nftguard.common import DEFAULT_TG_POOL   # тот же список, что у файервола хоста  # noqa: E402

TARGET_WAIT_S = 30 * 60
FACT_POLL_S = (15, 40)
K_WAIT_S = 120                  # конверт с K от основы после публикации разового ключа


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
    """Первая строка stdin — вход. Не разобралась как строка (JSON в несколько
    строк при ручном запуске) — дочитать до конца и разобрать целиком."""
    raw = sys.stdin.readline()
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raw += sys.stdin.read()
        try:
            data = json.loads(raw or "{}")
        except ValueError as e:
            raise OpenError(f"вход не JSON: {e}") from e
    if not isinstance(data, dict):
        raise OpenError("вход не объект")
    return data


def _read_line_within(budget_s: float) -> str:
    """Строка из stdin не позже срока; пусто — EOF (бот закрыл трубу) или срок."""
    deadline = time.monotonic() + budget_s
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return ""
        ready, _w, _x = select.select([sys.stdin], [], [], min(left, 5.0))
        if ready:
            return sys.stdin.readline()


def await_sealed_k(pkg_gen: int, budget_s: float = K_WAIT_S) -> bytes:
    """Переезд кнопкой: разовая пара X25519 → публичная часть и идентификатор
    открытия в open.status (шаг `eph`; бот резерва отдаёт их основе) → вторая
    строка stdin {"sealed": hex} → `K`. Закрытая часть — только здесь, в
    памяти; конверт с чужим поколением или идентификатором не открывается."""
    from awgbot.util.rsvpkg import seal
    priv, pub = seal.new_eph()
    oid = secrets.token_hex(seal.OID_LEN // 2)
    try:
        _step("eph", pkg_gen=pkg_gen, eph=pub.hex(), oid=oid)
        line = _read_line_within(budget_s)
        if not line:
            raise OpenError("ключ пакета от основы не пришёл за 2 минуты")
        try:
            obj = json.loads(line)
            sealed = bytes.fromhex(str((obj or {}).get("sealed") or ""))
        except (ValueError, AttributeError) as e:
            raise OpenError("ключ пакета не того вида") from e
        if not sealed:
            raise OpenError("ключ пакета пуст")
        try:
            return seal.open_sealed_k(priv, sealed, pkg_gen, oid)
        except seal.PackageError as e:
            raise OpenError(str(e)) from e
    finally:
        priv = None                                            # noqa: F841 — закрытая часть дальше не нужна


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
            raise OpenError("ключ пакета не hex") from e
        if len(k) != seal.K_LEN:
            raise OpenError("ключ пакета не той длины")
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
        raise OpenError("нет ни долей, ни кода, ни ключа пакета")
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


def _journal(k: bytes, jrn: dict, *, button: bool) -> dict:
    """База гостя по журналу: снимок уже в хранилище — события, счётчики, эпоха."""
    from awgbot.core import config
    from awgbot.infra import rsvzone
    from awgbot.util.rsvjrn import event, opener
    try:
        res = opener.build(rsvzone.RUN_DIR / "data" / "bot.db", k, jrn, os.path.join(str(config.DATA_DIR), "reserve", "jrn"),
                           rsvzone.VAR_DIR / "jrn", button=button)
    except (event.RecordError, OSError) as e:
        raise OpenError(f"журнал не применён: {e}") from e
    except Exception as e:                                     # noqa: BLE001 — sqlite3.Error и прочее
        raise OpenError(f"журнал не применён: {type(e).__name__}: {str(e)[:120]}") from e
    _step("journal", epoch=res.epoch, role=res.role, fork=res.fork, events=res.events, guest_events=res.guest_events,
          counts=res.counts, gap=res.gap, lost=len(res.lost))
    return {"jgap": int(res.gap_n or 0), "jlost": len(res.lost)}   # дальше по шагам — боту резерва (разрыв — не молча)


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


def fill_store(data: dict, k: bytes, parts: dict, db_bytes: bytes, iface: str, extra: list | None = None) -> None:
    from awgbot.infra import rsvzone
    from awgbot.util.rsvpkg import bot as pkgbot
    bot = parts.get("bot") or {}
    env = bot.get("env") or {}
    token, admin_id = str(env.get("BOT_TOKEN") or ""), int(env.get("ADMIN_ID") or 0)
    if not token or not admin_id:
        raise OpenError("в части бота нет токена или ADMIN_ID")
    if any(ch.isspace() for ch in token) or len(token) > 128:
        raise OpenError("токен бота в пакете не того вида")     # файл env юнита — только одна строка токена
    run = rsvzone.RUN_DIR
    rsvzone.write_private(run / "env", f"BOT_TOKEN={token}\nADMIN_ID={admin_id}\n")
    conf = bot.get("conf") or {}
    for name, vals in conf.items():
        # только известные имена наборов (pkgbot.CONF_FILES): имя из пакета второй
        # стороны — не путь, запись не выходит за хранилище
        if name == "app" or not isinstance(vals, dict) or name not in pkgbot.CONF_FILES:
            continue
        rsvzone.write_private(run / "conf" / f"{name}.yaml", _yaml_dump(vals))
    tz = str((parts.get("vpn") or {}).get("tz") or "")
    rsvzone.write_private(run / "conf" / "app.yaml", _guest_app_yaml(conf, bot.get("app_guest") or {}, iface, tz, extra or []))
    rsvzone.write_private(run / "data" / "bot.db", db_bytes)
    for kind, lines in (bot.get("lists") or {}).items():          # кэш списков РФ-доступа — гостю
        if isinstance(kind, str) and kind.replace("_", "").isalnum() and isinstance(lines, list):
            rsvzone.write_private(run / "data" / f"routing-{kind}.lst", "\n".join(str(x) for x in lines) + "\n")
    rsvzone.write_private(rsvzone.K_FILE, k.hex())
    from awgbot.util import timeutil
    rsvzone.write_private(run / "pkg.json", json.dumps({"pkg_gen": int(data.get("_pkg_gen") or 0),
                                                        "opened_at": timeutil.now_iso(),
                                                        "db_at": str(data.get("_db_at") or ""),
                                                        "ttl": int(data.get("_ttl") or 0)}))
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
    sealed_path = bool(data.get("await_k"))
    if sealed_path:
        try:
            data["k"] = await_sealed_k(head.pkg_gen).hex()
        except OpenError as e:
            return _fail(str(e))
    try:
        m, k = recover_keys(data, blob)
        _step("m" if m else ("k_sealed" if sealed_path else "k_given"), pkg_gen=head.pkg_gen, m_gen=head.m_gen)
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
        by_m = bool(m)                                        # по долям или коду: запись меняю я — без провайдера нельзя
        m = None                                              # M нужен был только для K и конверта DNS
        data["_ttl"] = int((dns or {}).get("ttl") or 0)      # TTL записи — гостю: пауза перед «переподключи VPN»
    except (OpenError, seal.PackageError, pkgvpn.VpnPartError) as e:
        return _fail(str(e))
    iface = vpn["ifaces"][0]
    extra = [i for i in vpn["ifaces"][1:] if i.get("name") and i.get("addr")]   # окно переезда профилей
    jrn = parts.get("jrn") if isinstance(parts.get("jrn"), dict) else None
    db_bytes, db_at = _db_from_snapshot(k, head.pkg_gen) if jrn is None else (None, "")
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
        fill_store(data, k, parts, db_bytes, iface["name"], extra)
        jinfo = _journal(k, jrn, button=sealed_path) if jrn is not None else {}
        _step("store", from_snapshot=int(from_snapshot))
        for x in extra:                                       # конфиги вторых интерфейсов — в хранилище, скрипт поднимет их
            rsvzone.write_private(rsvzone.RUN_DIR / "awg" / f"{x['name']}.conf", pkgvpn.clone_conf(x))
        rsvzone.take(pkgvpn.clone_conf(iface), iface=iface["name"], addr=iface["addr"], dns=list(vpn.get("dns") or []),
                     upstreams=list(vpn.get("upstreams") or []), admin=list(vpn.get("admin") or []),
                     channel_port=int(data.get("channel_port") or 0), extra=[(x["name"], x["addr"]) for x in extra])
        _step("clone_up", iface=iface["name"], port=iface["port"], ifaces=",".join([iface["name"]] + [x["name"] for x in extra]), **jinfo)
    except (rsvzone.ZoneError, OpenError, OSError) as e:
        return _fail(f"зона: {e}")
    finally:
        del parts, blob
    target = str(data.get("target_ip") or "")
    if by_m and bool(data.get("set_record", True)) and target and not dns.get("provider"):
        return _fail("запись DNS: провайдер «вручную» — аварийный переезд невозможен")
    want_record = bool(data.get("set_record", True)) and bool(target) and bool(dns.get("provider"))
    if not want_record:
        _step("done", record="skipped", **jinfo)
        if data.get("tg_take"):
            rsvzone.write_private(rsvzone.TG_TAKE_FILE, "1")
        return 0
    try:
        set_record(dns, target)
    except Exception as e:                                     # noqa: BLE001 — DnsError, NotSupported, сеть
        return _fail(f"запись DNS: {e}")
    name = str(dns.get("name") or "")
    dns = {}                                                   # токен провайдера дальше не нужен
    _step("record_set", name=name, ip=target)
    if not wait_fact(name, target):
        return _fail("факт записи не подтвердился за 30 минут")
    _step("record_fact", name=name, ip=target)
    rsvzone.write_private(rsvzone.TG_TAKE_FILE, "1")
    _step("done", **jinfo)
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
