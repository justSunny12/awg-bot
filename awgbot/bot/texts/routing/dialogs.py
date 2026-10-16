"""dialogs.py — диалоги слота: локальная сеть, роутер, переключение, VPN-транзит, подпись, снятие."""

from __future__ import annotations

from awgbot.bot import ui

from awgbot.util.textfmt import addr_codes

from ..fmt import _e, details

from .slots import ROUTER_IP_PLACEHOLDER, _slot_down_for, lan_pairs, lan_space_range, slot_name, slot_ref


# ── диалоги слота ────────────────────────────────────────────────────────────

def already_state(on: bool) -> str:
    """Всплывашка на повторное нажатие «Включить/Выключить» с той же целью —
    про VPN-транзит и SMB в туннеле, оба мужского рода."""
    return "Уже включён" if on else "Уже выключен"


def gw_token_not_forgotten(name: str, env_var: str, env_path: str) -> str:
    """Снятие шлюза: запись env не удалась, токен его бота остался — строку
    с ним человек убирает сам, иначе её получит следующий шлюз с тем же номером."""
    return (f"⚠️ Токен бота шлюза {_e(name)} не удалось убрать из <code>{_e(env_path)}</code> — удали строку "
            f"<code>{_e(env_var)}</code> сам, иначе новый шлюз с тем же номером получит его в файле "
            "конфигурации шлюза")


def _reissue_line(names, me: str) -> str:
    """«Потребуется перевыпуск конфигурации шлюза: A, B» — одной строкой с
    именами тех, кому изменение доедет только файлом; только сам слот — без
    имени. Пусто — строки нет."""
    names = list(dict.fromkeys(n for n in names if n))
    if not names:
        return ""
    if names == [me]:
        return "Потребуется перевыпуск конфигурации шлюза"
    return "Потребуется перевыпуск конфигурации шлюза: " + ", ".join(_e(n) for n in names)


def gateway_lan_ask(state: dict, on: bool, resolver: str, reissue=()) -> str:
    """Диалог «🔀 VPN-транзит»: что произойдёт, что нужно от человека, чем грозит;
    о перевыпуске — одной строкой с именами. reissue — кому подсети соседей
    после включения или выключения доедут только файлом (агенты 3.2.x этого и
    соседних слотов с VPN-транзитом)."""
    name = slot_ref(state)
    chan = state.get("channel") or {}
    # агент с документом получит режим каналом и не на связи — как только появится
    online = bool(chan.get("online")) or bool((chan.get("cfg") or {}).get("cfg1"))
    peers = [slot_ref(x) for x in state.get("states") or []
             if x["gateway"].id != state["gateway"].id and x["gateway"].lan_mode]
    linked = bool(peers) and not (state.get("lan_space") or {}).get("problem")
    # сам слот без канала получает всё только файлом — он в перечне первым
    me = state.get("display") or name
    names = ([me] if not online else []) + list(reissue)
    reissue_line = _reissue_line(names, me)
    if on:
        lines = [f"🔀 VPN-транзит на {name} — включить?",          # вопрос-подтверждение — без жирного
                 "Роутер отдаёт шлюзу весь трафик локальной сети, шлюз разводит его: заблокированное — в туннель, "
                 "остальное — напрямую. VPN на устройствах этой сети станет не нужен",
                 "Нужно от тебя: настроить роутер — что именно, покажу после включения",
                 "⚠️ Шлюз станет точкой отказа: упадёт — подсеть без интернета, резерв не поможет"]
        if linked:
            lines.append("🔗 Связь подсетей: устройства локальных подсетей шлюзов станут доступны из подсетей "
                         f"других шлюзов с включённым VPN-транзитом ({', '.join(peers)})")
        if not resolver:
            lines.append("⚠️ Свой резолвер не настроен: шлюз пойдёт на <code>1.1.1.1</code> через туннель, без "
                         "защиты от DoH — лучше задать свой: «⚙️ Настройки» → «🖥 Сервер AWG» → «🔒 Свой резолвер»")
        if reissue_line:
            lines.append(reissue_line)
        return "\n".join(lines)
    lines = [
        f"🔀 VPN-транзит на {name} — выключить?",
        "⚠️ Сначала убери на роутере маршрутизацию всего трафика на шлюз — иначе сеть останется без интернета",
        "Общие списки доменов и резолвер на шлюзе снимутся, свои списки сохранятся",
    ]
    if linked:
        # связь подсетей — своей строкой со значком, как в диалоге включения
        lines.insert(2, f"🔗 Связь подсетей {name} с другими шлюзами выключится")
    if reissue_line:
        lines.append(reissue_line)
    return "\n".join(lines)


GATEWAY_LAN_NO_SUBNET = "Сначала задай локальную подсеть шлюза — «🗺 Подсети»: VPN-транзит работает для неё"


