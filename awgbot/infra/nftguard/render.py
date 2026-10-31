"""render.py — часть пакета nftguard (см. __init__)."""
from __future__ import annotations

from awgbot.infra import nftjson
from awgbot.infra import nftguard as pkg       # имена пакета — через него: их подменяют тесты


# ── рендер таблицы ───────────────────────────────────────────────────────────

def _set_block(name: str, typ: str, elems: list[str], interval: bool) -> str:
    lines = [f"    set {name} {{", f"        type {typ}"]
    if interval:
        lines.append("        flags interval")
        elems = pkg.noverlap(elems)                # пересечение — «conflicting intervals», таблица целиком мимо
    if elems:
        lines.append("        elements = { " + ", ".join(elems) + " }")
    lines.append("    }")
    return "\n".join(lines)


def _ports(ps: list[int]) -> str:
    return "{ " + ", ".join(str(p) for p in ps) + " }" if len(ps) > 1 else str(ps[0])


def render(spec: pkg.GuardSpec) -> str:
    """Полный текст файла таблицы. Без времени и прочего шума: текст равен
    тексту ⇔ состояние равно состоянию, этим и живёт дешёвая сверка."""
    p = spec.ssh_port
    head: list[str] = [
        "#!/usr/sbin/nft -f",
        "# awg-bot: единственная точка файервола хоста (таблица awg_bot_guard).",
        "# Файл генерирует бот — правки руками перезапишутся. Управление:",
        "#   awg-bot firewall status | setup | allow <ip> | deny <ip> | apply | off",
        "# Вайтлист и порты — conf/app.yaml (firewall.*), устройства админа — из БД.",
    ]
    nat_chain: list[str] = []
    if spec.nat and spec.tunnel_nets4:
        nat_chain = [
            "",
            "    chain postrouting {",
            "        type nat hook postrouting priority srcnat; policy accept;",
        ]
        rule = f"ip saddr @{pkg.SET_TUNNEL_NETS} masquerade"
        if spec.nat_exclude_ifs:
            rule = f"oifname != {nftjson.ifs(spec.nat_exclude_ifs)} " + rule
        nat_chain.append("        " + rule)
        nat_chain += _guest_postrouting(spec) + _rfpeer_postrouting(spec)
        nat_chain.append("    }")
    elif _guest_postrouting(spec) or _rfpeer_postrouting(spec):
        nat_chain = ["", "    chain postrouting {",
                     "        type nat hook postrouting priority srcnat; policy accept;",
                     *_guest_postrouting(spec), *_rfpeer_postrouting(spec), "    }"]
    nat_chain += _guest_prerouting(spec)
    if not spec.filter:
        # NAT-only: файервол выключен, но пользователям нужен выход наружу.
        # Фильтрующих цепочек нет вовсе — политика хоста остаётся его. Одно
        # исключение: транзит линк ↔ линк — только между линками слотов с
        # VPN-транзитом (связь подсетей — его часть); при открытой политике
        # хоста закрыть остальные пары можно только здесь.
        # Второе — частные адреса назначения не уходят к провайдеру (ниже).
        rules = (_quarantine_forward(spec) + _rfpeer_forward(spec) + _pair_forward(spec) + _probe_forward(spec)
                 + _wan_private_drop(spec) + _guest_forward(spec) + _tg_forward(spec))
        if spec.peer_link_block:
            links = nftjson.ifs(spec.peer_link_block)
            if spec.peer_link_ifs:
                transit = nftjson.ifs(spec.peer_link_ifs)
                rules += [f"        iifname {links} oifname {links} iifname != {transit} drop",
                          f"        iifname {links} oifname {links} oifname != {transit} drop"]
            else:
                rules.append(f"        iifname {links} oifname {links} drop")
        block: list[str] = []
        if rules:
            block = ["", "    chain forward {",
                     "        type filter hook forward priority filter; policy accept;",
                     *rules,
                     "    }"]
        q_in = _quarantine_input(spec) + _pair_input(spec) + _probe_input(spec) + _guest_input(spec)
        if q_in:
            # карантин сопряжения и линк пары — и без фильтра: на сам ВПС с них
            # только канал своей паре (у стороны ВПС AllowedIPs = 0.0.0.0/0)
            block += ["", "    chain input {",
                      "        type filter hook input priority filter; policy accept;",
                      *_rfpeer_input(spec),
                      "        ct state established,related accept",
                      *q_in,
                      "    }"]
        out = head + [
            "# Форма NAT-only: firewall.enabled=false, фильтра нет, только выход пользователей в интернет (NAT).",
            f"table {pkg.TABLE}",
            f"delete table {pkg.TABLE}",
            f"table {pkg.TABLE} {{",
            pkg._set_block(pkg.SET_TUNNEL_NETS, "ipv4_addr", spec.tunnel_nets4, True),
            *_guest_sets(spec),
        ] + nat_chain + block + ["}"]
        return "\n".join(out) + "\n"
    out: list[str] = head + pkg.host_lines(spec.ssh_hosts) + [
        f"table {pkg.TABLE}",
        f"delete table {pkg.TABLE}",
        f"table {pkg.TABLE} {{",
        pkg._set_block(pkg.SET_ALLOW4, "ipv4_addr", spec.ssh_allow4, True),
        pkg._set_block(pkg.SET_ALLOW6, "ipv6_addr", spec.ssh_allow6, True),
        pkg._set_block(pkg.SET_TUNNEL_NETS, "ipv4_addr", spec.tunnel_nets4, True),
        pkg._set_block(pkg.SET_TUNNEL_ADMIN, "ipv4_addr", spec.tunnel_admin4, False),
        pkg._set_block(pkg.SET_LINK_PEERS, "ipv4_addr", spec.link_peers4, False),
        *_guest_sets(spec),
        "",
        "    chain input {",
        "        type filter hook input priority filter; policy drop;",
        *_rfpeer_input(spec),
        "        ct state established,related accept",
        "        ct state invalid drop",
        '        iifname "lo" accept',
        *_quarantine_input(spec),
        *_pair_input(spec),
        *_probe_input(spec),
        "        ip protocol icmp accept",
        "        meta l4proto ipv6-icmp accept",
    ]
    if spec.udp_ports:
        out.append(f"        udp dport {pkg._ports(spec.udp_ports)} accept")
    out += _guest_input(spec)
    if spec.admin_dns4:
        # резолвер админа: имена SMB локальных сетей шлюзов — только устройствам
        # админа; правило раньше общего «:53 из туннеля», иначе то пустило бы всех
        out.append(f"        ip daddr {spec.admin_dns4} udp dport 53 ip saddr @{pkg.SET_TUNNEL_ADMIN} accept")
        out.append(f"        ip daddr {spec.admin_dns4} tcp dport 53 ip saddr @{pkg.SET_TUNNEL_ADMIN} accept")
        out.append(f"        ip daddr {spec.admin_dns4} drop")
    if spec.tunnel_nets4:
        out.append(f"        ip saddr @{pkg.SET_TUNNEL_NETS} udp dport 53 accept")
        out.append(f"        ip saddr @{pkg.SET_TUNNEL_NETS} tcp dport 53 accept")
    if spec.link_peers4 and spec.link_channel_port and spec.link_ifs:
        # Канал ВПС ↔ шлюз: вход только с адреса шлюза в /30 своего линка И
        # только из интерфейса линка: адрес /30 снаружи подделать можно, а
        # пакет, пришедший с публичного интерфейса, линком быть не может.
        # Пользователям туннеля сюда хода нет — их подсеть в другом наборе.
        ports = [int(spec.link_channel_port), *[int(p) for p in spec.link_channel_extra]]
        out.append(f"        iifname {nftjson.ifs(spec.link_ifs)} ip saddr @{pkg.SET_LINK_PEERS} "
                   f"tcp dport {pkg._ports(ports)} accept")
    if spec.open_tcp:
        out.append(f"        tcp dport {pkg._ports(spec.open_tcp)} accept")
    if spec.open_udp:
        out.append(f"        udp dport {pkg._ports(spec.open_udp)} accept")
    out.append("")
    if spec.tunnel_nets4:
        # SSH из туннеля — только устройствам админа
        out.append(f"        tcp dport {p} ip saddr @{pkg.SET_TUNNEL_ADMIN} accept")
        out.append(f"        tcp dport {p} ip saddr @{pkg.SET_TUNNEL_NETS} drop")
    if spec.ssh_open:
        out.append(f"        tcp dport {p} accept")
    else:
        out.append(f"        tcp dport {p} ip saddr @{pkg.SET_ALLOW4} accept")
        out.append(f"        tcp dport {p} ip6 saddr @{pkg.SET_ALLOW6} accept")
        out.append(f"        tcp dport {p} drop")
    out.append("    }")
    if spec.own_forward:
        out += [
            "",
            "    chain forward {",
            "        type filter hook forward priority filter; policy drop;",
            *_quarantine_forward(spec),
            *_rfpeer_forward(spec),
            *_pair_forward(spec),
            *_probe_forward(spec),
            "        ct state established,related accept",
            "        ct state invalid drop",
        ]
        if spec.admin_dns4:
            # адрес резолвера админа — только локальный: пропади он с хоста,
            # запрос устройства админа не уходит маршрутом по умолчанию к провайдеру
            out.append(f"        ip daddr {spec.admin_dns4} drop")
        out += _wan_private_drop(spec)
        out += _guest_forward(spec)          # зона резерва — после запрета частных адресов наружу
        out += _tg_forward(spec)             # агенты шлюзов основы из зоны — только к Telegram
        if spec.peer_link_ifs:
            # линк ↔ линк: связь подсетей между слотами с VPN-транзитом;
            # что именно едет, ограничивает AllowedIPs на стороне шлюза
            links = nftjson.ifs(spec.peer_link_ifs)
            out.append(f"        iifname {links} oifname {links} accept")
        if spec.tunnel_ifs:
            # пир → пир: только устройствам админа (шлюз, свои устройства);
            # остальным пользователям друг до друга пути нет
            ifs = nftjson.ifs(spec.tunnel_ifs)
            out.append(f"        iifname {ifs} oifname {ifs} ip saddr @{pkg.SET_TUNNEL_ADMIN} accept")
            out.append(f"        iifname {ifs} oifname {ifs} drop")
        if spec.tunnel_nets4:
            if spec.lan_space4:
                # адреса в туннеле локальных сетей шлюзов — только устройствам
                # админа; второй рубеж после шлюза. Назначение — исходное
                # (ct original): к этому хуку адрес уже переведён в настоящий
                out.append(f"        ip saddr @{pkg.SET_TUNNEL_NETS} ip saddr != @{pkg.SET_TUNNEL_ADMIN} "
                           f"ct original ip daddr {spec.lan_space4} drop")
            out.append(f"        ip saddr @{pkg.SET_TUNNEL_NETS} accept")
            out.append(f"        ip daddr @{pkg.SET_TUNNEL_NETS} accept")
        out.append("    }")
    out += nat_chain
    out.append("}")
    return "\n".join(out) + "\n"


