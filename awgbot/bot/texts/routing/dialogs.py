"""dialogs.py — диалоги слота: локальная сеть, роутер, переключение, VPN-транзит, подпись, снятие."""

from __future__ import annotations

from awgbot.bot import ui

from ..fmt import _e, details

from .slots import ROUTER_IP_PLACEHOLDER, _slot_down_for, slot_name, slot_ref


# ── диалоги слота ────────────────────────────────────────────────────────────

def _reissue_or_channel(online: bool, what: str) -> str:
    """Хвост диалога: при живом канале изменение доедет само, без файла."""
    if online:
        return f"{what} шлюз получит по каналу и применит сам; итог придёт в чат бота шлюза"
    return f"{what} потребует перевыпуска конфигурации шлюза"


def already_state(on: bool) -> str:
    """Всплывашка на повторное нажатие «Включить/Выключить» с той же целью."""
    return "Уже включено" if on else "Уже выключено"


GW_TOKEN_NOT_FORGOTTEN = ("⚠️ Токен бота этого устройства не убран из env бота сервера — "
                          "убери строку GW_BOT_TOKEN слота руками, иначе новый слот с тем же "
                          "номером получит его в файле первого применения")


def gateway_lan_ask(state: dict, on: bool, resolver: str) -> str:
    """Диалог «🔀 VPN-транзит»: что произойдёт, что нужно от человека, чем грозит."""
    name = slot_ref(state)
    chan = state.get("channel") or {}
    # агент с документом получит режим каналом и не на связи — как только появится
    online = bool(chan.get("online")) or bool((chan.get("cfg") or {}).get("cfg1"))
    if on:
        lines = [ui.head(f"🔀 VPN-транзит на {name}") + " — включить?",
                 "Роутер отдаёт весь трафик сети шлюзу, шлюз маршрутизирует: заблокированное — в туннель, "
                 "остальное — напрямую. VPN на устройствах в сети становится не нужен",
                 "Нужно от тебя: настроить роутер по рецепту — покажу после включения",
                 "⚠️ Шлюз станет точкой отказа: упадёт — подсеть без интернета, резерв не поможет"]
        peers = [slot_ref(x) for x in state.get("states") or []
                 if x["gateway"].id != state["gateway"].id and x["gateway"].lan_mode]
        if peers:
            lines.append(f"Сеть свяжется с сетями других шлюзов с VPN-транзитом ({', '.join(peers)}): их "
                         "устройства смогут ходить сюда по адресам в туннеле, а здешние — к ним. Только твои "
                         "локальные сети")
        if not resolver:
            lines.append("⚠️ Свой резолвер не настроен: шлюз пойдёт на <code>1.1.1.1</code> через туннель, без "
                         "защиты от DoH — рекомендуется настроить (⚙️ → 🖥 Сервер AWG)")
        if not online:
            lines.append("Потребуется перевыпуск конфигурации шлюза")
        return "\n".join(lines)
    return "\n".join([
        ui.head(f"🔀 VPN-транзит на {name}") + " — выключить?",
        "⚠️ Сначала убери на роутере маршрутизацию всего трафика на шлюз — иначе сеть останется без интернета",
        f"Связь подсетей для {name} выключится; списки и резолвер снимутся, свои списки останутся"
        + ("" if online else ". " + _reissue_or_channel(False, "Выключение")),
    ])


GATEWAY_LAN_NO_SUBNET = "Сначала задай подсеть шлюза («🗺 Подсети»): без неё работать не будет"


ROUTER_TABS = (("mt", "MikroTik"), ("ow", "OpenWrt"))