ROUTER_TABS = (("mt", "MikroTik"), ("ow", "OpenWrt"))


def gateway_router_text(title: str, net: str, gw_ip: str = "", tab: str = "mt") -> str:
    """🛜 Настройка роутера — вкладками: требования одной строкой, подробно —
    под «подробнее», рецепт выбранной вкладки. Адрес шлюза в локальной сети
    знает он сам: основной бот берёт его из снимка канала конфигурации, а
    пока снимка нет — показывает плейсхолдер и говорит, где взять адрес. О
    связи подсетей рецепт не говорит: устройства сети отвечают соседям
    заворотом на шлюз, а сам роутер — шлюзу, который маскарадит к нему
    трафик соседей."""
    net = net or "ПОДСЕТЬ"
    placeholder = not gw_ip
    gw_ip = _e(gw_ip) if gw_ip else ROUTER_IP_PLACEHOLDER
    head = (ui.head(f"🛜 Настройка роутера для {_e(title)}", meta=[f"<code>{_e(net)}</code>", f"шлюз <code>{gw_ip}</code>"]) + "\n"
            "Весь трафик сети, кроме самого шлюза и трафика внутри сети, — на шлюз; DNS по DHCP — адрес шлюза; "
            "ускорение и IPv6 — выключить; асимметричный путь — разрешить")
    if placeholder:
        head += (f"\n<code>{ROUTER_IP_PLACEHOLDER}</code> — адрес шлюза в локальной сети: он на экране "
                 "«🔀 VPN-транзит» в чате бота шлюза; подставь его в команды")
    req = details("• весь трафик локальной сети, кроме самого шлюза и трафика внутри сети, — на адрес "
                  "шлюза (policy-based routing);\n"
                  "• разрешён асимметричный путь: ответ от шлюза к устройству идёт мимо роутера — "
                  "правило выше <code>drop invalid</code>;\n"
                  "• аппаратное ускорение (fasttrack, flow offloading) выключено;\n"
                  "• DHCP раздаёт DNS = адрес шлюза; у шлюза статический адрес;\n"
                  "• IPv6 в локальной сети выключен (RA/DHCPv6): резолвер шлюза AAAA не отдаёт, "
                  "но адрес v6 от роутера увёл бы трафик мимо туннеля")
    recipes = []
    if tab in ("ow", "all"):
        recipes.append(ui.sub("OpenWrt") + "\n"
                  "<pre>echo '200 vpn' &gt;&gt; /etc/iproute2/rt_tables\n"
                  f"ip route add default via {gw_ip} table vpn\n"
                  f"ip rule add from {gw_ip} priority 100 lookup main\n"
                  f"ip rule add from {_e(net)} to {_e(net)} priority 110 lookup main\n"
                  f"ip rule add from {_e(net)} priority 120 lookup vpn\n"
                  f"iptables -I FORWARD 1 -s {_e(net)} ! -d {_e(net)} -j ACCEPT\n"
                  "uci set firewall.@defaults[0].flow_offloading_hw='0'\n"
                  "uci set firewall.@defaults[0].flow_offloading='0'\n"
                  f"uci add_list dhcp.lan.dhcp_option='6,{gw_ip}'\n"
                  "uci commit</pre>\n"
                  "Порядок важен: сначала исключается сам шлюз, затем трафик внутри сети, потом всё "
                  "остальное уходит на шлюз. Чтобы правила пережили перезагрузку: <code>ip rule</code>/"
                  "<code>ip route</code> — в <code>/etc/rc.local</code>, <code>iptables</code> — в "
                  "<code>/etc/firewall.user</code>")
    if tab in ("mt", "all"):
        recipes.insert(0, ui.sub("MikroTik RouterOS 7") + "\n"
                  "<pre>/routing table add disabled=no fib name=antiblock\n"
                  "/ip firewall mangle\n"
                  f"add action=accept chain=prerouting comment=anti-loop src-address={gw_ip}\n"
                  "add action=mark-routing chain=prerouting comment=all-LAN-via-gw \\\n"
                  f"    new-routing-mark=antiblock passthrough=no src-address={_e(net)} dst-address=!{_e(net)}\n"
                  f"/ip route add dst-address=0.0.0.0/0 gateway={gw_ip} routing-table=antiblock\n"
                  "/ip firewall filter\n"
                  f"add action=accept chain=forward comment=asym-via-gw src-address={_e(net)} dst-address=!{_e(net)}\n"
                  f"/ip dhcp-server network set [find] dns-server={gw_ip}</pre>\n"
                  "Правило <code>asym-via-gw</code> — выше <code>drop invalid</code>; fasttrack выключить")
    recipe = "\n\n".join(recipes)
    return f"{head}\n{req}\n{recipe}"


