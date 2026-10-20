"""files.py — часть пакета merge (см. __init__): файлы распакованного бэкапа
после слияния — app.yaml, env, конфиги awg-интерфейсов и выданные шлюзовые
стороны линков (root/gw-<линк>.conf).

Текущим остаётся то, без чего линк и канал не поднимутся или что уже роздано
устройствам и шлюзам: на той же линии — сетевые параметры сервера и все
конфиги интерфейсов; на любой линии — факты хоста (адрес сервера, способ
запуска AWG, файервол, SSH), токен бота и ADMIN_ID, файлы текущих слотов.
"""
from __future__ import annotations

import os
import shutil

import yaml

from .. import plan as _plan

# app.yaml: факты хоста — текущие всегда
CONF_ALWAYS_CURRENT = (("network", "server_host"), ("network", "ssh_port"), ("role",), ("firewall",),
                       ("awg", "interface"), ("awg", "conf_dir"))
# app.yaml: роздано в конфиги устройств, аплинков и юниты шлюзов — текущее на
# той же линии; на другой линии (новый сервер) — из бэкапа
CONF_SAME_LINE = (("network", "subnet_prefix"), ("network", "subnet_cidr"), ("network", "server_port"),
                  ("network", "ip_host_start"), ("network", "ip_host_end"),
                  ("routing", "lan_space"), ("routing", "link_channel_port"),
                  # обвязка развёрнута на этом хосте (линк до шлюзов): копия до
                  # развёртывания усыпила бы маршрутизацию при живых шлюзах
                  ("routing", "gw_interface"),
                  # DNS в выданных конфигах устройств и устройств админа
                  ("client_config", "dns1"), ("client_config", "dns2"), ("client_config", "admin_dns"),
                  ("awg", "protocol_id"), ("awg", "migration_interface"),
                  ("awg", "migration_subnet_prefix"), ("awg", "migration_dns"))

ENV_ALWAYS_CURRENT = ("BOT_TOKEN", "ADMIN_ID")
GW_TOKEN = "GW_BOT_TOKEN"


def _get(doc, path):
    for seg in path:
        if not isinstance(doc, dict) or seg not in doc:
            return False, None
        doc = doc[seg]
    return True, doc


def merge_app_yaml(copy_dir: str, conf_dir: str, same_line: bool) -> None:
    """Ключи текущего app.yaml поверх app.yaml бэкапа (ruamel: комментарии
    бэкапа целы). Ключа нет в текущем — снимается и в итоге."""
    from ruamel.yaml import YAML
    dst = os.path.join(copy_dir, "state", "conf", "app.yaml")
    if not os.path.exists(dst):
        return
    try:
        with open(os.path.join(conf_dir, "app.yaml"), encoding="utf-8") as f:
            cur = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        cur = {}
    rt = YAML()
    rt.preserve_quotes = True
    with open(dst, encoding="utf-8") as f:
        doc = rt.load(f) or {}
    for path in CONF_ALWAYS_CURRENT + (CONF_SAME_LINE if same_line else ()):
        have, val = _get(cur, path)
        node = doc
        for seg in path[:-1]:
            if seg not in node or not isinstance(node[seg], dict):
                if not have:
                    node = None
                    break
                node[seg] = {}
            node = node[seg]
        if node is None:
            continue
        if have:
            node[path[-1]] = val
        elif path[-1] in node:
            del node[path[-1]]
    with open(dst, "w", encoding="utf-8") as f:
        rt.dump(doc, f)


def _env_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _env_key(line: str) -> str:
    return line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""


def gw_token_env(slot: int) -> str:
    return GW_TOKEN if int(slot) == 1 else f"{GW_TOKEN}_{int(slot)}"