def gateway_router_text(title: str, net: str, gw_ip: str = "", peer_nets: list | None = None,
                        tab: str = "mt") -> str:
    """❓ Роутер — вкладками: требования одной строкой, подробно — под
    «подробнее», рецепт выбранной вкладки. Адрес шлюза в подсети знает
    только он сам: основной бот показывает плейсхолдер."""
    net = net or "ПОДСЕТЬ"
    gw_ip = _e(gw_ip) if gw_ip else ROUTER_IP_PLACEHOLDER
    peers = [str(p) for p in (peer_nets or []) if p]
    head = (ui.head(f"❓ Роутер для {_e(title)}", meta=[f"<code>{_e(net)}</code>", f"шлюз <code>{gw_ip}</code>"]) + "\n"
            "Весь трафик сети, кроме шлюза и локального, — на шлюз; DNS по DHCP — шлюз; "
            "ускорение и IPv6 — выключить; асимметричный путь — разрешить")
    req = details("• весь трафик локальной сети, кроме самого шлюза и трафика внутри сети, — на адрес "
                  "шлюза (policy-based routing);\n"
                  "• разрешён асимметричный путь: ответ от шлюза к устройству идёт мимо роутера — "
                  "правило выше drop invalid;\n"
                  "• аппаратное ускорение (fasttrack, flow offloading) выключено;\n"
                  "• DHCP раздаёт DNS = адрес шлюза; у шлюза статический адрес;\n"
                  "• IPv6 в локальной сети выключен (RA/DHCPv6): резолвер шлюза AAAA не отдаёт, "
                  "но адрес v6 от роутера увёл бы трафик мимо туннеля")
    recipes = []
    if tab in ("ow", "all"):
        ow_peer = "".join(f"ip route add {_e(p)} via {gw_ip}\n" for p in peers)
        recipes.append(ui.sub("OpenWrt") + "\n"
                  "<pre>echo '200 vpn' &gt;&gt; /etc/iproute2/rt_tables\n"
                  f"ip route add default via {gw_ip} table vpn\n"
                  f"ip rule add from {gw_ip} priority 100 lookup main\n"
                  f"ip rule add from {_e(net)} to {_e(net)} priority 110 lookup main\n"
                  f"ip rule add from {_e(net)} priority 120 lookup vpn\n"
                  f"iptables -I FORWARD 1 -s {_e(net)} ! -d {_e(net)} -j ACCEPT\n"
                  "uci set firewall.@defaults[0].flow_offloading_hw='0'\n"
                  "uci set firewall.@defaults[0].flow_offloading='0'\n"
                  f"{ow_peer}"
                  f"uci add_list dhcp.lan.dhcp_option='6,{gw_ip}'\n"
                  "uci commit</pre>\n"
                  "Порядок важен: сначала исключается сам шлюз, затем трафик внутри сети, потом всё "
                  "остальное уходит на шлюз. Закрепить: ip rule/route — в /etc/rc.local, "
                  "iptables — в /etc/firewall.user")
    if tab in ("mt", "all"):
        mt_peer = "".join(f"/ip route add dst-address={_e(p)} gateway={gw_ip}\n" for p in peers)
        recipes.insert(0, ui.sub("MikroTik RouterOS 7") + "\n"
                  "<pre>/routing table add disabled=no fib name=antiblock\n"
                  "/ip firewall mangle\n"
                  f"add action=accept chain=prerouting comment=anti-loop src-address={gw_ip}\n"
                  "add action=mark-routing chain=prerouting comment=all-LAN-via-gw \\\n"
                  f"    new-routing-mark=antiblock passthrough=no src-address={_e(net)} dst-address=!{_e(net)}\n"
                  f"/ip route add dst-address=0.0.0.0/0 gateway={gw_ip} routing-table=antiblock\n"
                  "/ip firewall filter\n"
                  f"add action=accept chain=forward comment=asym-via-gw src-address={_e(net)} dst-address=!{_e(net)}\n"
                  f"{mt_peer}"
                  f"/ip dhcp-server network set [find] dns-server={gw_ip}</pre>\n"
                  "Правило asym-via-gw — выше drop invalid; fasttrack выключить")
    recipe = "\n\n".join(recipes)
    peer_note = ("" if not peers else
                 "\nСвязь подсетей: сам роутер отвечает со своего адреса по основной таблице, мимо "
                 "заворота, — чтобы он был достижим из подсетей других шлюзов, ему нужен маршрут до "
                 "них через шлюз (строки с " + ", ".join(f"<code>{_e(p)}</code>" for p in peers)
                 + "). Устройствам за роутером это не нужно")
    return f"{head}\n{req}\n{recipe}{peer_note}"