def _quarantine_input(spec) -> list[str]:
    """Линк сопряжения до верного кода: на ВПС с него — только канал на адрес
    ВПС в /30 этого линка (адрес — по конфигу; правило канала ниже держит и
    адрес шлюза), прочее — drop."""
    if not spec.quarantine_ifs:
        return []
    q = nftjson.ifs(spec.quarantine_ifs)
    ports = [int(spec.link_channel_port), *[int(p) for p in spec.link_channel_extra]] if spec.link_channel_port else []
    out = []
    if ports:
        # каждый линк — своей паре адресов: шлюз этого линка к адресу ВПС
        # этого линка, а не любой адрес шлюзов на любой локальный адрес
        for iface, vps, gw in spec.quarantine_pairs:
            out.append(f'        iifname "{iface}" ip saddr {gw} ip daddr {vps} tcp dport {pkg._ports(ports)} accept')
    out.append(f"        iifname {q} drop")
    return out


def _quarantine_forward(spec) -> list[str]:
    """Пересылки с линка сопряжения и на него нет вовсе — раньше любых
    разрешений транзита и связи подсетей."""
    if not spec.quarantine_ifs:
        return []
    q = nftjson.ifs(spec.quarantine_ifs)
    return [f"        iifname {q} drop", f"        oifname {q} drop"]


