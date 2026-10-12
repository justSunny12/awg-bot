"""Снимки агента шлюза: VPN-транзит и свои списки (W10–W14)."""
from __future__ import annotations

from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import CancelCB, GwCB, PageCB

from tests.screens.shots_gateway_common import _TICK_LAN, _lan, _status_lan
from tests.screens.shots_gateway_agent import _shot


_OWN_ITEMS = [("vpn", "example.com"), ("vpn", "example.net"), ("ru", "sber.ru")]


_OWN_SYNCED = {"active": True, "state": "synced", "pending": 0, "err": "", "rej": [], "synced": True}


def _lan_rm(items, dom: str) -> GwCB:
    """Кнопка «➖ домен» — номер в отсортированном списке и метка записи."""
    srt = kb.lan_own_sorted(items)
    i = next(n for n, (_k, d) in enumerate(srt) if d == dom)
    return GwCB(action="lan_rm", val=f"{i}.{kb.lan_own_tag(*srt[i])}")


_MANY_OWN = [("vpn", f"site{n:02d}.example.com") for n in range(1, 13)] + [("ru", "sber.ru")]


_HUGE = [f"very-long-subdomain-name-{i:04d}.example-shop.com" for i in range(80)]


LAN = [
    # ── W10–W14 VPN-транзит и свои списки ───────────────────────────────────
    _shot("gw.lan.empty.shared", press=[GwCB(action="lan")], tick=_status_lan(lan=_lan(own_vpn=0, own_ru=0)),
          own=_OWN_SYNCED, title="своих доменов нет, списки общие — обе подсказки первыми под «подробнее»"),
    _shot("gw.lan.bare", press=[GwCB(action="lan")], own_items=[("vpn", "a.com")],
          tick=_status_lan(lan=_lan(uplink="", updated_at="", own_vpn=1, own_ru=0,
                                    svc={"active": True, "own": [], "peer": ["naspi5"], "ever": True})),
          title="аплинк не назван, списки ещё не обновлялись, своих «напрямую» нет, SMB только извне"),
    _shot("gw.lan.smb_off", press=[GwCB(action="lan")], own_items=_OWN_ITEMS,
          tick=_status_lan(lan=_lan(svc={"active": False, "own": ["nas"], "peer": ["nas-2"], "ever": True})),
          title="связь подсетей не действует — строки 🗂 нет"),
    _shot("gw.lan", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_OWN_ITEMS, own=_OWN_SYNCED,
          title="три своих домена, списки общие и синхронизированы"),
    _shot("gw.lan.virtual", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_OWN_ITEMS, own=_OWN_SYNCED,
          lan_virtual=("10.240.1.0/24", "10.240.2.0/24"),
          title="сервер назвал в role адреса в туннеле своей сети — строка под интерфейсом"),
    _shot("gw.lan.pending", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_OWN_ITEMS,
          own={"active": True, "state": "no_link", "pending": 2},
          title="правки ждут синхронизации — нет связи с сервером"),
    _shot("gw.lan.pending.online", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_OWN_ITEMS,
          own={"active": True, "state": "pending", "pending": 1},
          title="одна правка ждёт — связь есть, сервер ещё не ответил"),
    _shot("gw.lan.failed", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_OWN_ITEMS,
          own={"active": True, "state": "failed", "err": "dnsmasq отверг конфиг <vpn.conf>"},
          title="свои списки не применились (ошибка с хоста — экранирована)"),
    _shot("gw.lan.broken", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_OWN_ITEMS,
          own={"active": True, "state": "failed", "err": "ошибка записи файла: нет места на диске"},
          title="поломка записи файла на шлюзе — отдельной фразой, без второго двоеточия"),
    _shot("gw.lan.rejected", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_OWN_ITEMS,
          own={"active": True, "state": "rejected", "rej": [["awg-srv.example.com", "это хост сервера"]]},
          title="сервер не принял домен"),
    _shot("gw.lan.many", press=[GwCB(action="lan")], tick=_TICK_LAN, own_items=_MANY_OWN, own=_OWN_SYNCED,
          title="13 своих доменов — первая страница"),
    _shot("gw.lan.page2", press=[GwCB(action="lan"),
                                 PageCB(screen="lanlist", ref=0, page=1, back=GwCB(action="lan").pack())],
          tick=_TICK_LAN, own_items=_MANY_OWN, own=_OWN_SYNCED, title="листание: вторая страница"),
    _shot("gw.lan.add.ask", press=[GwCB(action="lan"), GwCB(action="lan_add")], tick=_TICK_LAN,
          title="приглашение «➕ В туннель»"),
    _shot("gw.lan.ru.ask", press=[GwCB(action="lan"), GwCB(action="lan_ru")], tick=_TICK_LAN,
          title="приглашение «➕ Напрямую»"),
    _shot("gw.lan.add.done", press=[GwCB(action="lan"), GwCB(action="lan_add")],
          text="example.org example.com localhost", tick=_TICK_LAN, own_items=_OWN_ITEMS,
          title="итог первой строкой: добавлен / уже в списке / не домен"),
    _shot("gw.lan.add.online", press=[GwCB(action="lan"), GwCB(action="lan_add")], text="example.org",
          tick=_TICK_LAN, own_items=_OWN_ITEMS, own=_OWN_SYNCED, channel="active", standby=True,
          title="итог с хвостом «синхронизируются» — канал на связи"),
    _shot("gw.lan.add.later", press=[GwCB(action="lan"), GwCB(action="lan_ru")], text="shop.ru",
          tick=_TICK_LAN, own_items=_OWN_ITEMS, own=_OWN_SYNCED, channel="offline",
          title="итог с хвостом «будут синхронизированы» — канал включён, связи нет"),
    _shot("gw.lan.add.already", press=[GwCB(action="lan"), GwCB(action="lan_add")], text="example.com",
          tick=_TICK_LAN, own_items=_OWN_ITEMS, own=_OWN_SYNCED, channel="active", standby=True,
          title="«уже в списке» при живом канале — ничего не изменилось, хвоста нет"),
    _shot("gw.lan.add.huge", press=[GwCB(action="lan"), GwCB(action="lan_add")], text=" ".join(_HUGE),
          tick=_TICK_LAN, title="вставили 80 длинных доменов — итог обрезан, экран под ним в том же сообщении"),
    _shot("gw.lan.ru.solo", press=[GwCB(action="lan"), GwCB(action="lan_ru")], text="gosuslugi.ru",
          tick=_TICK_LAN, own_items=_OWN_ITEMS, own=_OWN_SYNCED, channel="active", standby=False,
          title="другого шлюза нет — итог без хвоста"),
    _shot("gw.lan.add.blank", press=[GwCB(action="lan"), GwCB(action="lan_add")], text=" ",
          tick=_TICK_LAN, title="пустой ввод — переспрос"),
    _shot("gw.lan.add.failed", press=[GwCB(action="lan"), GwCB(action="lan_add")], text="example.org",
          tick=_TICK_LAN, own_items=_OWN_ITEMS, lan_fail="dnsmasq не перезапустился — откатываю",
          title="скрипт отказал"),
    _shot("gw.lan.cancel", press=[GwCB(action="lan"), GwCB(action="lan_add"), CancelCB(kind="set_lan")],
          tick=_TICK_LAN, own_items=_OWN_ITEMS, title="«✖️ Отмена» под приглашением — экран на месте"),
    _shot("gw.lan.rm", press=[GwCB(action="lan"), _lan_rm(_OWN_ITEMS, "example.net")], tick=_TICK_LAN,
          own_items=_OWN_ITEMS, own=_OWN_SYNCED, channel="active", standby=True,
          title="«➖ домен» — сразу, всплывашка с хвостом синхронизации"),
    _shot("gw.lan.rm.later", press=[GwCB(action="lan"), _lan_rm(_OWN_ITEMS, "example.net")], tick=_TICK_LAN,
          own_items=_OWN_ITEMS, own=_OWN_SYNCED, channel="offline",
          title="«➖» при канале без связи — «будут синхронизированы»"),
    _shot("gw.lan.rm.solo", press=[GwCB(action="lan"), _lan_rm(_OWN_ITEMS, "example.net")], tick=_TICK_LAN,
          own_items=_OWN_ITEMS, own=_OWN_SYNCED, channel="active", standby=False,
          title="«➖», другого шлюза нет — итог без хвоста"),
    _shot("gw.lan.rm.late", press=[GwCB(action="lan"), _lan_rm(_OWN_ITEMS, "example.net")], tick=_TICK_LAN,
          own_items=_OWN_ITEMS, late_answer=True,
          title="скрипт ответил дольше, чем Telegram держит нажатие, — итог сообщением, экран перерисован"),
    _shot("gw.lan.rm.stale", press=[GwCB(action="lan"), GwCB(action="lan_rm", val="0.deadbeef")],
          tick=_TICK_LAN, own_items=_OWN_ITEMS, title="список изменился — переспрос, экран заново"),
    _shot("gw.lan.rm.failed", press=[GwCB(action="lan"), _lan_rm(_OWN_ITEMS, "sber.ru")], tick=_TICK_LAN,
          own_items=_OWN_ITEMS, lan_fail="<code>sber.ru</code>: dnsmasq не перезапустился — откатываю",
          title="скрипт отказал — alert без разметки"),
    _shot("gw.lan.router", press=[GwCB(action="lan"), GwCB(action="lan_router")], tick=_TICK_LAN,
          title="❓ Роутер: вкладка MikroTik"),
    _shot("gw.lan.router.ow", press=[GwCB(action="lan"), GwCB(action="lan_router", val="ow")],
          tick=_TICK_LAN, channel="active", title="❓ Роутер: вкладка OpenWrt, имя слота с сервера"),
    _shot("gw.lan.router.unknown_tab", press=[GwCB(action="lan"), GwCB(action="lan_router", val="zz")],
          tick=_TICK_LAN, title="вкладка из чужого колбэка — рецепт MikroTik"),
    _shot("gw.lan.router.named", press=[GwCB(action="lan"), GwCB(action="lan_router")], tick=_TICK_LAN,
          channel="active", slot_name="NASPi (<дача>)", title="имя слота с сервера экранировано в заголовке"),
]