def gateway_switch_ask(target: dict, current, healthy: bool) -> str:
    who = slot_ref(target)
    cur = slot_ref(current) if current else "Прежний шлюз"
    if healthy:
        return (f"▶️ Переключить трафик на {who}?\n"
                "РФ-сервисы у всех начнут выходить с адреса этой сети — приложения могут попросить "
                f"войти заново. {cur} останется в резерве, обратно бот сам не вернёт")
    return (f"⚠️ {who} не отвечает {_slot_down_for(target)} — точно переключаем?\n"
            "РФ-сервисы у всех перестанут работать, пока он не оживёт")


SMB_TUNNEL_ON = "✅ SMB в туннеле включён"
SMB_TUNNEL_OFF = "☑️ SMB в туннеле выключен"


def smb_tunnel_records_failed(on: bool) -> str:
    """Тумблер переставлен, а записи резолвера админа поставить или снять не вышло."""
    return ("⚠️ SMB в туннеле включён, но имена не поставлены" if on else
            "⚠️ SMB в туннеле выключен, но имена ещё не сняты") + " — диагностика на сервере: <code>awg-bot resolver admin status</code>"


def gateway_smb_ask(on: bool, split_tunnel: bool = False) -> str:
    """Диалог тумблера «🗂 SMB в туннеле». split_tunnel — конфиги сервера с
    раздельным туннелем: имена устройство админа увидит, а подключение к диску
    пойдёт мимо туннеля (настоящих адресов локальных сетей в его AllowedIPs нет)."""
    if on:
        warn = ("\n⚠️ Туннель у конфигов раздельный (client_config.allowed_ips): имена будут видны, "
                "но подключиться к дискам получится только с устройства с полным туннелем") if split_tunnel else ""
        return ui.confirm("🗂 SMB в туннеле — включить?",
                          "SMB-серверы локальных сетей шлюзов станут видны по именам на твоих устройствах в туннеле: "
                          "macOS — Finder, «Сеть» → awg.internal; Windows — <code>\\\\имя.awg.internal</code>. "
                          "Ресурсы доступны только с устройств админа") + warn
    return ui.confirm("🗂 SMB в туннеле — выключить?",
                      "Имена SMB-серверов пропадут с твоих устройств; по адресам серверы останутся доступны тебе")


def gateway_lan_text(state: dict) -> str:
    gw = state["gateway"]
    nets = gw.lan_subnets
    cur = ", ".join(f"<code>{_e(n)}</code>" for n in nets) if nets else "не заданы"
    lines = [ui.head(f"🗺 Подсети {slot_ref(state)}", meta=[f"сейчас: {cur}"]), "",
             "Пришли подсети через пробел: <code>192.168.2.0/24</code>; «-» — убрать все. "
             "Доступ через туннель — только твоим устройствам"]
    if gw.lan_mode:
        lines.append("VPN-транзит будет работать для первой подсети в списке")
    return "\n".join(lines)


def gateway_lan_report(res: dict, state: dict) -> str:
    """Итог правки подсетей — первыми строками карточки."""
    parts = []
    kept = res.get("kept") or []
    shown = ", ".join(f"<code>{_e(n)}</code>" for n in kept[:8]) + (f" и ещё {len(kept) - 8}" if len(kept) > 8 else "")
    parts.append(f"✅ Подсети {slot_ref(state)}: " + (shown if kept else "убраны"))
    rejected = res.get("rejected") or []
    if rejected:
        shown = "; ".join(f"<code>{_e(raw[:40])}</code> — {_e(why)}" for raw, why in rejected[:3])
        more = f"; и ещё {len(rejected) - 3}" if len(rejected) > 3 else ""
        parts.append(f"⚠️ Не принято: {shown}{more}")
    return "\n".join(parts)


def gateway_label_text(state: dict) -> str:
    return (ui.head(f"✏️ Подпись {slot_ref(state)}") + " — место одним-двумя словами: «дача», «офис». "
            "До 20 символов, «—» — убрать")