def gateway_switch_ask(target: dict, current, healthy: bool) -> str:
    who = slot_ref(target)
    cur = slot_ref(current) if current else "Прежний шлюз"
    if healthy:
        return (f"▶️ Переключить трафик на {who}?\n"
                f"Российские сервисы у всех начнут открываться с адреса сети {who} — приложения могут попросить "
                f"войти заново. {cur} останется в резерве; обратно бот сам не переключит")
    return (f"⚠️ {who} не отвечает {_slot_down_for(target)} — точно переключаем?\n"
            "Российские сервисы у всех перестанут работать, пока он не оживёт")


SMB_TUNNEL_ON = "✅ SMB в туннеле включён"
SMB_TUNNEL_OFF = "☑️ SMB в туннеле выключен"


def smb_tunnel_records_failed(on: bool) -> str:
    """Тумблер переставлен, а записи резолвера админа поставить или снять не вышло."""
    return ("⚠️ SMB в туннеле включён, но имена не применились" if on else
            "⚠️ SMB в туннеле выключен, но имена ещё не сняты") + " — диагностика на сервере: <code>awg-bot resolver admin status</code>"


def gateway_smb_ask(on: bool) -> str:
    """Диалог тумблера «🗂 SMB в туннеле». Предупреждения о раздельном туннеле
    больше нет: имена ведут на адреса в туннеле, а их пространство устройства
    админа ведут в туннель и при раздельном (configgen.allowed_ips_for)."""
    if on:
        return ui.confirm("🗂 SMB в туннеле — включить?",
                          "SMB-серверы локальных сетей шлюзов станут видны по именам на твоих устройствах в "
                          "туннеле: macOS — Finder, «Сеть» → awg.internal; Windows — <code>\\\\имя.awg.internal</code>")
    return ui.confirm("🗂 SMB в туннеле — выключить?",
                      "Имена SMB-серверов пропадут с твоих устройств; по адресам серверы останутся доступны тебе")


def gateway_lan_text(state: dict) -> str:
    """Приглашение «🗺 Подсети»: заголовок с текущими парами «реальная · в
    туннеле», пустая строка, что прислать (список заменяется целиком; «-»
    — убрать все, только пока VPN-транзит выключен: с ним ввод «-» отвергнут)
    и что получится — адреса в туннеле как зеркало сети."""
    gw = state["gateway"]
    nets = gw.lan_subnets
    cur = lan_pairs(nets, state.get("lan_twins"), limit=16) if nets else "не заданы"   # «· сейчас …» без двоеточия
    # «из подсетей других шлюзов» — только при живой связи подсетей
    linked = bool(state.get("peer_nets"))
    access = ("только твоим устройствам и подсетям других шлюзов" if linked
              else "только твоим устройствам")
    lines = [ui.head(f"🗺 Подсети {slot_ref(state)}", meta=[f"сейчас {cur}"]), "",
             "Пришли все подсети шлюза через пробел, например <code>192.168.1.0/24</code> — список заменится "
             "целиком" + ("" if gw.lan_mode else "; «-» — убрать все"),
             "Из туннеля сеть открывается по адресам в туннеле — это зеркало сети в служебном пространстве "
             "сервера: два последних октета те же, что в реальных адресах. "
             f"Доступ через туннель — {access}"]
    if gw.lan_mode:
        lines.append("🔀 VPN-транзит работает для первой подсети в списке")
    lines.append(details(f"Если сеть, где сейчас твоё устройство, сама в {lan_space_range()}, адреса в "
                         "туннеле из неё не откроются"))
    return "\n".join(lines)


def gateway_lan_report(res: dict, state: dict) -> str:
    """Итог правки подсетей — первыми строками карточки; отказы — построчно
    с «• » (внутри одного отказа «;» не ставим — читалось бы одной кашей)."""
    parts = []
    kept = res.get("kept") or []
    shown = lan_pairs(kept, res.get("twins"), limit=8)
    parts.append(f"✅ Подсети {slot_ref(state)}: " + (shown if kept else "убраны"))
    rejected = res.get("rejected") or []
    if rejected:
        items = [f"• <code>{_e(raw[:40])}</code> — {addr_codes(why)}" for raw, why in rejected[:3]]
        if len(rejected) > 3:
            items.append(f"• и ещё {len(rejected) - 3}")
        parts.append("⚠️ Не принято:\n" + "\n".join(items))
    return "\n".join(parts)


def gateway_label_text(state: dict) -> str:
    gw = state.get("gateway")
    cur = f"«{_e(gw.label)}»" if gw is not None and gw.label else "не задана"
    return "\n".join([ui.head(f"✏️ Подпись {slot_ref(state)}", meta=[f"сейчас {cur}"]), "",
                      "Где стоит шлюз, одним-двумя словами: «дача», «офис». До 20 символов; «-» — убрать"])


