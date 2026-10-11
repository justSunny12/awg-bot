"""
Документ конфигурации с сервера AWG на агенте шлюза (domain/gateway/cfg):
отпечатки стоящего для снимка, проверка документа целиком до первого
действия, порядок разделов, транзакция env + link с пробой линка и откатом,
ответ cfg_ack и его очередь.

Документ приходит без кнопки человека и применяется от root. Цена ошибки:
отпечаток, посчитанный не по тому, что стоит, — бесконечная доставка или
молча не доехавшая настройка; документ, применённый наполовину, — шлюз с
подсетями соседей в юните и без них в AllowedIPs линка («зелёная сверка при
неработающем доступе»); конфиг линка, после которого линк не поднялся и не
откатился, — РФ-доступ у всех лежит до визита к шлюзу.

Хост подменён классом `_Host` поверх `_Unit` из теста настроек канала: юнит
обвязки — настоящий файл во временном каталоге, `systemctl`, скрипт обвязки
(`--link-apply`), `awg show … latest-handshakes` и `ip -j` — подставные
команды, которые записывают вызовы. Весь код агента и `gwguard` — боевой:
проверяется, что оказалось в юните и в конфиге линка, сколько раз
перевыставляли обвязку и что ушло серверу и человеку.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.domain import gwcfg
from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gateway.cfg import txn
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from awgbot.util import wgconf
from tests.unit.test_gwlink_settings_apply import UNIT_TEXT, _cp, _Unit
from tests.unit.test_wgconf_allowed import LINK

pytestmark = pytest.mark.unit

# env документа в именах ДОКУМЕНТА — ровно то, что стоит в UNIT_TEXT
SAME_ENV = {"CLIENT_SUBNET": "10.8.1.0/24", "ADMIN_IPS": "10.8.1.2", "LAN_MODE": "1",
            "LAN_SUBNETS": "192.168.68.0/24", "RESOLVER": "10.9.1.1", "PEER_LAN_NETS": "",
            "LINK_CHANNEL_PORT": "8787"}
MAIL = {"login": "box@example.org", "password": "DUMMY-pass", "imap_host": "imap.example.org",
        "imap_port": 993, "smtp_host": "smtp.example.org", "smtp_port": 465, "backup": ""}
DIGEST = "9c41" * 4

# конфиг линка с теми же ключами, но другим Endpoint — только down/up
LINK_NEW_ENDPOINT = LINK.replace("Endpoint = vps.example.org:51821", "Endpoint = vps2.example.org:51821")
# только keepalive — syncconf без перезапуска
LINK_NEW_KA = LINK.replace("PersistentKeepalive = 25-35", "PersistentKeepalive = 25")
# подсеть соседа и в AllowedIPs
LINK_WITH_PEER = LINK.replace("AllowedIPs = 10.8.1.0/24, 10.99.99.0/30",
                              "AllowedIPs = 10.8.1.0/24, 10.99.99.0/30, 192.168.70.0/24")


class _Clock:
    """Часы для пробы линка и ожидания юнита: sleep двигает monotonic, а не ждёт."""

    def __init__(self, monkeypatch):
        self.t = 1000.0
        self.slept = 0.0
        monkeypatch.setattr(time, "monotonic", lambda: self.t)
        monkeypatch.setattr(time, "sleep", self._sleep)

    def _sleep(self, s):
        self.t += s
        self.slept += s


class _Host(_Unit):
    """Шлюз целиком для документа: юнит (из `_Unit`), установленный конфиг
    линка, каталог /etc/awg-gw, скрипт обвязки в режиме --link-apply, awg и ip.

    `active` — ActiveState юнита; `how` — что ответит скрипт на новый конфиг
    (unchanged | syncconf | restarted); `link_rc` — его код (не 0 — отказал,
    ничего не установил); `handshake` — epoch последнего хендшейка линка или 0
    (не было). Скрипт при успехе ставит файл на место установленного конфига,
    как настоящий `install -m 600`."""

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        self.etc = tmp_path / "awg-gw"
        self.link = tmp_path / "awglink.conf"
        self.link.write_text(LINK, encoding="utf-8")
        monkeypatch.setattr(config, "GW_LINK_CONF", str(self.link))
        monkeypatch.setattr(gwguard, "GW_ETC", str(self.etc))
        monkeypatch.setattr(gwguard, "_unit_cache", None)
        self.active = "active"
        self.how = "syncconf"
        self.link_rc = 0
        self.link_runs: list[tuple[str, str]] = []      # (имя файла, текст) — что подали скрипту
        self.etc_seen: list[dict] = []                  # каталог транзакции в момент каждого --link-apply: имя → (текст, права)
        self.handshake = int(time.time())
        self.events: list[str] = []                     # последовательность действий на хосте

    def _run(self, argv, **kw):
        argv = list(argv)
        if argv[:2] == ["systemctl", "show"]:
            self.calls.append(argv)
            return _cp(0, f"ActiveState={self.active}\nUnitFileState=enabled\nResult=success\n".encode())
        if len(argv) >= 3 and argv[1] == "--link-apply":
            self.calls.append(argv)
            src = Path(argv[2])
            text = src.read_text(encoding="utf-8")
            self.link_runs.append((src.name, text))
            self.etc_seen.append({f.name: (f.read_text(encoding="utf-8"), f.stat().st_mode)
                                  for f in self.etc.iterdir() if f.is_file()})
            self.events.append(f"link:{src.name}")
            if self.link_rc:
                return _cp(self.link_rc, b"", "ОШИБКА: конфиг линка не прошёл проверку — не установлен\n".encode())
            how = "unchanged" if text == self.link.read_text(encoding="utf-8") else self.how
            self.link.write_text(text, encoding="utf-8")
            return _cp(0, f"── Конфиг линка из упр. канала\nlink: {how}\n".encode())
        if argv[:2] == ["awg", "show"] and argv[-1] == "latest-handshakes":
            self.calls.append(argv)
            return _cp(0, f"DUMMYPUB=\t{self.handshake}\n".encode())
        if argv[:2] == ["ip", "-j"]:
            self.calls.append(argv)
            return _cp(0, b"[]")
        if argv[:2] == ["systemctl", "restart"]:
            self.events.append("reassert")
        return super()._run(argv, **kw)

    @property
    def link_applies(self) -> list[str]:
        return [name for name, _ in self.link_runs]

    def installed_link_print(self) -> str:
        return gwcfg.link_print(self.link.read_text(encoding="utf-8"))


@pytest.fixture()
def conf_dir(tmp_path):
    """Своя копия conf: почта с сервера пишет серверы в email.yaml, канал
    бэкапов — в app.yaml; соседним тестам это видеть незачем."""
    from awgbot.core import settings
    from tests.conftest import _CONF_COPY, restore_settings
    d = tmp_path / "conf"
    shutil.copytree(_CONF_COPY, d)
    settings.init(d)
    yield d
    restore_settings()


@pytest.fixture()
def svc(tmp_path, conf_dir):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    yield GatewayServices(d)
    d.close()


@pytest.fixture()
def host(tmp_path, monkeypatch):
    return _Host(tmp_path, monkeypatch)


def _doc(*, gen: int = 7, env: dict | None = None, link: str | None = None, **sections) -> dict:
    """Тело сообщения cfg так, как его собирает сервер."""
    body = {"env": {**SAME_ENV, **(env or {})}}
    if link is not None:
        body["link"] = {"conf": link}
    body.update(sections)
    return gwcfg.build(gen, DIGEST, body)


# ── отпечатки стоящего: поле cfg снимка ──────────────────────────────────────

def test_fingerprints_describe_what_stands_on_the_gateway(svc, host):
    """env — по юниту, но в именах документа (HOME_SUBNETS юнита — это
    LAN_SUBNETS документа): посчитай агент отпечаток в именах юнита, он не
    совпал бы с серверным никогда, и документ ехал бы на каждом снимке."""
    fp = svc.cfg_fingerprints()
    assert fp["env"] == gwcfg.env_print(SAME_ENV), "env снимка посчитан не по юниту в именах документа"
    assert fp["link"] == gwcfg.link_print(LINK), "link снимка — не по установленному конфигу"
    assert svc.cfg_env_current()["LAN_SUBNETS"] == "192.168.68.0/24"
    assert svc.cfg_env_current()["PEER_LAN_NETS"] == ""
    assert fp["gen"] == 0 and fp["uplink"] == ""
    # токен — отпечаток того, на чём агент работает сейчас, не сам токен:
    # пустой отпечаток сервер принял бы за «токена нет» и вёз бы раздел вечно
    assert fp["token"] == gwcfg.token_print(config.BOT_TOKEN) and fp["token"], fp["token"]
    assert config.BOT_TOKEN not in str(fp), "в снимок ушёл сам токен бота"


def test_fingerprints_of_stubs_are_empty(svc, host):
    """Заглушка (ящик не задан или задан наполовину, шифрования нет, имени
    сервера нет) — пустой отпечаток: по нему сервер понимает «можно молча»."""
    fp = svc.cfg_fingerprints()
    assert (fp["mail"], fp["backup"], fp["server"]) == ("", "", ""), fp
    svc.db.set_state(svc._MAIL_LOGIN_KEY, "box@example.org")       # логин есть, пароля нет
    assert svc.cfg_fingerprints()["mail"] == "", "наполовину заданный ящик дал отпечаток"


def test_fingerprints_of_own_values_follow_the_values(svc, host):
    """Свой ящик, своя фраза, случайный ключ прежней схемы, имя сервера —
    отпечатки тех же значений, что считает сервер; случайный ключ — в своём
    домене, с фразой не совпадает никогда (иначе шлюз на ключе приняли бы за
    «уже стоит фраза сервера»)."""
    svc.email_save(MAIL["login"], MAIL["password"], MAIL["imap_host"], MAIL["imap_port"],
                   MAIL["smtp_host"], MAIL["smtp_port"])
    svc.db.set_state(svc._SERVER_NAME_KEY, "AWG-SRV")
    svc.db.set_state(svc._BK_KEY_KEY, "RFVNTVlLRVk=")
    fp = svc.cfg_fingerprints()
    assert fp["mail"] == gwcfg.mail_print(MAIL)
    assert fp["server"] == gwcfg.server_print("AWG-SRV")
    assert fp["backup"] == gwcfg.backup_key_print("RFVNTVlLRVk=") and fp["backup"]
    svc.backup_set_passphrase("DUMMY-phrase-1")
    assert svc.cfg_fingerprints()["backup"] == gwcfg.backup_print("DUMMY-phrase-1")


def test_a_broken_link_conf_gives_an_empty_link_print(svc, host):
    """Конфиг линка не разбирается — пустой отпечаток («стоит не наш»), а не
    исключение, роняющее весь снимок."""
    host.link.write_text("мусор\n", encoding="utf-8")
    assert svc.cfg_fingerprints()["link"] == ""
    assert svc.cfg_link_cidr() == "" and svc.cfg_link_fixed() == {}, "из мусора вынута /30 или ключи"


# ── документ целиком ─────────────────────────────────────────────────────────

def test_one_bad_value_rejects_the_whole_document_and_touches_nothing(svc, host):
    """AllowedIPs = 0.0.0.0/0 увёл бы весь трафик шлюза в туннель. Документ
    с ним отвергается целиком: ни юнит, ни конфиг линка, ни почта, ни имя
    сервера не тронуты, серверу — failed {"*": причина}, человеку — «Ничего не
    менял»."""
    bad = LINK.replace("AllowedIPs = 10.8.1.0/24, 10.99.99.0/30", "AllowedIPs = 0.0.0.0/0")
    res = svc.apply_cfg(_doc(env={"ADMIN_IPS": "10.8.1.2 10.8.1.7"}, link=bad, mail=MAIL,
                             server={"name": "AWG-SRV"}))
    ack = res["ack"]
    assert set(ack["failed"]) == {"*"} and "AllowedIPs" in ack["failed"]["*"], ack
    assert ack["applied"] == [] and ack["gen"] == 7 and ack["hash"] == DIGEST
    assert "не прошла проверку" in res["note"] and res["note"].endswith("Ничего не менял"), res["note"]
    assert res["ask"] == []
    assert host.text() == UNIT_TEXT, "юнит тронут документом, который не прошёл проверку"
    assert host.link.read_text(encoding="utf-8") == LINK, "конфиг линка тронут"
    assert host.restarts == 0 and host.link_runs == [], host.calls
    assert svc.email_account() is None and not svc.db.get_state(svc._SERVER_NAME_KEY)
    assert not svc.db.get_state(svc._CFG_GEN_KEY), "поколение отвергнутого документа записано"


def test_a_document_without_env_is_rejected(svc, host):
    res = svc.apply_cfg(gwcfg.build(3, DIGEST, {"server": {"name": "AWG-SRV"}}))
    assert "*" in res["ack"]["failed"] and not svc.db.get_state(svc._SERVER_NAME_KEY)


def test_link_address_outside_the_installed_link_is_rejected(svc, host):
    """/30 и адрес линка документ берёт из стоящего конфига: каналом их не
    сменить — иначе линк поднялся бы в чужой подсети."""
    moved = LINK.replace("Address = 10.99.99.2/30", "Address = 10.99.98.2/30").replace(
        "10.99.99.0/30", "10.99.98.0/30")
    res = svc.apply_cfg(_doc(link=moved))
    assert "*" in res["ack"]["failed"] and host.link_runs == []


def test_sections_go_in_order_server_env_link_mail_backup(svc, host, monkeypatch):
    """Порядок — имя сервера, env + link одной транзакцией, почта, фраза:
    почта и фраза зависят от живого линка меньше всего и идут последними."""
    from awgbot.infra import mail as mailmod
    monkeypatch.setattr(mailmod, "check_imap", lambda a, timeout=15.0: host.events.append("mail-check"))
    monkeypatch.setattr(mailmod, "check_smtp", lambda a, timeout=15.0: None)
    real_set = svc.db.set_state

    def set_state(key, value):
        if key == svc._SERVER_NAME_KEY:
            host.events.append("server")
        if key == svc._BK_PASSPHRASE_KEY:
            host.events.append("backup")
        return real_set(key, value)
    monkeypatch.setattr(svc.db, "set_state", set_state)
    res = svc.apply_cfg(_doc(env={"ADMIN_IPS": "10.8.1.2 10.8.1.7"}, link=LINK_NEW_KA, mail=MAIL,
                             backup={"phrase": "DUMMY-phrase-1"}, server={"name": "AWG-SRV"}))
    assert host.events == ["server", "link:link.next", "reassert", "mail-check", "backup"], host.events
    assert res["ack"]["applied"] == ["server", "env", "link", "mail", "backup"], res["ack"]


# ── env + link: одна транзакция ──────────────────────────────────────────────

def test_env_alone_rewrites_the_unit_and_reasserts_once(svc, host):
    """Сменились только устройства админа: юнит переписан, обвязка
    перевыставлена один раз, скрипт линка не звали; серверу — applied, человеку
    — «применено: устройства админа», поколение и отпечаток записаны."""
    res = svc.apply_cfg(_doc(env={"ADMIN_IPS": "10.8.1.2 10.8.1.7"}, link=LINK))
    assert host.env()["ADMIN_IPS"] == "10.8.1.2 10.8.1.7"
    assert host.restarts == 1, f"реассертов {host.restarts}, нужен ровно один"
    assert host.link_runs == [], "конфиг линка тот же, а скрипт линка звали"
    assert res["ack"]["applied"] == ["env", "link"] and not res["ack"]["failed"]
    assert res["note"] == "⚙️ Сервер AWG прислал конфигурацию шлюза — применено: устройства админа", res["note"]
    assert svc.db.get_state(svc._CFG_GEN_KEY) == "7" and svc.db.get_state(svc._CFG_HASH_KEY) == DIGEST
    assert svc.cfg_fingerprints()["gen"] == 7


def test_unit_lines_are_written_under_the_unit_names(svc, host):
    """PEER_LAN_NETS документа живёт в юните как PEER_HOME_NETS: скрипт и
    агенты 3.2.x читают прежнее имя. Подсеть соседа доезжает и в юнит, и в
    AllowedIPs линка — одной транзакцией."""
    res = svc.apply_cfg(_doc(env={"PEER_LAN_NETS": "192.168.70.0/24"}, link=LINK_WITH_PEER))
    assert gwguard.unit_env("PEER_HOME_NETS") == "192.168.70.0/24", host.text()
    assert "PEER_LAN_NETS" not in host.text(), "в юнит ушло имя документа"
    assert "192.168.70.0/24" in host.link.read_text(encoding="utf-8")
    assert host.link_applies == ["link.next"] and host.restarts == 1
    assert "локальные подсети других шлюзов" in res["note"] and "конфиг линка" in res["note"], res["note"]


def test_the_same_document_twice_changes_nothing_the_second_time(svc, host):
    """Повтор доставки (сессия умерла до ответа) — нечего менять: ни
    перезаписи юнита, ни реассерта, ни уведомления."""
    svc.apply_cfg(_doc(env={"ADMIN_IPS": "10.8.1.2 10.8.1.7"}, link=LINK_NEW_KA))
    restarts, runs = host.restarts, list(host.link_runs)
    res = svc.apply_cfg(_doc(env={"ADMIN_IPS": "10.8.1.2 10.8.1.7"}, link=LINK_NEW_KA))
    assert host.restarts == restarts and host.link_runs == runs, "повтор того же документа дёрнул обвязку"
    assert res["note"] == "" and res["ack"]["applied"] == ["env", "link"]


def test_keepalive_change_goes_by_syncconf_without_the_unit(svc, host):
    """Сменился keepalive клиентов: юнит не трогается, конфиг линка —
    через link.next, прежний — в link.prev для отката; реассерт один."""
    host.how = "syncconf"
    res = svc.apply_cfg(_doc(link=LINK_NEW_KA))
    assert host.text() == UNIT_TEXT, "юнит переписан, хотя env не менялся"
    assert not [c for c in host.calls if c[:2] == ["systemctl", "daemon-reload"]]
    assert host.link_applies == ["link.next"]
    seen = host.etc_seen[0]                             # в момент применения: прежний — для отката
    assert gwcfg.link_print(seen["link.prev"][0]) == gwcfg.link_print(LINK)
    assert gwcfg.link_print(seen["link.next"][0]) == gwcfg.link_print(LINK_NEW_KA)
    assert not (host.etc / "link.next").exists() and not (host.etc / "link.prev").exists(), (
        "после фиксации файлы транзакции с ключом линка остались на диске")
    assert host.installed_link_print() == gwcfg.link_print(LINK_NEW_KA)
    assert host.restarts == 1
    assert res["ack"]["applied"] == ["env", "link"] and res["note"].endswith("применено: конфиг линка"), res["note"]
    assert not [c for c in host.calls if c[:2] == ["awg", "show"]], "после syncconf линк пробовать незачем"


def test_link_files_are_private(svc, host):
    """В link.next и link.prev — приватный ключ и PSK линка: пока они на диске,
    читает их только root; после фиксации их нет вовсе."""
    svc.apply_cfg(_doc(link=LINK_NEW_KA))
    seen = host.etc_seen[0]
    for name in ("link.next", "link.prev"):
        assert seen[name][1] & 0o077 == 0, f"{name} читается не только root"
        assert not (host.etc / name).exists(), f"{name} остался после фиксации"


def test_restarted_link_with_a_fresh_handshake_is_kept(svc, host, monkeypatch):
    """Сменился Endpoint: down/up; линк поднялся (свежий хендшейк) — новый
    конфиг остаётся, отката нет."""
    _Clock(monkeypatch)
    host.how = "restarted"
    res = svc.apply_cfg(_doc(link=LINK_NEW_ENDPOINT))
    assert host.link_applies == ["link.next"]
    assert host.installed_link_print() == gwcfg.link_print(LINK_NEW_ENDPOINT)
    assert not res["ack"]["failed"] and "link" in res["ack"]["applied"]


def test_restarted_link_that_did_not_come_up_is_rolled_back(svc, host, monkeypatch):
    """Линк после down/up не дал хендшейка за 90 с — откат: прежний конфиг
    через link.prev, юнит прежним текстом, реассерт; серверу — failed env и
    link с причиной, человеку — «не применилась … Вернул прежнюю»; поколение
    не записано — сервер повторит."""
    clock = _Clock(monkeypatch)
    host.how = "restarted"
    host.handshake = 0                                  # хендшейка нет
    res = svc.apply_cfg(_doc(env={"ADMIN_IPS": "10.8.1.2 10.8.1.7"}, link=LINK_NEW_ENDPOINT))
    assert host.link_applies == ["link.next", "link.prev"], host.link_applies
    assert host.installed_link_print() == gwcfg.link_print(LINK), "прежний конфиг линка не вернулся"
    assert host.text() == UNIT_TEXT, "юнит не вернулся к прежнему тексту"
    # проба линка — до реассерта: не поднялся — сразу откат, реассерт один (уже
    # с прежним юнитом и прежним конфигом), не «новый, потом прежний»
    assert host.restarts == 1, "после отката обвязку не перевыставили (или перевыставили дважды)"
    assert clock.slept >= txn.PROBE_SECONDS - 3, f"пробовали линк {clock.slept} с — меньше обещанного"
    failed = res["ack"]["failed"]
    assert set(failed) == {"env", "link"} and "не поднялся" in failed["link"], failed
    assert "env" not in res["ack"]["applied"] and "link" not in res["ack"]["applied"]
    assert "не применилась" in res["note"] and res["note"].endswith("Вернул прежнюю"), res["note"]
    assert "устройства админа" in res["note"] and "конфиг линка" in res["note"]
    assert "применено:" not in res["note"]
    assert not svc.db.get_state(svc._CFG_GEN_KEY)


def test_a_stale_handshake_is_not_taken_for_a_live_link(svc, host, monkeypatch):
    """Хендшейк старше минуты — от прежнего конфига: линк не поднялся."""
    _Clock(monkeypatch)
    host.how = "restarted"
    host.handshake = int(time.time()) - txn.FRESH_SECONDS - 30
    res = svc.apply_cfg(_doc(link=LINK_NEW_ENDPOINT))
    assert "link" in res["ack"]["failed"] and host.installed_link_print() == gwcfg.link_print(LINK)


def test_the_script_refusing_the_link_conf_rolls_back_the_unit(svc, host):
    """Второй рубеж — скрипт обвязки — отказал: юнит, уже переписанный,
    возвращается; причина серверу — последняя строка вывода скрипта."""
    host.link_rc = 1
    res = svc.apply_cfg(_doc(env={"ADMIN_IPS": "10.8.1.2 10.8.1.7"}, link=LINK_NEW_KA))
    assert host.text() == UNIT_TEXT
    assert host.installed_link_print() == gwcfg.link_print(LINK)
    failed = res["ack"]["failed"]
    assert failed["link"].startswith("конфиг линка: ") and "не прошёл проверку" in failed["link"], failed
    assert "Вернул прежнюю" in res["note"]


def test_a_failed_reassert_rolls_back_both(svc, host):
    """Обвязка с новыми значениями не перевыставилась (systemctl restart
    отказал) — откат и юнита, и конфига линка."""
    host.restart = [_cp(1, err=b"Job failed"), _cp(0)]
    res = svc.apply_cfg(_doc(env={"ADMIN_IPS": "10.8.1.2 10.8.1.7"}, link=LINK_NEW_KA))
    assert host.text() == UNIT_TEXT and host.installed_link_print() == gwcfg.link_print(LINK)
    assert host.link_applies == ["link.next", "link.prev"]
    assert set(res["ack"]["failed"]) == {"env", "link"}


def test_an_activating_unit_asks_the_server_to_retry_and_touches_nothing(svc, host, monkeypatch):
    """Юнит обвязки ещё стартует (загрузка, применение файла): транзакцию не
    начинаем, серверу — retry (он повторит), человеку — ничего: временный
    отказ уведомлением не является."""
    _Clock(monkeypatch)
    host.active = "activating"
    res = svc.apply_cfg(_doc(env={"ADMIN_IPS": "10.8.1.2 10.8.1.7"}, link=LINK_NEW_KA, server={"name": "AWG-SRV"}))
    assert res["ack"].get("retry") is True and "env" in res["ack"]["failed"], res["ack"]
    assert res["note"] == "" and res["ask"] == []
    assert host.text() == UNIT_TEXT and host.link_runs == [] and host.restarts == 0
    assert not svc.db.get_state(svc._CFG_GEN_KEY)


def test_a_unit_that_cannot_be_rewritten_fails_without_touching_the_link(svc, host):
    """Юнит обвязки не переписать (daemon-reload отказал): конфиг линка не
    ставим вовсе, серверу — причина."""
    host.reload = [_cp(1, err=b"Access denied")]
    res = svc.apply_cfg(_doc(env={"ADMIN_IPS": "10.8.1.2 10.8.1.7"}, link=LINK_NEW_KA))
    assert host.link_runs == [] and host.text() == UNIT_TEXT
    assert res["ack"]["failed"]["env"].startswith("юнит не переписан"), res["ack"]


# ── имя сервера ──────────────────────────────────────────────────────────────

def test_server_name_is_stored_and_named_in_the_note(svc, host):
    res = svc.apply_cfg(_doc(server={"name": "AWG-SRV"}))
    assert svc.db.get_state(svc._SERVER_NAME_KEY) == "AWG-SRV"
    assert res["note"].endswith("применено: имя сервера") and res["ack"]["applied"] == ["server", "env"]
    again = svc.apply_cfg(_doc(server={"name": "AWG-SRV"}))
    assert again["note"] == "", "то же имя второй раз — уведомление ни о чём"


# ── очередь ответа ───────────────────────────────────────────────────────────

def test_the_ack_queue_keeps_one_answer_until_cleared(svc, host):
    """Ответ, который не ушёл (линк перезапускался), лежит в БД до новой
    сессии; после отправки очередь пуста."""
    assert svc.cfg_ack_pending_get() == {}
    ack = gwcfg.ack_build(7, DIGEST, applied=["env"])
    svc.cfg_ack_pending_put(ack)
    got = svc.cfg_ack_pending_get()
    assert {k: v for k, v in got.items() if k != "at"} == ack and got.get("at")
    svc.cfg_ack_pending_clear()
    assert svc.cfg_ack_pending_get() == {}


def test_the_answer_ack_names_the_asking_document(svc, host):
    """Ответ человека уходит серверу от имени документа, который спросил:
    принял — раздел в applied, оставил своё — в declined с серверным
    отпечатком; вопроса не было — ответа нет."""
    yes = svc.cfg_answer_ack({"ok": True, "section": "mail", "accepted": True, "gen": 9, "hash": DIGEST, "print": "ab12"})
    assert yes == gwcfg.ack_build(9, DIGEST, applied=["mail"])
    no = svc.cfg_answer_ack({"ok": True, "section": "backup", "accepted": False, "gen": 9, "hash": DIGEST,
                             "print": "ab12"})
    assert no["declined"] == {"backup": "ab12"} and no["applied"] == []
    assert svc.cfg_answer_ack({"ok": False, "section": "mail"}) is None


def test_link_next_is_rendered_canonically(svc, host):
    """На диск уходит канонический текст со штампом контракта: снимок читает
    штамп, а лишние комментарии в конфиг для awg-quick не попадают."""
    svc.apply_cfg(_doc(link=LINK_NEW_KA))
    text = host.etc_seen[0]["link.next"][0]
    assert text.startswith("# awg-bot: контракт линка 1\n")
    assert text == wgconf.render(wgconf.parse(LINK_NEW_KA), "link", stamp="1")
