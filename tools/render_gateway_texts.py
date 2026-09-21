"""Отрисовать все экраны и уведомления резервного шлюза из кода на образцовых
данных — в stdout, на вычитку. Запуск: PYTHONPATH=. .venv/bin/python tools/render_gateway_texts.py > <файл вне репозитория>"""
import tempfile
import pathlib

from awgbot.core import config
from awgbot.infra import routing
from awgbot.infra.db import Database
from awgbot.domain.services import Services
from awgbot.bot import texts, keyboards as kb

db = Database(pathlib.Path(tempfile.mkdtemp()) / "t.db"); db.init_schema()
svc = Services(db)
cid = db.create_client(name="Админ", device_limit=0, period_start="2026-01-01",
                       period_end="2027-01-01", invite_code="A")
db.update_client_fields(cid, tg_id=config.ADMIN_ID)
d1 = db.create_device(cid, "NASPi", "PK1=", "S", "10.8.1.5", private_key="k")
d2 = db.create_device(cid, "Pi2", "PK2=", "S", "10.8.1.9", private_key="k")
db.gateway_add(d1, "awglink", 443, "10.99.99.0/30", slot_id=1)
db.gateway_add(d2, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
db.gateway_update(1, label="дом 1", home_subnets=["192.168.1.0/24"])
db.gateway_update(2, label="дом 2")
db.set_state("routing_active_gateway", "1")
db.set_state("routing_link_ok", "1")
db.set_state("gw_bundle_issued_at_1", "2026-09-12T21:40:00+03:00")
db.set_state("gw_bundle_issued_at_2", "2026-09-17T18:05:00+03:00")
db.set_state("routing_switched_at", "2026-09-14T03:12:00+03:00")
db.set_state("routing_gw_ping_1", "43 2026-09-18T10:00:00+03:00")
db.set_state("routing_gw_ping_2", "61 2026-09-18T10:00:00+03:00")
db.set_state("routing_gw_extip_1", "203.0.113.10 2026-09-18T10:00:00+03:00")
db.set_state("routing_gw_extip_2", "198.51.100.7 2026-09-18T10:00:00+03:00")
config.ROUTING_GW_INTERFACE = "awglink"
HS = {"awglink": 38, "awglink2": 51}
routing.link_handshake_age = lambda iface="": HS.get(iface)
routing.ping_peer = lambda *a, **k: 43
routing.link_peer_endpoint = lambda iface="": "203.0.113.10" if iface == "awglink" else "198.51.100.7"
svc._probe_slot = lambda g, active=False: "ok"
from awgbot.bot.texts import routing as _tr
_tr.vps_hostname = lambda: "AWG-SRV"

out = []
def h(t): out.append(f"\n## {t}\n")
def screen(title, text, markup=None, note=""):
    out.append(f"### {title}\n")
    if note: out.append(f"_{note}_\n")
    out.append("```html\n" + text + "\n```\n")
    if markup is not None:
        rows = [" | ".join(f"[{b.text}]" for b in row) for row in markup.inline_keyboard]
        out.append("Кнопки:\n```\n" + "\n".join(rows) + "\n```\n")
def notice(title, text):
    out.append(f"### {title}\n```html\n{text}\n```\n")

def states(active=1, ok=(True, True), down=(0, 0), up=(3, 3), hs=(38, 51)):
    HS["awglink"], HS["awglink2"] = hs
    st = svc.gateway_states()
    for x in st:
        i = x["gateway"].id
        x["active"] = (i == active)
        x["link_ok"] = ok[i - 1]
        x["down_ticks"] = down[i - 1]
        x["unavailable"] = down[i - 1] >= 5
        x["up_ticks"] = up[i - 1]
        x["ping_ms"] = (43 if i == 1 else 61) if ok[i - 1] else None
        x["ext_ip"] = "203.0.113.10" if i == 1 else "198.51.100.7"
        x["states"] = st
        act = next(y for y in st if y["gateway"].id == active)
        x["active_display"] = act["display"]
    return st

out.append("# Резервный шлюз — тексты на вычитку\n\nОтрисовано из кода v2.24.0 на образцовых данных: слот 1 «NASPi» (дом 1, предпочтительный), слот 2 «Pi2» (дом 2). HTML-теги — как уходят в Telegram.\n")

h("1. Раздел «Условная маршрутизация»")
st1 = [s for s in states() if s["gateway"].id == 1]
text = texts.settings_routing_text(True, (True, "ок")) + texts.settings_routing_gateway_block(st1)
screen("1.1 Один шлюз", text, kb.settings_routing(True, st1, can_add=True))
st = states()
text = texts.settings_routing_text(True, (True, "ок")) + texts.settings_routing_gateway_block(st)
screen("1.2 Два шлюза", text, kb.settings_routing(True, st, can_add=False))
text = texts.settings_routing_text(True, (True, "ок")) + texts.settings_routing_gateway_block([])
screen("1.3 Шлюз не назначен", text, kb.settings_routing(True, [], can_add=True))

h("2. Список шлюзов")
st = states()
screen("2.1 Оба в порядке", texts.gateway_list_text(st, db.get_state("routing_switched_at"), True),
       kb.gateway_list(st, can_add=False, failover_on=True))
st = states(ok=(True, False), down=(0, 28), up=(3, 0), hs=(38, None))
screen("2.2 Резерв лежит, автопереключение выключено", texts.gateway_list_text(st, "", False),
       kb.gateway_list(st, can_add=False, failover_on=False))
st = states(ok=(True, False), down=(0, 0), up=(3, 1))
screen("2.3 Резерв только назначен", texts.gateway_list_text(st, "", True),
       kb.gateway_list(st, can_add=False, failover_on=True))

h("3. Карточка шлюза")
st = states()
screen("3.1 Активный", texts.gateway_card_text(st[0], st), kb.gateway_card(st[0], back_to_list=True))
screen("3.2 Резервный", texts.gateway_card_text(st[1], st), kb.gateway_card(st[1], back_to_list=True))
st = states(ok=(True, False), down=(0, 28), up=(3, 0), hs=(38, None))
screen("3.3 Резервный лежит (хендшейка нет)", texts.gateway_card_text(st[1], st), kb.gateway_card(st[1], back_to_list=True))
st = states(ok=(True, False), down=(0, 28), up=(3, 0), hs=(38, 51))
screen("3.3а Резервный: линк жив, интернета за ним нет", texts.gateway_card_text(st[1], st), None)
st = states(ok=(False, True), down=(6, 0), up=(0, 3), hs=(None, 51))
screen("3.4 Активный лежит (условная маршрутизация выключена)", texts.gateway_card_text(st[0], st), kb.gateway_card(st[0], back_to_list=True))
db.gateway_update(2, home_subnets=["192.168.1.0/24"])
st = states()
screen("3.5 Совпадение домашних подсетей у двух шлюзов", texts.gateway_card_text(st[1], st), None)
db.gateway_update(2, home_subnets=[])

h("4. Карточка устройства-шлюза (из «Моих устройств»)")
st = states()
dev1 = db.get_device(d1); dev2 = db.get_device(d2)
screen("4.1 Активный", texts.gateway_device_card(dev1, st[0]), kb.gateway_device_actions(dev1, "back", slot=1))
screen("4.2 Резервный", texts.gateway_device_card(dev2, st[1]), kb.gateway_device_actions(dev2, "back", slot=2))

h("5. Назначение в слот")
screen("5.1 Первый шлюз (как было)", texts.GATEWAY_CHOOSE_INTRO, kb.gateway_choose_kind(True, 0))
screen("5.2 Резервный шлюз", texts.GATEWAY_STANDBY_CHOOSE_INTRO, kb.gateway_choose_kind(True, 0))
st = states()
screen("5.3 Заменить устройство (резервный слот)", texts.gateway_replace_intro(st[1]), kb.gateway_choose_kind(True, 2))
screen("5.4 Заменить устройство (активный слот)", texts.gateway_replace_intro(st[0]), kb.gateway_choose_kind(True, 1))
screen("5.5 Из моих устройств", texts.GATEWAY_PICK_INTRO, kb.gateway_pick([dev2], 0))
screen("5.6 Подтверждение: это шлюз (резервный слот)", texts.gateway_mark_ask(dev2, None, standby=True), kb.gateway_mark_confirm(dev2.id, 0))
screen("5.7 Подтверждение: замена в активном слоте", texts.gateway_mark_ask(dev2, dev1, replace_state=st[0]), kb.gateway_mark_confirm(dev2.id, 1))
screen("5.8 Новое устройство (слот 2)", texts.gateway_new_ask(2), kb.gateway_new_confirm(0))
screen("5.9 Токен бота первого шлюза", texts.gateway_ask_token(1), kb.settings_cancel("rt_gw"))
screen("5.10 Токен бота резервного шлюза", texts.gateway_ask_token(2), kb.settings_cancel("rt_gw"))
screen("5.11 Итог назначения — инструкция первого применения (слот 2)", texts.gateway_install_instructions(dev2, "awg-gw-bundle-awglink2.sh"), note="единственный путь назначения; следом — файл")
screen("5.12 Запасной путь: пересланный токен агента — итог", texts.gateway_claim_marked(dev1), note="только для токена от агента; файл шифрованный, для чата агента")

h("6. Переключение трафика")
st = states()
screen("6.1 Резерв в порядке", texts.gateway_switch_ask(st[1], st[0], True), kb.gateway_switch_confirm(2, True))
st = states(ok=(True, False), down=(28, 28), up=(3, 0), hs=(38, None))
screen("6.2 Резерв не отвечает", texts.gateway_switch_ask(st[1], st[0], False), kb.gateway_switch_confirm(2, False))

h("7. Домашние подсети и подпись")
st = states()
screen("7.1 Ввод подсетей", texts.gateway_home_text(st[1]), kb.gateway_slot_cancel(2))
screen("7.2 Отчёт после ввода", texts.gateway_home_report(
    {"kept": ["192.168.2.0/24"], "rejected": [("мусор", "не похоже на подсеть"), ("10.8.1.0/24", "это подсеть клиентов")], "conflict": None}, st[1]))
screen("7.3 Отчёт с совпадением", texts.gateway_home_report(
    {"kept": ["192.168.1.0/24"], "rejected": [], "conflict": db.gateway(1)}, st[1]))
screen("7.4 Подпись", texts.gateway_label_text(st[1]), kb.gateway_slot_cancel(2))

h("8. Убрать шлюз")
st = states()
screen("8.1 Резервный", texts.gateway_remove_ask(dev2, state=st[1], other=st[0]), kb.gateway_remove_confirm(2))
screen("8.2 Активный при живом резерве", texts.gateway_remove_ask(dev1, state=st[0], other=st[1]), kb.gateway_remove_confirm(1))
screen("8.3 Последний", texts.gateway_remove_ask(dev1, state=st[0], other=None), kb.gateway_remove_confirm(1))
screen("8.4 Итог: убран резервный, трафик через другой", texts.gateway_removed(dev2, st[0]))
screen("8.5 Итог: убран последний", texts.gateway_removed(dev1, None))

h("9. Конфигурация шлюза")
head = texts.ROUTING_BUNDLE_INTRO.replace("Конфигурация шлюза</b>", f"Конфигурация шлюза {texts.slot_short(st[1])}</b>", 1)
screen("9.1 Экран перед выпуском (слот 2)", head, kb.settings_routing_bundle(2))

h("9а. Мониторинг и резервирование")
info = svc.routing_monitor_info()
screen("9а.1 Экран", texts.routing_monitor_text(info), kb.settings_routing_monitor(info),
       note="⚙️ Настройки → Условная маршрутизация → последним пунктом перед «Назад»")

h("10. Уведомления админу")
g1, g2 = db.gateway(1), db.gateway(2)
notice("10.1 Автоматическое переключение (шлюз молчит)", svc._txt_rt_switched(g1, g2, routing.PROBE_DOWN))
notice("10.2 Автоматическое переключение (туннель жив, интернета за ним нет)", svc._txt_rt_switched(g1, g2, routing.PROBE_NO_PATH))
notice("10.3 Прежний активный ожил, остаётся в резерве", svc._txt_rt_standby_up(g1, g2))
notice("10.4 Оба лежат", svc._txt_rt_gw_down(g1, [g2]))
notice("10.5 Оба лежат, у активного туннель жив", svc._txt_rt_gw_no_path(g1, [g2]))
notice("10.4а Активный лежит, резерва нет (один шлюз)", svc._txt_rt_gw_down(g1))
notice("10.6 Активный снова в строю", svc._txt_rt_gw_up(g1))
notice("10.7 Резерв молчит (без звука)", svc._txt_rt_standby_down(g2, g1, 20))
notice("10.8 Резерв снова отвечает", svc._txt_rt_standby_up(g2, g1))
db.set_state("routing_switched_at", __import__("awgbot.util.timeutil", fromlist=["x"]).to_iso(
    __import__("awgbot.util.timeutil", fromlist=["x"]).now() - __import__("datetime").timedelta(minutes=4)))
notice("10.9 Второе переключение подряд не сделано", svc._txt_rt_switch_refused(g2))
config.ROUTING_ENABLED = True
db.set_state("gw_bundle_ssh_allow_2", "10.8.1.99")
notice("10.10 Дрейф состава устройств админа (бандл слота)", svc.gw_bundle_drift_notes()[0].text)
svc._probe_slot = lambda g, active=False: "ok" if g.id == 1 else "down"
notice("10.11 Замечание при запуске: резерв не отвечает", "\n".join(svc.routing_startup_warnings()))
svc._probe_slot = lambda g, active=False: "down"
notice("10.11а Замечания при запуске: оба не отвечают", "\n".join(svc.routing_startup_warnings()))
notice("10.12 Ответы на кнопки (всплывашки)", "Трафик идёт через Pi2 (дом 2)\nПинг с AWG-SRV: 43 мс\nШлюз «Pi2» (дом 2) не отвечает\nПредпочтительный: NASPi\nПредпочтительный: снят\nАвтопереключение включено / выключено")

print("\n".join(out))
