"""
Документ конфигурации слота на стороне ВПС (domain/services/gwchannel/cfg.py)
и его доставка каналом (runtime/linkserver: deliver_cfg, приём cfg_ack).

Сервер не хранит «что выдал»: желаемое каждый раз вычисляется из тех же
источников, из которых собирается файл первого применения, — подсеть
клиентов, устройства админа, локальная сеть слота, порт канала, конфиг линка
шлюзовой стороны (`/root/gw-<линк>.conf` с поправками), почта, фраза, имя
сервера. Доставка — по расхождению отпечатков снимка с желаемым; раздел,
который шлюз держит в `wait` или `declined` с тем же отпечатком, не шлётся.

Цена ошибок: сервер, который шлёт документ каждым тактом, перезапускает
обвязку и линк шлюза без конца; сервер, который не шлёт, оставляет шлюз со
старыми подсетями, пока карточка уверяет, что всё доехало; документ, который
шлюз заведомо отвергнет, — вечный отказ вместо доставки; вопрос о почте,
задаваемый заново после «Оставить своё», — спам в чате бота шлюза.

Хост подменён: файлы `/root/gw-<линк>.conf` и живой конфиг линка ВПС читаются
из временного каталога (класс `_Host`), имя хоста — подстановкой. Слушатель
канала — боевой LinkServer на петле, агент — подписанные строки `_Gw`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket

import pytest

from awgbot.core import config, settings
from awgbot.domain import gwcfg
from awgbot.domain.services.gwchannel import cfg as cfgmod
from awgbot.runtime import linkserver
from awgbot.util import gwlink, wgconf
from tests.integration.test_gwlink_channel import _Gw, _free_port, _until
from tests.unit.test_wgconf_allowed import LINK

pytestmark = pytest.mark.integration

ADMIN = config.ADMIN_ID
PRIV = base64.b64encode(os.urandom(32)).decode()
KEY = gwlink.channel_key(PRIV)
MAIL = {"email_login": "box@example.org", "email_password": "DUMMY-pass"}
MAIL_CONF = {"email.imap_host": "imap.example.org", "email.imap_port": 993,
             "email.smtp_host": "smtp.example.org", "email.smtp_port": 465}


class _Host:
    """Файлы ВПС, из которых сервер собирает конфиг линка шлюза: источник
    `/root/gw-<линк>.conf` (его пишет скрипт линка) и живой конфиг линка ВПС с
    `ListenPort`. Чтение в модуле cfg перенаправлено во временный каталог;
    настройки — поверх настоящих, словарём `conf`."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "root"
        self.awg = tmp_path / "awg"
        self.root.mkdir()
        self.awg.mkdir()
        monkeypatch.setattr(config, "AWG_DIR", str(self.awg))
        real_open = open

        def _open(path, *a, **kw):
            p = str(path)
            if p.startswith("/root/"):
                p = str(self.root / p[len("/root/"):])
            return real_open(p, *a, **kw)
        monkeypatch.setattr(cfgmod, "open", _open, raising=False)
        self.conf: dict = {"app.network.subnet_cidr": "10.8.1.0/24", "app.network.server_host": "",
                           "app.client_config.keepalive_seconds": "25-35"}
        real_get = settings.get
        monkeypatch.setattr(settings, "get", lambda k, d=None: self.conf[k] if k in self.conf else real_get(k, d))
        self.hostname = "awg-srv"
        monkeypatch.setattr(socket, "gethostname", lambda: self.hostname)
        self.source(LINK)
        self.listen(51821)

    def source(self, text: str | None) -> None:
        """Источник конфига линка шлюзовой стороны; None — файла нет."""
        path = self.root / "gw-awglink.conf"
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text, encoding="utf-8")

    def listen(self, port: int | None) -> None:
        """Живой конфиг линка ВПС; None — файла нет."""
        path = self.awg / "awglink.conf"
        if port is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(f"[Interface]\nListenPort = {port}\nPrivateKey = DUMMY\n", encoding="utf-8")


@pytest.fixture()
def host(tmp_path, monkeypatch):
    return _Host(tmp_path, monkeypatch)