def merge_env(copy_dir: str, env_path: str, p) -> None:
    """Та же линия — текущий env целиком. Другая — env бэкапа: токен бота и
    ADMIN_ID текущие; токены ботов шлюзов — текущие у текущих слотов, из
    бэкапа — у слотов, вставших из бэкапа, снятых после бэкапа — прочь."""
    dst = os.path.join(copy_dir, "state", "env")
    cur = _env_lines(env_path)
    if p.same_line:
        if cur:
            with open(dst, "w", encoding="utf-8") as f:
                f.write("\n".join(cur) + "\n")
        return
    cur_vals = {_env_key(x): x for x in cur if _env_key(x)}
    keep_current = set(ENV_ALWAYS_CURRENT) | {gw_token_env(n) for n in p.current_slots()}
    drop = {gw_token_env(n) for n in p.removed_slots()}
    out, seen = [], set()
    for line in _env_lines(dst):
        k = _env_key(line)
        if k in drop:
            continue
        if k in keep_current:
            seen.add(k)
            if k in cur_vals:
                out.append(cur_vals[k])
            continue
        out.append(line)
    for k in sorted(keep_current - seen):
        if k in cur_vals:
            out.append(cur_vals[k])
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(dst, 0o600)


def _put_live(src: str, dst: str) -> None:
    """Текущий файл — в распакованный бэкап; текущего нет — и в бэкапе нет."""
    if os.path.exists(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
    elif os.path.exists(dst):
        os.unlink(dst)


def merge_link_files(copy_dir: str, p, *, awg_dir: str, root_dir: str) -> None:
    """Файлы линков: у текущих слотов — текущие, у снятых после бэкапа —
    прочь, у вставших из бэкапа (пустой сервер) — из бэкапа."""
    removed, live_slots = set(p.removed_slots()), set(p.current_slots())
    for s in p.slots:
        if not s.link_if:
            continue
        if s.case == _plan.C_REMOVED and s.slot in live_slots:
            continue                                      # снят и назначен заново: файлы — текущего
        pair = ((os.path.join(awg_dir, f"{s.link_if}.conf"), os.path.join(copy_dir, "awg", f"{s.link_if}.conf")),
                (os.path.join(root_dir, f"gw-{s.link_if}.conf"),
                 os.path.join(copy_dir, "root", f"gw-{s.link_if}.conf")))
        for live, dst in pair:
            if s.slot in removed:
                if os.path.exists(dst):
                    os.unlink(dst)
            elif s.case != _plan.C_RESTORED:
                _put_live(live, dst)


def merge_iface_confs(copy_dir: str, p, *, awg_dir: str, link_ifs: set[str], client_if: str,
                      transplanted: dict[str, str], lost_pubs: set[str]) -> None:
    """Конфиги awg-интерфейсов, кроме линков. Та же линия — текущие (роздано
    устройствам). Другая — из бэкапа; пиры устройств текущих слотов —
    текущие, в конфиге своего интерфейса (двойник переезда — в интерфейсе
    переезда); пиры невставших и мёртвых устройств — прочь.
    transplanted — {ключ: интерфейс устройства ('' — клиентский)}."""
    from awgbot.infra.awg import parsers
    d = os.path.join(copy_dir, "awg")
    if not os.path.isdir(d):
        return
    by_iface: dict[str, set[str]] = {}
    for pub, iface in transplanted.items():
        by_iface.setdefault(iface or client_if, set()).add(pub)
    for name in sorted(os.listdir(d)):
        if not name.endswith(".conf") or name[:-5] in link_ifs:
            continue
        dst = os.path.join(d, name)
        live = os.path.join(awg_dir, name)
        if p.same_line:
            _put_live(live, dst)
            continue
        mine = by_iface.get(name[:-5], set())
        if not (mine or lost_pubs or transplanted):
            continue
        with open(dst, encoding="utf-8") as f:
            header, peers = parsers._split_conf(f.read())
        live_peers = {}
        try:
            with open(live, encoding="utf-8") as f:
                live_peers = {x["pubkey"]: x for x in parsers._split_conf(f.read())[1] if x["pubkey"]}
        except OSError:
            pass
        out, have = [], set()
        for x in peers:
            pub = x["pubkey"]
            if pub in transplanted and pub not in mine:
                continue                                  # устройство живёт на другом интерфейсе
            if pub in lost_pubs and pub not in transplanted:
                continue
            if pub in mine and pub in live_peers:
                x = live_peers[pub]
            have.add(pub)
            out.append(x)
        for pub in sorted(mine - have):
            if pub in live_peers:
                out.append(live_peers[pub])
        with open(dst, "w", encoding="utf-8") as f:
            f.write(parsers._build_conf(header, out))
        os.chmod(dst, 0o600)