def gateway_remove_ask(dev, *, state=None, other=None) -> str:
    """Три варианта: резервный; активный при живом резерве; последний."""
    name = _e(dev.name)
    if state and not state.get("active") and other is not None:
        return (f"🛑 {name} — больше не резерв?\n"
                f"Линк снимется; трафик пойдёт через {slot_ref(other)}, резерва не будет. "
                "Устройство станет обычным")
    if state and state.get("active") and other is not None:
        return (f"🛑 {name} — больше не шлюз?\n"
                f"Трафик сразу перейдёт на {slot_ref(other)} — адрес сменится, приложения могут "
                "попросить войти заново. Линк снимется; устройство станет обычным")
    return (f"🛑 {name} — больше не шлюз?\n"
            "Ключи линка сменятся; РФ-доступ выключится до назначения нового. Устройство станет обычным")


def gateway_removed(dev, now_active=None) -> str:
    if now_active is not None:
        return f"🛑 {_e(dev.name)} больше не шлюз · трафик идёт через {slot_ref(now_active)}, резерва нет"
    return f"🛑 {_e(dev.name)} больше не шлюз · РФ-доступ выключен до назначения нового"


def gateway_keys_ask(state: dict, live_standby: bool) -> str:
    """Диалог «🔑 Ключи линка» (по правилу подтверждений: вопрос и цена)."""
    lines = [f"🔑 Сменить ключи линка {slot_name(state)}?"]     # с подписью: два шлюза с одним именем различимы
    if state.get("active"):
        lines.append("Новые ключи уйдут шлюзу по упр. каналу — подтверди их в чате бота шлюза. "
                     "РФ-доступ прервётся на секунды; не поднимется — обе стороны вернут прежние")
        if live_standby:
            lines.append("Есть живой резерв: сначала сделай его активным — смена пройдёт без обрыва")
    else:
        lines.append("Новые ключи уйдут шлюзу по упр. каналу — подтверди их в чате бота шлюза. "
                     "Не поднимется — обе стороны вернут прежние")
    return "\n".join(lines)


GATEWAY_KEYS_PICK = "\n".join([
    ui.head("🔑 Ключи линка"),
    "Новые ключи, PSK и обфускация линка того же устройства — на случай утечки файла первого применения "
    "или компрометации обфускации. Выбери шлюз — он применит их после подтверждения в чате своего бота"])
GATEWAY_TOKEN_PICK = "\n".join([
    ui.head("🤖 Токен бота шлюза"),
    "Новый токен бота шлюза — когда прежний утёк и отозван в @BotFather. Можно и другого бота: вся "
    "настройка шлюза останется. Уедет шлюзу по упр. каналу, бот шлюза перезапустится на нём"])
GATEWAY_TOKEN_SENT = "🤖 Новый токен ушёл шлюзу — бот шлюза перезапустится на нём"
GATEWAY_TOKEN_QUEUED = "🤖 Новый токен записан — шлюз получит его по упр. каналу при подключении"


def gateway_token_prompt(name: str) -> str:
    return (ui.head(f"🤖 Новый токен бота {_e(name)}") + " — пришли токен из @BotFather "
            "<code>123456789:AA…</code>: прежнего бота или нового. "
            "Уедет шлюзу по упр. каналу, бот шлюза перезапустится; сообщение с токеном сразу удалю")


def gateway_token_refused(why: str) -> str:
    return f"⚠️ Токен не принят: {_e(why)}. Пришли токен ещё раз или нажми «✖️ Отмена»"


def gateway_token_new_bot(username: str) -> str:
    if not username:
        return "Бот шлюза изменился: нажми «Запустить» в чате с ним — без этого он не сможет тебе написать"
    return (f"Бот шлюза теперь @{_e(username)}: открой его и нажми «Запустить» — без этого он не сможет "
            "тебе написать")


GATEWAY_KEYS_SENT = "🔑 Новые ключи ушли шлюзу — подтверди их в чате бота шлюза"
GATEWAY_KEYS_OFFLINE = "Шлюз не на связи — новые ключи уйдут по упр. каналу, когда он выйдет на связь"