@pytest.fixture()
def slot(services, fake_awg, fake_routing, make_active_client, monkeypatch, host):
    """Слот шлюза с VPN-транзитом и локальной подсетью; маршрутизация включена;
    снимка и сессии нет."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.add_device(admin.id, "iPhone")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.gateway_set_lan_subnets(1, "192.168.68.0/24")
    services.gateway_set_lan_mode(1, True)
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1" if g.lan_mode else "")
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    flags = {"app.routing.enabled": True, "app.routing.peer_nets.enabled": False}
    real = settings.get_bool
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: flags[k] if k in flags else real(k, d))
    services.flags = flags
    return services.db.gateway(1)


def _gw(services):
    return services.db.gateway(1)


def _neighbour(services, nets: list[str]) -> None:
    """Второй слот с VPN-транзитом — связь подсетей его часть: слоту 1 едет
    префикс слота 2 в туннеле (10.241.0.0/16), какими бы ни были его
    подсети. Пишется прямо в БД: ввод подсетей шире /16 отказывает, а такие
    слоты остались от прежних версий."""
    admin = services.db.get_client_by_tg(ADMIN)
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.db.gateway_update(2, lan_subnets=nets, lan_mode=1)


def _mail_on(services, host) -> None:
    for k, v in MAIL.items():
        services.db.set_state(k, v)
    host.conf.update(MAIL_CONF)


def _have(services, **over) -> dict:
    """Поле cfg снимка, когда на шлюзе стоит ровно желаемое, — с правками."""
    doc = services.gwcfg_doc(_gw(services))
    return {"gen": doc["gen"], **doc["prints"], **over}


def _snap(services, cfg: dict | None, *, bundle: dict | None = None) -> None:
    body = {"agent_version": "3.3.0", "rev": 1}
    if cfg is not None:
        body["cfg"] = cfg
    if bundle is not None:
        body["bundle"] = bundle
    services.gwlink_snapshot_in(1, body, 1, True)


def _parsed(text: str) -> wgconf.Conf:
    return wgconf.parse(text)


# ── желаемое: env ────────────────────────────────────────────────────────────

def test_env_comes_from_the_same_sources_as_the_link_script(services, slot):
    """Раздел env считается из тех же источников, что окружение скрипта линка
    и обвязки (_lan_env) — разойдись они, канал довозил бы одно, а скрипт
    ставил другое. Имена — документа (LAN_*), значения те же, и агент обязан
    их принять."""
    env = services.gwcfg_env(slot)
    lan = services._lan_env(slot)
    assert tuple(env) == gwcfg.ENV_KEYS, f"состав env не по таблице документа: {list(env)}"
    assert len(env["ADMIN_IPS"].split()) == 2, env
    assert env["LAN_SUBNETS"] == lan["LAN_SUBNETS"] == "192.168.68.0/24"
    assert env["LAN_MODE"] == lan["LAN_MODE"] == "1"
    assert env["RESOLVER"] == lan["RESOLVER"] == "10.9.1.1"
    assert env["PEER_LAN_NETS"] == lan["PEER_LAN_NETS"] == ""
    assert env["LINK_CHANNEL_PORT"] == str(gwlink.channel_port())
    assert env["CLIENT_SUBNET"] == "10.8.1.0/24"
    assert gwcfg.check_env(env) == env, "агент отверг бы env, который хочет сервер"


def test_the_client_subnet_follows_the_network_setting(services, slot, host):
    """Подсеть клиентов — тот же ключ, что вшивает скрипт линка
    (`network.subnet_cidr`): она и в env, и первой записью AllowedIPs линка."""
    host.conf["app.network.subnet_cidr"] = "10.20.30.0/24"
    want = services.gwcfg_desired(slot)
    assert want["env"]["CLIENT_SUBNET"] == "10.20.30.0/24"
    allowed = _parsed(want["link"]["conf"]).get("Peer", "AllowedIPs")
    assert allowed.startswith("10.20.30.0/24"), allowed


def test_a_neighbours_subnets_and_the_lan_mode_go_into_env(services, slot):
    """Сосед с VPN-транзитом — его префикс в туннеле в env слота; VPN-транзит
    снят — режим, резолвер и подсети соседей уходят вместе с ним."""
    _neighbour(services, ["192.168.70.0/24"])
    assert services.gwcfg_env(_gw(services))["PEER_LAN_NETS"] == "10.241.0.0/16"
    services.gateway_set_lan_mode(1, False)
    env = services.gwcfg_env(_gw(services))
    assert env["LAN_MODE"] == "0" and env["RESOLVER"] == "" and env["PEER_LAN_NETS"] == "", env


# ── желаемое: конфиг линка ───────────────────────────────────────────────────

def test_the_link_conf_gets_the_server_host_and_the_live_listen_port(services, slot, host):
    """Порт Endpoint — из живого ListenPort ВПС, хост — `network.server_host`,
    как при сборке файла: порт сменили, а источник остался прежним — шлюз
    стучался бы в закрытый порт. Ключи, PSK и обфускация — из источника как
    есть (их меняет только переход), штамп контракта — первой строкой, и
    агент принимает это своей же проверкой со стоящими ключами."""
    host.conf["app.network.server_host"] = "vpn.example.org"
    host.listen(51999)
    text = services.gwcfg_link_conf(slot, services.gwcfg_env(slot))
    conf = _parsed(text)
    assert conf.get("Peer", "Endpoint") == "vpn.example.org:51999", text
    assert text.splitlines()[0] == "# awg-bot: контракт линка 1", "штамп контракта потерян"
    src = _parsed(LINK)
    for section, key in (("Interface", "PrivateKey"), ("Peer", "PublicKey"), ("Peer", "PresharedKey"),
                         ("Interface", "Jc"), ("Interface", "H4"), ("Interface", "Address")):
        assert conf.get(section, key) == src.get(section, key), f"{key} не тот, что в источнике"
    # стоящее на шлюзе — целиком, как его собирает агент (cfg_link_fixed): ключи, PSK и
    # обфускация; проверка двусторонняя — недостающий в стоящем ключ документ добавить не вправе
    fixed = {k: v for _s, items in src.sections for k, v in items
             if k in ("PrivateKey", "PublicKey", "PresharedKey") or (k[:1] in "JSHI" and k[1:].isalnum())}
    gwcfg.check_link(text, services.gwcfg_env(slot), link_cidr=slot.link_cidr, address="10.99.99.2/30",
                     fixed=fixed)
    assert services.gwcfg_link_error(1) == ""


def test_a_bad_server_host_keeps_the_host_from_the_source(services, slot, host):
    """В `network.server_host` мусор (пробел, точка с запятой) — в конфиг он не
    идёт: остаётся хост источника, порт по-прежнему живой."""
    host.conf["app.network.server_host"] = "vpn.example.org; reboot"
    text = services.gwcfg_link_conf(slot, services.gwcfg_env(slot))
    assert _parsed(text).get("Peer", "Endpoint") == "vps.example.org:51821", text


def test_without_the_live_conf_the_slot_port_is_used(services, slot, host):
    """Живого конфига линка нет (скрипт линка ещё не отработал) — порт слота."""
    host.listen(None)
    text = services.gwcfg_link_conf(slot, services.gwcfg_env(slot))
    assert _parsed(text).get("Peer", "Endpoint") == "vps.example.org:443", text


def test_allowed_ips_are_built_by_the_rule_not_copied_from_the_source(services, slot, host):
    """`AllowedIPs = 0.0.0.0/0` в источнике (руками или подложено) в документ
    не попадает: записи — ровно подсеть клиентов, /30 линка и подсети соседей."""
    host.source(LINK.replace("AllowedIPs = 10.8.1.0/24, 10.99.99.0/30", "AllowedIPs = 0.0.0.0/0"))
    text = services.gwcfg_link_conf(slot, services.gwcfg_env(slot))
    assert _parsed(text).get("Peer", "AllowedIPs") == "10.8.1.0/24, 10.99.99.0/30", text
    _neighbour(services, ["192.168.70.0/24"])
    gw = _gw(services)
    text = services.gwcfg_link_conf(gw, services.gwcfg_env(gw))
    assert _parsed(text).get("Peer", "AllowedIPs") == "10.8.1.0/24, 10.99.99.0/30, 10.241.0.0/16", text


@pytest.mark.parametrize("value, want", [("20-30", "20-30"), (25, "25"), ("каждые 25", "25-35"), ("", "25-35")])
def test_the_link_keepalive_follows_the_client_config(services, slot, host, value, want):
    """Ритм keepalive линка — тот же, что у клиентских пиров; мусор в настройке
    не уходит в конфиг (агент отверг бы его), остаётся «25-35»."""
    host.conf["app.client_config.keepalive_seconds"] = value
    text = services.gwcfg_link_conf(slot, services.gwcfg_env(slot))
    assert _parsed(text).get("Peer", "PersistentKeepalive") == want, text


def test_without_the_source_the_link_is_left_out_and_the_reason_is_kept(services, slot, host):
    """Источника нет — раздела link в документе нет (своё на шлюзе не трогаем),
    причина лежит для карточки; источник вернулся — причина снята."""
    host.source(None)
    assert services.gwcfg_desired(slot)["link"] is None
    assert "не прочитан" in services.gwcfg_link_error(1), services.gwcfg_link_error(1)
    host.source(LINK)
    assert services.gwcfg_desired(slot)["link"] is not None
    assert services.gwcfg_link_error(1) == "", "причина отказа пережила починку"


def test_a_source_with_a_root_command_is_not_passed_on(services, slot, host):
    """PostUp в источнике awg-quick исполнил бы на шлюзе от root. Сервер не
    передаёт такой конфиг дальше — раздела нет, причина названа."""
    host.source(LINK.replace("[Peer]", "PostUp = /bin/sh -c id\n\n[Peer]"))
    assert services.gwcfg_desired(slot)["link"] is None
    assert services.gwcfg_link_error(1), "причина не записана"


def test_the_document_carries_prefixes_and_the_link_passes_the_agents_check(services, slot, host):
    """Подсети соседа в документ слота не идут вовсе — только префикс его слота
    в туннеле: даже подсеть шире /16, оставшаяся от прежних версий, больше не
    роняет конфиг линка. Конфиг линка с префиксом проходит ту же проверку,
    что делает агент (общий модуль)."""
    _neighbour(services, ["172.16.0.0/12"])
    gw = _gw(services)
    env = services.gwcfg_env(gw)
    assert env["PEER_LAN_NETS"] == "10.241.0.0/16"
    link = services.gwcfg_desired(gw)["link"]
    assert link is not None and services.gwcfg_link_error(1) == ""
    src = _parsed(LINK)
    fixed = {k: v for _s, items in src.sections for k, v in items
             if k in ("PrivateKey", "PublicKey", "PresharedKey") or (k[:1] in "JSHI" and k[1:].isalnum())}
    gwcfg.check_link(link["conf"], env, link_cidr=gw.link_cidr, address="10.99.99.2/30", fixed=fixed)


def test_neighbour_subnets_do_not_touch_the_document_only_its_lan_mode_does(services, slot, host):
    """Подсети соседей не зависят от подсетей соседей: правка подсетей слота 2
    документ слота 1 не меняет (ему ничего не едет), выключение VPN-транзита у
    слота 2 — меняет: связь подсетей с ним кончилась."""
    _neighbour(services, ["192.168.70.0/24"])
    before = services.gwcfg_doc(_gw(services))["hash"]
    services.gateway_set_lan_subnets(2, "192.168.71.0/24")
    assert services.gwcfg_doc(_gw(services))["hash"] == before
    services.gateway_set_lan_mode(2, False)
    assert services.gwcfg_env(_gw(services))["PEER_LAN_NETS"] == ""
    assert services.gwcfg_doc(_gw(services))["hash"] != before


def test_a_wide_neighbour_subnet_does_not_block_the_rest_of_the_document(services, slot):
    """Остальные разделы обязаны доехать: сервер не шлёт документ, который
    шлюз заведомо отвергнет целиком. Подсеть соседа шире /16 (данные прежних
    версий) в env слота больше не попадает — там префикс его слота, и
    документ проходит проверку шлюза целиком."""
    _neighbour(services, ["172.16.0.0/12"])
    gw = _gw(services)
    services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1"])
    _snap(services, _have(services, env="0" * 16))
    body = services.gwcfg_due(gw)
    assert body is not None, "расхождение есть, а документа нет"
    parsed = gwcfg.parse(body)
    gwcfg.check_doc(parsed, link_cidr=gw.link_cidr, address="10.99.99.2/30")



def test_a_bad_env_is_shown_as_the_whole_document_not_the_link(services, slot, monkeypatch):
    """Раздел env не прошёл проверку — документа нет вовсе, не одного конфига
    линка: карточка говорит «Конфигурация не собрана», а прошлая причина линка
    рядом не всплывает. Починили env — строка уходит."""
    from awgbot.bot.texts.routing.card import _cfg_lines
    gw = _gw(services)
    services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1"])
    _snap(services, _have(services))
    services._gwcfg_link_error_set(1, "прошлая причина")
    real = services.gwcfg_env
    monkeypatch.setattr(services, "gwcfg_env", lambda g: {**real(g), "CLIENT_SUBNET": "10.8.1.1/24"})
    assert all(v is None for v in services.gwcfg_desired(gw).values())
    card = services.gwcfg_card(gw, services.gwlink_snapshot(1))
    assert card["env_err"] and card["link_err"] == "", card
    warns = _cfg_lines(card, {})
    assert any(w.startswith("⚠️ Конфигурация для шлюза не собрана: ") for w in warns), warns
    assert not any("Конфиг линка" in w for w in warns), warns
    monkeypatch.setattr(services, "gwcfg_env", real)
    services.gwcfg_desired(gw)
    assert services.gwcfg_card(gw, services.gwlink_snapshot(1))["env_err"] == "", "причина пережила починку"


# ── желаемое: почта, имя сервера, аплинк, токен ──────────────────────────────

def test_mail_is_absent_without_a_mailbox_and_is_just_the_mailbox_with_one(services, slot, host):
    """Почты на ВПС нет — раздела нет: своё на шлюзе не трогается. Есть —
    ящик и серверы, и только они: подсказки «куда уходят бэкапы» больше нет
    (своих копий у шлюза нет), канал бэкапов ВПС документ не сдвигает."""
    assert services.gwcfg_mail() is None
    _mail_on(services, host)
    mail = services.gwcfg_mail()
    assert mail == {"login": "box@example.org", "password": "DUMMY-pass", "imap_host": "imap.example.org",
                    "imap_port": 993, "smtp_host": "smtp.example.org", "smtp_port": 465}
    gwcfg.check_mail(mail)
    before = services.gwcfg_doc(slot)
    host.conf["app.scheduler.backup_channel"] = "email"
    assert services.gwcfg_mail() == mail
    assert services.gwcfg_doc(slot)["hash"] == before["hash"], "канал бэкапов ВПС сдвинул документ"


def test_the_backup_phrase_is_not_in_the_document(services, slot, host):
    """Фраза бэкапов сервера на шлюз не едет: своих копий у шлюза нет — ни
    раздела, ни отпечатка; смена фразы на ВПС документ не сдвигает."""
    _mail_on(services, host)
    assert not hasattr(services, "gwcfg_backup")
    before = services.gwcfg_doc(slot)
    assert "backup" not in before["sections"] and "backup" not in before["prints"]
    services.db.set_state("backup_passphrase", "DUMMY phrase long enough")
    after = services.gwcfg_doc(slot)
    assert after["hash"] == before["hash"] and after["gen"] == before["gen"], "фраза ВПС сдвинула документ"
    assert "DUMMY phrase long enough" not in str(gwcfg.build(after["gen"], after["hash"], after["sections"]))


def test_the_server_name_is_the_host_name(services, slot, host):
    assert services.gwcfg_server() == {"name": "awg-srv"}
    host.hostname = ""
    assert services.gwcfg_server() is None, "пустое имя ушло бы на шлюз"


def test_uplink_and_token_are_not_in_this_document(services, slot, host):
    """Агент этапа знает только cfg1: аплинк и токен — отдельные возможности.
    Их нет ни в теле документа, ни в сверке — отпечаток токена в снимке не
    рождает расхождения."""
    _mail_on(services, host)
    services.db.set_state("backup_passphrase", "DUMMY phrase long enough")
    want = services.gwcfg_desired(slot)
    assert want["uplink"] is None and want["token"] is None
    body = gwcfg.build(1, "ab" * 8, want)
    assert "uplink" not in body and "token" not in body, sorted(body)
    assert {"env", "link", "mail", "server"} <= set(body) and "backup" not in body, sorted(body)
    _snap(services, _have(services, token="ab" * 8, uplink="cd" * 8))
    assert services.gwcfg_due(slot) is None, "отпечаток токена или аплинка вошёл в сверку"


def test_the_document_keeps_no_private_key_of_the_server(services, slot):
    """Приватный ключ ВПС в документ не попадает — в канал уходит только
    шлюзовая сторона линка."""
    doc = services.gwcfg_doc(slot)
    assert PRIV not in json.dumps(doc["sections"], ensure_ascii=False)


# ── поколение ────────────────────────────────────────────────────────────────

def test_the_generation_grows_only_when_the_document_changes(services, slot):
    """Номер растёт на каждую смену документа и только на неё: карточку
    открывают часто, и «конфигурация №18» превращалась бы в «№4000». Возврат к
    прежнему значению — тоже смена (новый номер, а не прежний)."""
    gw = _gw(services)
    first = services.gwcfg_doc(gw)
    commits = services.db.commits
    again = services.gwcfg_doc(gw)
    assert (again["gen"], again["hash"]) == (first["gen"], first["hash"])
    assert services.db.commits == commits, "повторный расчёт того же документа что-то записал"
    services.gateway_set_lan_subnets(1, "192.168.70.0/24")
    second = services.gwcfg_doc(_gw(services))
    assert second["gen"] == first["gen"] + 1 and second["hash"] != first["hash"]
    assert services.gwcfg_doc(_gw(services))["gen"] == second["gen"]
    services.gateway_set_lan_subnets(1, "192.168.68.0/24")
    third = services.gwcfg_doc(_gw(services))
    assert third["hash"] == first["hash"] and third["gen"] == second["gen"] + 1, (third["gen"], second["gen"])


def test_the_document_print_is_the_print_of_its_sections(services, slot):
    doc = services.gwcfg_doc(slot)
    assert doc["hash"] == gwcfg.doc_print(doc["prints"])
    assert doc["prints"] == services.gwcfg_prints(doc["sections"])
    assert doc["prints"]["uplink"] == "" and doc["prints"]["token"] == ""
    assert all(len(p) == 16 for s, p in doc["prints"].items() if s in ("env", "link", "server")), doc["prints"]


# ── что доставить ────────────────────────────────────────────────────────────

def test_nothing_is_due_without_a_snapshot_or_its_cfg_field(services, slot):
    """Судить не по чему — не шлём: агент 3.2.x поля cfg не присылает, а шлюз,
    ещё не приславший снимок, мог уже всё получить."""
    assert services.gwcfg_due(slot) is None
    _snap(services, None, bundle={"lan_mode": "1"})
    assert services.gwcfg_due(slot) is None, "снимок без cfg сошёл за расхождение"


def test_a_matching_snapshot_brings_nothing_and_a_drift_brings_the_whole_document(services, slot):
    gw = _gw(services)
    _snap(services, _have(services))
    assert services.gwcfg_due(gw) is None, "всё совпадает, а документ уходит"
    _snap(services, _have(services, env="0" * 16))
    body = services.gwcfg_due(gw)
    doc = services.gwcfg_doc(gw)
    assert body is not None and body["hash"] == doc["hash"] and body["gen"] == doc["gen"]
    assert body["env"] == services.gwcfg_env(gw)
    assert body["link"]["conf"] == services.gwcfg_link_conf(gw, body["env"])
    assert gwcfg.parse(body)["hash"] == doc["hash"]


def test_a_section_the_server_does_not_have_is_left_alone(services, slot):
    """У сервера нет почты, а на шлюзе своя — это не расхождение: «нет у
    сервера» значит «своё на шлюзе не трогать»."""
    _snap(services, _have(services, mail="ab" * 8))
    assert services.gwcfg_due(slot) is None


def test_mail_waiting_for_a_decision_is_not_sent_again_until_it_changes(services, slot, host):
    """Шлюз спросил человека о почте (на шлюзе свой ящик) и ответил `wait` с
    отпечатком серверной почты. Пока на ВПС та же почта — документ не шлётся:
    иначе вопрос задавался бы на каждом такте. Почту на ВПС сменили — новый
    документ (и новый вопрос)."""
    _mail_on(services, host)
    gw = _gw(services)
    doc = services.gwcfg_doc(gw)
    _snap(services, _have(services, mail="ab" * 8))
    assert services.gwcfg_due(gw) is not None, "сценарий без расхождения ничего не проверяет"
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"], "applied": [],
                              "wait": {"mail": doc["prints"]["mail"]}})
    assert services.gwcfg_due(gw) is None, "почта в ожидании решения ушла снова"
    services.db.set_state("email_password", "DUMMY-other")
    assert services.gwcfg_due(gw) is not None, "новая почта на ВПС не ушла"


def test_kept_own_mail_is_not_sent_again_until_the_server_mail_changes(services, slot, host):
    """«Оставить своё» о почте — `declined` с отпечатком серверной почты.
    Тот же отпечаток — тишина; почту на ВПС сменили — новый документ."""
    _mail_on(services, host)
    gw = _gw(services)
    doc = services.gwcfg_doc(gw)
    _snap(services, _have(services, mail="ab" * 8))
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"], "applied": [],
                              "declined": {"mail": doc["prints"]["mail"]}})
    assert services.gwcfg_due(gw) is None, "отклонённая почта ушла снова"
    services.db.set_state("email_password", "DUMMY-other")
    assert services.gwcfg_due(gw) is not None, "новая почта на ВПС не ушла"


def test_a_decline_of_an_older_value_does_not_hold_the_new_one(services, slot, host):
    """Отказ запомнен по отпечатку значения, а не по разделу: «оставить своё»
    про прежнюю почту не глушит доставку новой."""
    _mail_on(services, host)
    gw = _gw(services)
    doc = services.gwcfg_doc(gw)
    _snap(services, _have(services, mail="ab" * 8))
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"], "declined": {"mail": "ef" * 8}})
    assert services.gwcfg_due(gw) is not None


def test_a_held_section_does_not_hold_the_others(services, slot, host):
    """Почта ждёт решения — а подсети на ВПС сменили: документ уходит."""
    _mail_on(services, host)
    gw = _gw(services)
    doc = services.gwcfg_doc(gw)
    _snap(services, _have(services, mail="ab" * 8))
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"], "wait": {"mail": doc["prints"]["mail"]}})
    assert services.gwcfg_due(gw) is None
    services.gateway_set_lan_subnets(1, "192.168.70.0/24")
    assert services.gwcfg_due(_gw(services)) is not None, "ожидание почты заглушило смену подсетей"


# ── ответ шлюза ──────────────────────────────────────────────────────────────

def test_the_answer_is_kept_whole_with_its_time_and_without_garbage(services, slot):
    """Ответ — одним JSON со временем приёма: по нему карточка пишет
    «применена …». Чужие разделы, не-hex отпечатки и разметка в причине —
    данные с чужой машины — не сохраняются как есть."""
    ack = services.gwcfg_ack_in(1, {"gen": 3, "hash": "ab" * 8, "applied": ["env", "bogus"],
                                    "wait": {"mail": "<b>x</b>", "zzz": "ab"},
                                    "failed": {"link": "нет\nстроки"}, "retry": True, "extra": 1})
    stored = services.gwcfg_ack(1)
    assert stored == ack and stored["at"], stored
    assert stored["applied"] == ["env"] and stored["wait"] == {"mail": ""}, stored
    assert stored["failed"] == {"link": "нет строки"} and stored["retry"] is True
    assert "extra" not in stored


def test_a_broken_stored_answer_reads_as_no_answer(services, slot):
    services.db.set_state("gwcfg_ack_1", "{не json")
    assert services.gwcfg_ack(1) == {}
    services.db.set_state("gwcfg_ack_1", "мусор")
    assert services.gwcfg_ack(1) == {}


def test_removing_the_slot_forgets_its_document_state(services, slot, monkeypatch):
    """Снятие слота уносит поколение, отпечаток, ответ и причину: чужой отказ
    не должен проступить у следующего шлюза в том же слоте."""
    services.gwcfg_doc(slot)
    services.gwcfg_ack_in(1, {"gen": 1, "hash": "ab" * 8, "failed": {"env": "x"}})
    services.db.set_state("gwcfg_link_err_1", "x")
    services.gateway_remove(1)
    for key in ("gwcfg_gen_1", "gwcfg_hash_1", "gwcfg_ack_1", "gwcfg_link_err_1"):
        assert not services.db.get_state(key), f"{key} пережил снятие слота"


# ── что знает карточка ───────────────────────────────────────────────────────

def test_the_card_knows_nothing_of_the_document_for_an_agent_without_cfg1(services, slot):
    """Агент ниже 3.3.0 (возможности cfg1 нет) — устаревший: о документе
    карточке сказать нечего, строка «обнови агента» — её дело."""
    services.gwlink_session_opened(1, "3.2.1", 2)
    _snap(services, _have(services, env="0" * 16))
    card = services.gwcfg_card(slot, services.gwlink_snapshot(1))
    assert services.gwlink_agent_outdated(1) is True
    assert card["due"] == [] and card["failed"] == {} and "cfg1" not in card


def test_the_card_follows_the_answer_to_the_current_document_only(services, slot, host):
    """Отказ и «применена» — только об ответе на текущий документ: отказ на
    прежний документ, после которого человек всё исправил, карточка не
    показывает."""
    gw = _gw(services)
    services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1"])
    _snap(services, _have(services, env="0" * 16))
    doc = services.gwcfg_doc(gw)
    card = services.gwcfg_card(gw, services.gwlink_snapshot(1))
    assert card["due"] == ["env"], card
    assert card["failed"] == {} and card["applied_at"] == ""
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"], "failed": {"env": "exit 1"}})
    card = services.gwcfg_card(gw, services.gwlink_snapshot(1))
    assert card["failed"] == {"env": "exit 1"} and card["applied_at"] == "", card
    services.gateway_set_lan_subnets(1, "192.168.70.0/24")
    card = services.gwcfg_card(_gw(services), services.gwlink_snapshot(1))
    assert card["failed"] == {}, "отказ на прежний документ выдан за отказ на текущий"
    doc = services.gwcfg_doc(_gw(services))
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"], "applied": ["env"]})
    _snap(services, _have(services))
    card = services.gwcfg_card(_gw(services), services.gwlink_snapshot(1))
    assert card["due"] == [] and card["applied_at"] and card["gen"] == doc["gen"], card


def test_the_card_names_waiting_and_kept_sections(services, slot, host):
    """Ждёт решения и оставлено своим — по разделам; фраза прежнего агента
    (раздел, которого в документе больше нет) в карточку не попадает."""
    _mail_on(services, host)
    gw = _gw(services)
    services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1"])
    doc = services.gwcfg_doc(gw)
    _snap(services, _have(services, mail="ab" * 8))
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"], "wait": {"mail": doc["prints"]["mail"]},
                              "declined": {"backup": "cd" * 8}})
    card = services.gwcfg_card(gw, services.gwlink_snapshot(1))
    assert card["wait"] == ["mail"] and card["declined"] == [] and card["due"] == [], card


def test_capabilities_outlive_the_session(services, slot):
    """Шлюз ушёл со связи — карточка по-прежнему знает, что его агент
    принимает документ: иначе не на связи он выглядел бы устаревшим. Слот,
    ни разу не выходивший на связь, — не устаревший."""
    assert services.gwlink_agent_outdated(1) is False, "новый слот выглядит устаревшим"
    services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1"])
    services.gwlink_session_closed(1)
    assert services.gwlink_agent_caps(1) == ["cfg1"]
    assert services.gwlink_agent_outdated(1) is False
    services.gwlink_session_opened(1, "3.2.1", 2)
    assert services.gwlink_agent_caps(1) == [], "откат агента не сбросил возможности"
    assert services.gwlink_agent_outdated(1) is True


# ── доставка на живом слушателе ──────────────────────────────────────────────

@pytest.fixture()
async def srv(services, make_active_client, monkeypatch, host):
    """Слушатель канала на петле и слот с локальной подсетью. Адрес /30 здесь —
    петля, а правило AllowedIPs петлю не пропустит: конфиг линка в этих
    сценариях не собирается (его сборка — тесты выше)."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "127.0.0.0/30", slot_id=1)
    services.gateway_set_lan_subnets(1, "192.168.68.0/24")
    monkeypatch.setattr(services, "_link_privkey", lambda g=None: PRIV)
    monkeypatch.setattr(services, "gwcfg_link_conf", lambda gw, env: "")
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    port = _free_port()
    monkeypatch.setattr(linkserver, "channel_port", lambda: port)
    monkeypatch.setattr(linkserver, "gw_address", lambda cidr: "127.0.0.1")
    server = linkserver.LinkServer(services)
    await server.ensure()
    assert server._bound, "слушатель канала не поднялся на адресе линка"
    yield server
    await server.stop()