def _pair_input(spec) -> list[str]:
    """Линк пары серверов: с него на хост — только канал пары (TCP) с адреса
    второй стороны в /30; всё остальное с линка — прочь. Правило одно и то же
    до кода (карантин) и после: другого у линка пары нет никогда."""
    if not spec.pair_if or not spec.pair_peer4 or not spec.pair_port:
        return []
    q = nftjson.ifs([spec.pair_if])
    return [f"        iifname {q} ip saddr {spec.pair_peer4} tcp dport {int(spec.pair_port)} accept",
            f"        iifname {q} drop"]


RFP_PRIVATE4 = (*pkg.PRIVATE4, "100.64.0.0/10", "169.254.0.0/16")


def _rfpeer_input(spec) -> list[str]:
    """Просящий: `AllowedIPs` линка пары расширены до 0.0.0.0/0, и вторая сторона может прислать
    пакет с любым адресом — на хост с линка пары только к своему адресу в /30 (канал пары),
    раньше `ct state established`: иначе пакет под чужое соединение хоста прошёл бы."""
    if not spec.pair_if or not spec.rfp_self4:
        return []
    return [f"        iifname {nftjson.ifs([spec.pair_if])} ip daddr != {spec.rfp_self4} drop"]


def _rfpeer_forward(spec) -> list[str]:
    """РФ-доступ через второй сервер — раньше запрета пересылки линка пары. Транзитный:
    с адреса второй стороны из линка пары — только в линки своих слотов и не в частные
    адреса; обратно — только ответы. Просящий: пользователи из интерфейсов AWG в линк
    пары не в частные; из линка пары — только ответы соединений, ушедших туда с маскарадом."""
    if not spec.pair_if:
        return []
    q = nftjson.ifs([spec.pair_if])
    priv = "{ " + ", ".join(RFP_PRIVATE4) + " }"
    out: list[str] = []
    if spec.rfp_offer4 and spec.link_ifs:
        out += [f"        iifname {q} ip saddr {spec.rfp_offer4} oifname {nftjson.ifs(spec.link_ifs)} ip daddr != {priv} accept",
                f"        oifname {q} ip daddr {spec.rfp_offer4} ct direction reply accept"]
    if spec.rfp_self4 and spec.tunnel_ifs and spec.tunnel_nets4:
        out += [f"        iifname {nftjson.ifs(spec.tunnel_ifs)} oifname {q} ip saddr @{pkg.SET_TUNNEL_NETS} ip daddr != {priv} accept",
                f"        iifname {q} ct direction reply ct reply ip daddr {spec.rfp_self4} accept"]
    return out