def gateway_label_done(label: str) -> str:
    return f"✅ Подпись: {_e(label)}" if label else "✅ Подпись убрана"


def gateway_remove_ask(dev, *, state=None, other=None) -> str:
    """Три варианта: резервный; активный при живом резерве; последний."""
    name = _e(dev.name)
    if state and not state.get("active") and other is not None:
        return (f"➖ Снять {name} с резерва?\n"
                f"Резерва не останется: упадёт {slot_ref(other)} — российские сервисы станут открываться "
                f"с зарубежного адреса. Линк снимется, {name} станет обычным устройством")
    if state and state.get("active") and other is not None:
        return (f"➖ Снять {name} с роли шлюза?\n"
                f"Трафик сразу перейдёт на {slot_ref(other)} — российские сервисы начнут открываться с адреса "
                f"его сети, приложения могут попросить войти заново. Линк снимется, {name} станет обычным "
                "устройством")
    return (f"➖ Снять {name} с роли шлюза?\n"
            f"РФ-доступ выключится: чтобы вернуть, включи его и назначь новый шлюз. Линк снимется, {name} "
            "станет обычным устройством")


def gateway_removed(dev, now_active=None) -> str:
    if now_active is not None:
        return f"✅ {_e(dev.name)} больше не шлюз — стал обычным устройством"
    return f"✅ {_e(dev.name)} больше не шлюз — РФ-доступ выключен: чтобы вернуть, включи его и назначь новый шлюз"


def gateway_keys_ask(state: dict, live_standby: bool) -> str:
    """Диалог «🔑 Ключи линка» (по правилу подтверждений: вопрос и цена)."""
    lines = [f"🔑 Сменить ключи линка шлюза «{slot_name(state)}»?"]     # с подписью: два шлюза с одним именем различимы
    if state.get("active"):
        lines.append("Новые ключи уйдут шлюзу по каналу конфигурации — подтверди их в чате бота шлюза. "
                     "РФ-доступ прервётся на секунды; не поднимется — обе стороны вернут прежние.")
        if live_standby:
            lines.append("Есть живой резерв: сначала сделай его активным — смена пройдёт без обрыва")
    else:
        lines.append("Новые ключи уйдут шлюзу по каналу конфигурации — подтверди их в чате бота шлюза. "
                     "Не поднимется — обе стороны вернут прежние.")
    return "\n".join(lines)


GATEWAY_KEYS_PICK = "\n".join([
    ui.head("🔑 Ключи линка"),
    "Новые ключи, PSK и обфускация линка для того же устройства — если файл конфигурации шлюза утёк или "
    "обфускацию раскрыли. Выбери шлюз — он применит их после подтверждения в чате своего бота."])
GATEWAY_TOKEN_PICK = "\n".join([
    ui.head("🤖 Токен бота шлюза"),
    "Новый токен бота шлюза — когда прежний утёк и отозван в @BotFather. Можно и другого бота: вся "
    "настройка шлюза останется. Уйдёт шлюзу по каналу конфигурации, бот шлюза перезапустится на нём."])
GATEWAY_TOKEN_SENT = "🤖 Новый токен ушёл шлюзу — бот шлюза перезапустится на нём"
GATEWAY_TOKEN_QUEUED = "🤖 Новый токен записан — шлюз получит его по каналу конфигурации при подключении"


def gateway_token_prompt(name: str) -> str:
    return "\n".join([ui.head(f"🤖 Новый токен бота шлюза «{_e(name)}»"), "",
                      "Пришли токен вида <code>123456789:AA…</code> из @BotFather — прежнего бота или нового. "
                      "Уйдёт шлюзу по каналу конфигурации, бот шлюза перезапустится; сообщение с токеном сразу удалю."])


def gateway_token_refused(why: str) -> str:
    return f"⚠️ Токен не принят: {_e(why)}. Пришли токен ещё раз или нажми «✖️ Отмена»"


def gateway_token_new_bot(username: str) -> str:
    if not username:
        return "Бот шлюза изменился: нажми «Запустить» в чате с ним — без этого он не сможет тебе написать"
    return (f"Бот шлюза теперь @{_e(username)}: открой его и нажми «Запустить» — без этого он не сможет "
            "тебе написать")


GATEWAY_KEYS_SENT = "🔑 Новые ключи ушли шлюзу — подтверди их в чате бота шлюза"
# шлюз не на связи: ключи записаны и ждут его подключения — «ушли» было бы неправдой
GATEWAY_KEYS_QUEUED = ("🔑 Новые ключи записаны — шлюз получит их по каналу конфигурации при подключении; "
                       "подтверди их в чате бота шлюза")