def _bundle(services, **over) -> dict:
    want = services.gwlink_issued_env(services.db.gateway(1))
    names = {"ADMIN_IPS": "admin_ips", "LAN_SUBNETS": "lan_subnets", "LAN_MODE": "lan_mode",
             "PEER_LAN_NETS": "peer_lan_nets", "RESOLVER": "resolver"}
    return {**{names[k]: v for k, v in want.items()}, **over}


async def _connect(server, services, *, caps=("cfg1",), cfg: dict | None = None, bundle=None) -> _Gw:
    """Шлюз подключился (с возможностями caps) и прислал снимок."""
    host, port = server._bound[0]
    gw = _Gw(*await asyncio.open_connection(host, port), key=KEY)
    hello = {"proto": gwlink.PROTO, "agent": "3.3.0" if caps else "3.2.1"}
    if caps is not None:
        hello["caps"] = list(caps)
    await gw.send("hello", hello)
    snap = {"bundle": bundle if bundle is not None else _bundle(services), "link_contract": "1",
            "plumbing_gen": "new", "mark_status": "confirmed", "agent_version": hello["agent"],
            "awg_generation": 1, "egress_ok": True, "boot_id": "b" * 36, "rev": 1}
    if cfg is not None:
        snap["cfg"] = cfg
    await gw.send("snap", snap)
    return gw