def _rfpeer_postrouting(spec) -> list[str]:
    """Маскарад транзита: у транзитного — с адреса второй стороны в линки слотов (шлюз адреса
    линка пары не знает); у просящего — пользователи в линк пары своим адресом в /30."""
    if not spec.pair_if:
        return []
    q = nftjson.ifs([spec.pair_if])
    out: list[str] = []
    if spec.rfp_offer4 and spec.link_ifs:
        out.append(f"        oifname {nftjson.ifs(spec.link_ifs)} ip saddr {spec.rfp_offer4} masquerade")
    if spec.rfp_self4 and spec.tunnel_nets4:
        out.append(f"        oifname {q} ip saddr @{pkg.SET_TUNNEL_NETS} masquerade")
    return out


def _pair_forward(spec) -> list[str]:
    """Пересылки через линк пары нет, кроме РФ-доступа через второй сервер (выше) — раньше
    любых разрешений."""
    if not spec.pair_if:
        return []
    q = nftjson.ifs([spec.pair_if])
    return [f"        iifname {q} drop", f"        oifname {q} drop"]


def _probe_input(spec) -> list[str]:
    """Кворум резерва: на хост с интерфейса AWG — только пробы зондов своей линии к
    ответчику (udp на его порт), всё прочее с блока зондов — прочь."""
    if not spec.probe_if or not spec.probe_resp4 or not spec.probe_port:
        return []
    q = nftjson.ifs([spec.probe_if])
    return [f"        iifname {q} ip saddr {spec.probe_net4} ip daddr {spec.probe_resp4} udp dport {int(spec.probe_port)} accept",
            f"        ip saddr {spec.probe_block4} drop"]


def _probe_forward(spec) -> list[str]:
    """Пересылки с блока зондов нет вовсе: у зонда нет выхода в интернет по построению."""
    if not spec.probe_if or not spec.probe_resp4:
        return []
    return [f"        ip saddr {spec.probe_block4} drop"]


def _guest_sets(spec) -> list[str]:
    """Набор пула адресов Telegram — только пока зона обслуживает (serving); набор
    Telegram для выхода агентов из зоны — пока стоит veth awggx."""
    out = []
    if spec.zone_tg_if:
        out.append(pkg._set_block(pkg.SET_TG_OUT, "ipv4_addr", spec.tg_out4 or list(pkg.TG_OUT4), True))
    if not spec.guest_webhook_port or not spec.guest_zone4:
        return out
    return out + [pkg._set_block(pkg.SET_TG_POOL, "ipv4_addr", spec.tg_pool4 or list(pkg.DEFAULT_TG_POOL), True),
                  f"    set {pkg.SET_TG_SEEN} {{\n        type ipv4_addr\n        flags dynamic,timeout\n        timeout 10m\n        size 4096\n    }}"]