async def _next(gw: _Gw, kinds=("cfg", "settings"), timeout: float = 1.5) -> dict | None:
    """Следующее сообщение одного из видов kinds или None за время ожидания;
    прочее (роль, фиды) пропускается."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        left = end - loop.time()
        if left <= 0:
            return None
        msg = await gw.next_msg(left)
        if msg is None:
            return None
        if msg.get("t") in kinds:
            return msg


async def test_an_agent_with_cfg1_gets_the_document_not_the_settings(services, srv):
    """Агент назвал cfg1 и прислал отпечатки, разошедшиеся с желаемым, — ему
    уходит документ, а не прежние settings: два пути одному шлюзу значили бы
    двойное применение и спор, чей ответ верен."""
    gw = await _connect(srv, services, cfg=_have(services, env="0" * 16),
                        bundle=_bundle(services, lan_subnets="192.168.1.0/24"))
    msg = await _next(gw)
    assert msg is not None, "расхождение есть, а документ не пришёл"
    assert msg["t"] == "cfg", f"агенту с cfg1 ушли {msg['t']}"
    doc = services.gwcfg_doc(services.db.gateway(1))
    assert msg["hash"] == doc["hash"] and msg["gen"] == doc["gen"]
    assert msg["env"] == services.gwcfg_env(services.db.gateway(1))
    assert "link" not in msg and "uplink" not in msg and "token" not in msg, sorted(msg)
    assert PRIV not in json.dumps(msg), "приватный ключ линка уехал в канал"
    assert await _next(gw, timeout=0.4) is None, "документ или настройки пришли второй раз"
    await gw.close()


async def test_the_same_document_goes_once_per_session_and_again_in_the_next(services, srv):
    """Один документ — одна отправка за сессию: такт живости не долбит шлюз
    тем же. Новая сессия — новая попытка: память «уже слали» принадлежит
    сессии, иначе расхождение после перезагрузки шлюза осталось бы навсегда."""
    gw = await _connect(srv, services, cfg=_have(services, env="0" * 16))
    assert (await _next(gw) or {}).get("t") == "cfg"
    await srv.deliver_all()
    await srv.deliver_all()
    assert await _next(gw, timeout=0.4) is None, "тот же документ ушёл в сессии повторно"
    await gw.close()
    await _until(lambda: not services.gwlink_session(1))
    gw2 = await _connect(srv, services, cfg=_have(services, env="0" * 16))
    assert (await _next(gw2) or {}).get("t") == "cfg", "после переподключения документ не пришёл"
    await gw2.close()


async def test_a_matching_gateway_hears_nothing(services, srv):
    gw = await _connect(srv, services, cfg=_have(services))
    await _until(lambda: services.gwlink_snapshot(1))
    assert await _next(gw, timeout=0.4) is None, "всё совпадает, а документ пришёл"
    await srv.deliver_all()
    assert await _next(gw, timeout=0.3) is None, "такт живости прислал документ без расхождения"
    await gw.close()


async def test_a_new_edit_goes_out_in_the_same_session(services, srv):
    gw = await _connect(srv, services, cfg=_have(services, env="0" * 16))
    first = await _next(gw)
    services.gateway_set_lan_subnets(1, "192.168.70.0/24")
    await srv.deliver_all()
    second = await _next(gw)
    assert second is not None and second["t"] == "cfg", "правка на ВПС не ушла в той же сессии"
    assert second["hash"] != first["hash"] and second["env"]["LAN_SUBNETS"] == "192.168.70.0/24"
    await gw.close()


async def test_a_refusal_lands_in_state_and_notifies_once(services, srv):
    """Шлюз не применил документ: ответ ложится в state, человеку — одно
    уведомление «Шлюз не применил: …» (расхождение пережило доставку), второго
    на тот же отказ нет."""
    gw = await _connect(srv, services, cfg=_have(services, env="0" * 16),
                        bundle=_bundle(services, lan_subnets="192.168.1.0/24"))
    msg = await _next(gw)
    await gw.send("cfg_ack", {"gen": msg["gen"], "hash": msg["hash"], "applied": [],
                              "failed": {"env": "юнит обвязки не отработал за 600 с"}})
    await _until(lambda: services.gwcfg_ack(1))
    assert services.gwcfg_ack(1)["failed"] == {"env": "юнит обвязки не отработал за 600 с"}
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1, [n.text for n in notes]
    assert "Шлюз не применил: юнит обвязки не отработал за 600 с" in notes[0].text, notes[0].text
    assert services.gw_bundle_drift_notes() == [], "тот же отказ — второе уведомление"
    await srv.deliver_all()
    assert await _next(gw, timeout=0.4) is None, "отвергнутый документ ушёл снова в той же сессии"
    await gw.close()


async def test_a_temporary_refusal_sends_the_document_again(services, srv):
    """Агент ответил «повтори» (юнит обвязки ещё работал): документ уходит
    снова на следующей сверке, уведомления об отказе нет."""
    gw = await _connect(srv, services, cfg=_have(services, env="0" * 16),
                        bundle=_bundle(services, lan_subnets="192.168.1.0/24"))
    msg = await _next(gw)
    await gw.send("cfg_ack", {"gen": msg["gen"], "hash": msg["hash"], "retry": True})
    await _until(lambda: services.gwcfg_ack(1))
    assert services.gw_bundle_drift_notes() == [], "временный отказ принят за настоящий"
    await srv.deliver_all()
    again = await _next(gw)
    assert again is not None and again["t"] == "cfg" and again["hash"] == msg["hash"], "после «повтори» не ушло"
    await gw.close()


async def test_an_agent_without_capabilities_gets_nothing_to_apply(services, srv):
    """Агент ниже 3.3.0 вернулся после молчания: прежних settings больше нет,
    а документ он не разберёт — снимок без отпечатков cfg, сверять нечего.
    Конфигурация до него не доходит, пока его не обновят."""
    gw = await _connect(srv, services, caps=None, bundle=_bundle(services, lan_subnets="192.168.1.0/24"))
    assert await _next(gw, timeout=0.6) is None, "агенту ниже 3.3.0 что-то ушло"
    assert services.gwlink_agent_outdated(1) is True
    await gw.close()