def _tg_forward(spec) -> list[str]:
    """Выход агентов шлюзов основы из зоны (veth awggx): только во внешние
    интерфейсы, только tcp 443 на набор Telegram; прочее с veth — drop. В обеих
    формах файервола — без своего фильтра пересылку решала бы политика хоста."""
    if not spec.zone_tg_if:
        return []
    v = nftjson.ifs([spec.zone_tg_if])
    out = []
    if not spec.filter:
        out.append(f"        oifname {v} ct state established,related accept")
    if spec.wan_ifs:
        out.append(f"        iifname {v} oifname {nftjson.ifs(spec.wan_ifs)} ip daddr @{pkg.SET_TG_OUT} tcp dport 443 accept")
    else:
        out.append(f"        iifname {v} ip daddr @{pkg.SET_TG_OUT} tcp dport 443 accept")
    out.append(f"        iifname {v} drop")
    return out


def _guest_input(spec) -> list[str]:
    """Гостевая зона резерва, вход на хост: порт клона (его UDP-сокет живёт у
    хоста, расшифрованный пакет выходит в зоне) и, в relay, тот же порт для
    пересылки хвоста (DNAT ниже — правило input его не видит, но ct нужен)."""
    out = []
    if spec.guest_phase in ("taking", "serving") and (spec.guest_udp_ports or spec.guest_udp_port):
        out.append(f"        udp dport {pkg._ports(list(spec.guest_udp_ports) or [int(spec.guest_udp_port)])} accept")
    if spec.guest_phase == "serving" and spec.guest_webhook_port and spec.guest_zone4:
        # SYN на порт вебхука с адреса вне пула виден до отбрасывания: адрес и
        # число попыток — в динамический набор (обучение пула, reserve/tgpool)
        out.append(f"        tcp dport {int(spec.guest_webhook_port)} ip saddr != @{pkg.SET_TG_POOL} "
                   f"tcp flags & (syn|ack) == syn limit rate 20/second "
                   f"update @{pkg.SET_TG_SEEN} {{ ip saddr counter }} drop")
    return out


def _guest_forward(spec) -> list[str]:
    """Пересылка зоны: наружу через veth (бот основы, Telegram, почта, фиды,
    пользователи в интернет), вебхук после DNAT — в зону, хвост после DNAT — к
    основе; в зону с внешнего интерфейса без DNAT хода нет."""
    out = []
    if not spec.filter and spec.guest_phase in ("taking", "serving"):
        # NAT-only: общего «established accept» наверху нет, а ниже стоят drop на вход в
        # зону — ответы наружным соединениям зоны пропускаются здесь
        ifs = [i for i in (spec.guest_veth_if, spec.guest_rf_if) if i]
        if ifs:
            out.append(f"        oifname {nftjson.ifs(ifs)} ct state established,related accept")
    if spec.guest_veth_if and spec.guest_phase in ("taking", "serving"):
        v = nftjson.ifs([spec.guest_veth_if])
        if spec.wan_ifs:
            out.append(f"        iifname {v} oifname {nftjson.ifs(spec.wan_ifs)} accept")   # только наружу
            out.append(f"        iifname {v} drop")             # ни к своим пользователям, ни в линки, ни в локальные сети
        else:
            out.append(f"        iifname {v} ip daddr != @{pkg.SET_TUNNEL_NETS} accept")
        if spec.guest_webhook_port and spec.guest_zone4:
            out.append(f"        oifname {v} ip daddr {spec.guest_zone4} tcp dport {int(spec.guest_webhook_port)} "
                       f"ct status dnat accept")
        out.append(f"        oifname {v} drop")
    if spec.guest_rf_if and spec.guest_phase in ("taking", "serving"):
        # РФ-трафик пользователей основы из зоны — только в линки моих слотов (запасной
        # слот «шлюзы резерва»); ответы — по conntrack
        r = nftjson.ifs([spec.guest_rf_if])
        if spec.link_ifs:
            # не к частным адресам: линк шлюза резерва и его локальная сеть — не для пользователей основы
            nets = "{ " + ", ".join(pkg.PRIVATE4) + " }"
            out.append(f"        iifname {r} oifname {nftjson.ifs(spec.link_ifs)} ip daddr != {nets} accept")
        out.append(f"        iifname {r} drop")
        out.append(f"        oifname {r} drop")              # в зону через этот veth — только ответы (ct выше)
    if spec.relay_to and spec.relay_udp_port:
        out.append("        ct status dnat meta l4proto udp accept")
    return out


def _guest_prerouting(spec) -> list[str]:
    """DNAT: порт вебхука с пула Telegram — боту основы в зоне (serving); порт
    клона — на основу (relay: хвост сессий, пока пользователи не переподключились)."""
    rules = []
    if spec.guest_phase == "serving" and spec.guest_webhook_port and spec.guest_zone4:
        rules.append(f"        tcp dport {int(spec.guest_webhook_port)} ip saddr @{pkg.SET_TG_POOL} "
                     f"dnat ip to {spec.guest_zone4}:{int(spec.guest_webhook_port)}")
    if spec.guest_phase == "relay" and spec.relay_to and spec.relay_udp_port:
        ports = list(spec.relay_udp_ports) or [int(spec.relay_udp_port)]
        host = spec.relay_to.rsplit(":", 1)[0]
        wan = f"iifname {nftjson.ifs(spec.wan_ifs)} " if spec.wan_ifs else ""   # только с внешнего интерфейса
        # counter — по нему бот видит, идёт ли ещё хвост (пересылка снимается после тишины)
        if len(ports) == 1:
            rules.append(f"        {wan}udp dport {int(ports[0])} counter dnat ip to {spec.relay_to}")
        else:                                                 # каждый порт — на тот же порт основы
            rules.append(f"        {wan}udp dport {pkg._ports(ports)} counter dnat ip to {host}")
    if not rules:
        return []
    return ["", "    chain prerouting {", "        type nat hook prerouting priority dstnat; policy accept;",
            *rules, "    }"]


def _guest_postrouting(spec) -> list[str]:
    """Маскарад зоны наружу (veth и блоки NETMAP подсетей клонов) и пересланного хвоста (ответ основы должен прийти
    сюда же, чтобы вернуться пользователю через conntrack)."""
    out = []
    if spec.guest_zone_net4 and spec.guest_phase in ("taking", "serving"):
        nets = [spec.guest_zone_net4, *spec.guest_pool4]
        rule = f"ip saddr {nets[0] if len(nets) == 1 else '{ ' + ', '.join(nets) + ' }'} masquerade"
        if spec.nat_exclude_ifs:
            rule = f"oifname != {nftjson.ifs(spec.nat_exclude_ifs)} " + rule
        out.append("        " + rule)
    if spec.guest_rf_if and spec.guest_rf_zone4 and spec.link_ifs and spec.guest_phase in ("taking", "serving"):
        out.append(f"        oifname {nftjson.ifs(spec.link_ifs)} ip saddr {spec.guest_rf_zone4} masquerade")
    if spec.guest_phase == "relay" and spec.relay_to and spec.relay_udp_port:
        out.append("        ct status dnat meta l4proto udp masquerade")
    if spec.zone_tg_if and spec.zone_tg_net4:                 # агенты из зоны к Telegram — адресом хоста
        rule = f"ip saddr {spec.zone_tg_net4} masquerade"
        if spec.nat_exclude_ifs:
            rule = f"oifname != {nftjson.ifs(spec.nat_exclude_ifs)} " + rule
        out.append("        " + rule)
    return out


def _wan_private_drop(spec) -> list[str]:
    """Частные адреса назначения (RFC 1918, с ними и пространство адресов в
    туннеле) не уходят во внешний интерфейс — ни у кого. Без правила такой
    пакет шёл бы маршрутом по умолчанию к провайдеру: адрес в туннеле в окно,
    когда таблицы awg_lan_map нет (ребут до старта бота, адреса выключены,
    у слотов нет подсетей), и старые закладки на настоящие адреса сетей
    шлюзов. Только из интерфейсов AWG сервера и линков: проходящий трафик
    хоста не из туннеля не задет. Резолвер и перехват DNS — локальные
    (input), РФ-доступ — публичные адреса, линки и awg-интерфейсы — не
    внешние: их это не касается."""
    # Только из туннеля пользователей и линков: прочий проходящий трафик хоста
    # (VM, резолвер сети провайдера) — не наше, в NAT-only тем более.
    src = list(dict.fromkeys([*spec.tunnel_ifs, *spec.link_ifs]))
    if spec.guest_veth_if and spec.guest_phase in ("taking", "serving"):
        src.append(spec.guest_veth_if)                       # бот основы в зоне — чужой код, в частную сеть провайдера ему нельзя
    if not spec.wan_ifs or not src:
        return []
    nets = "{ " + ", ".join(pkg.PRIVATE4) + " }"
    return [f"        iifname {nftjson.ifs(src)} oifname {nftjson.ifs(spec.wan_ifs)} ip daddr {nets} drop"]
