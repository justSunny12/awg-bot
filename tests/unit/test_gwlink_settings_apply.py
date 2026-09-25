"""
Настройки с сервера по каналу линка — сторона шлюза: закрытый список ключей, строгая проверка значений,
правка строк юнита обвязки, перезапуск ровно тогда, когда есть что менять, и
откат к прежнему тексту юнита при неудаче.

Настройки приходят без кнопки человека, поэтому цена ошибки здесь выше, чем у
бандла: лишний перезапуск обвязки — это рестарт dnsmasq и перевыставление
файервола в квартире посреди дня; пропущенная в юнит кавычка или перевод строки
— чужая строка в окружении скрипта, который исполняется от root; недокаченный
откат — «половина новых настроек», с которой шлюз живёт до следующего бандла.

Хост подменён целиком классом `_Unit`: файл юнита во временном каталоге (по
тому же пути, что читает `gwguard`) и `systemctl`, который записывает вызовы и
отвечает так, как велит сценарий. Настоящий systemd и nft здесь не нужны:
проверяется, что оказалось в юните и сколько раз его перезапускали.
"""
from __future__ import annotations

import subprocess
import threading
import types
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.domain import gateway as gwmod
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from awgbot.util import gwlink

pytestmark = pytest.mark.unit

# Юнит так, как его пишет install/routing-gw-setup.sh (раздел «Автозапуск»):
# часть значений в кавычках, часть без, ключи рядом с настройками. Конфига
# аплинка (UPLINK_B64) в юните больше нет — он едет только на время применения.
UNIT_TEXT = """[Unit]
Description=awg-bot: линк до сервера AWG и изоляция клиентов (шлюз)
After=network-online.target awg-quick@awg0.service
Wants=network-online.target awg-quick@awg0.service

[Service]
Type=oneshot
RemainAfterExit=yes
# Подсеть вшита: app.yaml на шлюзе нет, смена подсети = новый бандл с ВПС.
Environment=CLIENT_SUBNET=10.8.1.0/24
Environment=LINK_IF=awglink
Environment="ADMIN_IPS=10.8.1.2"
Environment=GATEWAY_PUBKEY=AAAAPUBKEY=
Environment=GATEWAY_PREV_PUBKEY=
Environment=LAN_MODE=1
Environment="HOME_SUBNETS=192.168.68.0/24"
Environment=RESOLVER=10.9.1.1
Environment="PEER_HOME_NETS="
Environment=LINK_CHANNEL=1
Environment=LINK_CHANNEL_PORT=8787
EnvironmentFile=-/etc/awg-gw/firewall.env
ExecStart=/usr/local/sbin/routing-gw-setup.sh --apply /etc/amnezia/amneziawg/awglink.conf
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""

# Ровно то, что стоит в UNIT_TEXT: первая доставка на неизменённой системе.
SAME = {"ADMIN_IPS": "10.8.1.2", "HOME_SUBNETS": "192.168.68.0/24", "LAN_MODE": "1",
        "RESOLVER": "10.9.1.1"}


def _cp(rc=0, out=b"", err=b""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


class _Unit:
    """Шлюз для этих тестов: юнит обвязки во временном файле и systemctl.

    `restart` — чем отвечает `systemctl restart` юнита: список исходов по
    порядку вызовов (последний повторяется); исход — CompletedProcess или
    исключение, которое бросит subprocess.run. `started_with` — с каким
    окружением юнит стартовал при каждом рестарте (что увидел скрипт обвязки).
    """

    def __init__(self, tmp_path, monkeypatch, text: str | None = UNIT_TEXT):
        self.path = tmp_path / config.GW_UNIT
        if text is not None:
            self.path.write_text(text, encoding="utf-8")
        self.calls: list[list[str]] = []
        self.restart: list = [_cp(0)]
        self.reload: list = [_cp(0)]                   # исходы `systemctl daemon-reload`
        self.started_with: list[dict] = []
        self.gate: threading.Event | None = None      # держать рестарт, пока не отпустят
        self.in_restart = threading.Event()
        target = f"/etc/systemd/system/{config.GW_UNIT}"
        real_path = Path

        def _path(p, *rest):
            return self.path if str(p) == target and not rest else real_path(p, *rest)

        monkeypatch.setattr(gwguard, "Path", _path)
        fake = types.SimpleNamespace(run=self._run, CompletedProcess=subprocess.CompletedProcess,
                                     SubprocessError=subprocess.SubprocessError,
                                     TimeoutExpired=subprocess.TimeoutExpired)
        monkeypatch.setattr(gwguard, "subprocess", fake)

    def _run(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:2] == ["systemctl", "restart"]:
            self.started_with.append({k: gwguard.unit_env(k) for k in gwlink.SETTINGS_KEYS})
            self.in_restart.set()
            if self.gate is not None:
                self.gate.wait(5)
            n = len([c for c in self.calls if c[:2] == ["systemctl", "restart"]])
            outcome = self.restart[min(n, len(self.restart)) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        if argv[:2] == ["systemctl", "daemon-reload"]:
            n = len([c for c in self.calls if c[:2] == ["systemctl", "daemon-reload"]])
            outcome = self.reload[min(n, len(self.reload)) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return _cp(0)

    @property
    def restarts(self) -> int:
        return len([c for c in self.calls if c[:2] == ["systemctl", "restart"]])

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def env(self) -> dict:
        return {k: gwguard.unit_env(k) for k in gwlink.SETTINGS_KEYS}


@pytest.fixture()
def svc(tmp_path):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    yield GatewayServices(d)
    d.close()


@pytest.fixture()
def unit(tmp_path, monkeypatch):
    return _Unit(tmp_path, monkeypatch)


# ── закрытый список ключей и секреты ─────────────────────────────────────────

def test_the_list_of_keys_is_closed_and_changes_only_on_purpose():
    """Канал возит данные из закрытого списка — ровно то, что бандл кладёт в юнит
    настройками. Новый ключ, добавленный «заодно», должен уронить этот тест: его
    придётся вписать сюда и объяснить в сообщении коммита, почему это данные, а
    не код и не секрет."""
    assert gwlink.SETTINGS_KEYS == ("ADMIN_IPS", "HOME_SUBNETS", "LAN_MODE", "RESOLVER"), (
        "список ключей канала изменился без правки теста")


def test_neighbour_subnets_stay_bundle_only():
    """Подсети за другими шлюзами живут на малине ещё и в AllowedIPs пира ВПС в
    конфиге линка, а его правит только бандл. Доставь их канал в юнит — карточка
    показала бы «совпадает», а доступ между подсетями не работал бы."""
    assert "PEER_HOME_NETS" in gwlink.BUNDLE_ONLY_KEYS
    assert "PEER_HOME_NETS" not in gwlink.SETTINGS_KEYS
    assert not set(gwlink.BUNDLE_ONLY_KEYS) & set(gwlink.SETTINGS_KEYS)


def test_no_secret_and_no_port_ever_rides_the_channel():
    """Приватный ключ аплинка, токен агента, почта, фраза копий, ключи линка —
    только бандлом, который несёт человек. Порт канала каналом не едет: смену
    порта по старому порту доставить некому."""
    never = {"UPLINK_B64", "MAIL_B64", "BACKUP_B64", "AGENT_BOT_TOKEN", "GW_BOT_TOKEN",
             "GATEWAY_PUBKEY", "GATEWAY_PREV_PUBKEY", "LINK_CHANNEL", "LINK_CHANNEL_PORT",
             "CLIENT_SUBNET", "LINK_IF", "SERVER_NAME"}
    assert not never & set(gwlink.SETTINGS_KEYS)
    for key in gwlink.SETTINGS_KEYS:
        assert not any(w in key for w in ("KEY", "TOKEN", "B64", "PASS", "SECRET", "PORT")), key


def test_a_secret_slipped_into_the_message_is_dropped_not_applied():
    """Незнакомый ключ игнорируется (новый ВПС, старый агент), а не кладётся в
    юнит: скомпрометированный ВПС не должен подменить аплинк малины сообщением."""
    got = gwlink.validate_settings({**SAME, "UPLINK_B64": "QUJD", "ExecStart": "/bin/sh"})
    assert set(got) == set(gwlink.SETTINGS_KEYS), f"в разобранное попало лишнее: {sorted(got)}"


# ── строгая проверка значений ────────────────────────────────────────────────

def test_a_normal_set_passes_and_whitespace_is_normalised():
    got = gwlink.validate_settings({**SAME, "HOME_SUBNETS": "  192.168.68.0/24\n 10.20.0.0/16 ",
                                    "ADMIN_IPS": "10.8.1.2  10.8.1.3"})
    assert got["HOME_SUBNETS"] == "192.168.68.0/24 10.20.0.0/16"
    assert got["ADMIN_IPS"] == "10.8.1.2 10.8.1.3"
    assert gwlink.validate_settings({**SAME, "LAN_MODE": "0", "RESOLVER": ""})["RESOLVER"] == ""


@pytest.mark.parametrize("key,value", [
    ("HOME_SUBNETS", '192.168.68.0/24"\nExecStart=/bin/sh -c id'),
    ("HOME_SUBNETS", "192.168.68.0/24; reboot"),
    ("HOME_SUBNETS", "$(reboot)"),
    ("HOME_SUBNETS", "`id`"),
    ("HOME_SUBNETS", "192.168.68.0"),                  # CIDR без маски
    ("HOME_SUBNETS", "fd00:1::/64"),                   # IPv6 — обвязка его не знает
    ("HOME_SUBNETS", "192.168.68.0/33"),
    ("HOME_SUBNETS", "192.168.68.0/24 'x'"),
    ("ADMIN_IPS", "10.8.1.2;10.8.1.3"),
    ("ADMIN_IPS", "fd00::2"),
    ("ADMIN_IPS", "10.8.1.0/24"),                      # адрес, а не сеть
    ("ADMIN_IPS", "10.8.1.2\"\nEnvironment=\"UPLINK_B64=x"),
    ("RESOLVER", "10.9.1.1 10.9.1.2"),
    ("RESOLVER", "10.9.1.1/32"),
    ("RESOLVER", "::1"),
    ("RESOLVER", "resolver.local"),
    ("LAN_MODE", ""),                                  # пустой режим — не «выключено»
    ("LAN_MODE", "2"),
    ("LAN_MODE", "yes"),
    ("LAN_MODE", "1\nExecStart=/bin/sh"),
])
def test_a_single_bad_value_rejects_the_whole_message(key, value):
    """Не прошло одно значение — отвергается всё сообщение: половина новых
    настроек хуже старых целиком. И ни кавычки, ни перевода строки, ни `;` —
    значению нечем выйти ни из строки юнита, ни в оболочку."""
    with pytest.raises(gwlink.ProtocolError):
        gwlink.validate_settings({**SAME, key: value})


def test_a_message_without_a_key_or_not_a_dict_is_refused():
    """Недостающий ключ не читается как «пусто»: иначе ВПС, забывший поле,
    молча снял бы подсети или устройства админа на шлюзе."""
    partial = dict(SAME)
    partial.pop("ADMIN_IPS")
    with pytest.raises(gwlink.ProtocolError):
        gwlink.validate_settings(partial)
    for junk in (None, [], "ADMIN_IPS=1.2.3.4", 42):
        with pytest.raises(gwlink.ProtocolError):
            gwlink.validate_settings(junk)


def test_a_list_longer_than_the_limit_is_refused():
    nets = " ".join(f"10.{i}.0.0/24" for i in range(65))
    with pytest.raises(gwlink.ProtocolError):
        gwlink.validate_settings({**SAME, "HOME_SUBNETS": nets})
    ok = " ".join(f"10.{i}.0.0/24" for i in range(64))
    assert gwlink.validate_settings({**SAME, "HOME_SUBNETS": ok})["HOME_SUBNETS"] == ok


def test_the_fingerprint_depends_on_content_not_on_spelling():
    """Отпечаток решает, слали ли уже этот набор. Лишний пробел или порядок ключей
    не должны делать из того же набора «новый» (обвязку рестартили бы зря), а
    смена одного значения — обязана."""
    base = gwlink.settings_hash(SAME)
    spaced = {k: f"  {v}  " for k, v in reversed(list(SAME.items()))}
    assert gwlink.settings_hash(spaced) == base
    assert gwlink.settings_hash({**SAME, "SOMETHING_ELSE": "x"}) == base
    assert gwlink.settings_hash({**SAME, "HOME_SUBNETS": "192.168.70.0/24"}) != base
    assert gwlink.settings_hash({**SAME, "LAN_MODE": "0"}) != base


# ── правка юнита ─────────────────────────────────────────────────────────────

def test_rewriting_the_unit_touches_only_the_named_lines(unit):
    """Юнит несёт и ключи шлюза, и чужие строки. Правка настроек обязана
    поменять ровно свои строки, а прочее оставить байт в байт — иначе доставка
    подсетей однажды сотрёт ключ, по которому скрипт узнаёт свою машину."""
    before = gwguard.unit_set_env({"HOME_SUBNETS": "192.168.70.0/24 10.20.0.0/16",
                                   "RESOLVER": ""})
    assert before == UNIT_TEXT, "для отката возвращён не прежний текст"
    after = unit.text()
    assert 'Environment="HOME_SUBNETS=192.168.70.0/24 10.20.0.0/16"' in after
    assert 'Environment="RESOLVER="' in after
    changed = [(a, b) for a, b in zip(UNIT_TEXT.splitlines(), after.splitlines()) if a != b]
    assert len(changed) == 2 and len(after.splitlines()) == len(UNIT_TEXT.splitlines()), changed
    # тот же юнит читает скрипт обвязки и снимок агента — форматы обязаны сойтись
    assert gwguard.unit_env("HOME_SUBNETS") == "192.168.70.0/24 10.20.0.0/16"
    assert gwguard.unit_env("RESOLVER") == ""
    assert gwguard.unit_env("GATEWAY_PUBKEY") == "AAAAPUBKEY="
    assert ["systemctl", "daemon-reload"] in unit.calls, "systemd не перечитал изменённый юнит"


def test_a_unit_rewritten_by_the_channel_stays_readable_only_by_root(unit):
    """Скрипт обвязки ставит юниту 0600, канал переписывает его через
    временный файл. Вернись права к 0644 после первой доставки подсетей —
    любой локальный пользователь малины (OMV, шары) читал бы юнит, в том числе
    юнит прежнего выпуска, где ещё лежит конфиг аплинка с приватным ключом."""
    unit.path.chmod(0o644)                        # юнит прежнего выпуска
    gwguard.unit_set_env({"HOME_SUBNETS": "192.168.70.0/24"})
    assert unit.path.stat().st_mode & 0o777 == 0o600, oct(unit.path.stat().st_mode & 0o777)
    assert not list(unit.path.parent.glob("*.tmp")), "временный файл юнита остался рядом"


def test_admin_addresses_written_by_the_channel_read_back_as_a_list(unit):
    """Файервол шлюза берёт устройства админа из юнита; список из трёх адресов
    обязан прочитаться тремя адресами, а не одним со скобкой."""
    gwguard.unit_set_env({"ADMIN_IPS": "10.8.1.2 10.8.1.3 10.8.1.4"})
    assert gwguard.unit_admin_ips() == ["10.8.1.2", "10.8.1.3", "10.8.1.4"]


def test_an_unquoted_line_rewritten_in_quotes_reads_back_the_same(unit):
    """Скрипт пишет часть строк без кавычек (LAN_MODE, RESOLVER), канал — всегда
    в кавычках. Читатель юнита обязан видеть одно и то же значение в обоих
    видах: иначе снимок после доставки показал бы расхождение, которого нет."""
    gwguard.unit_set_env({"LAN_MODE": "1", "RESOLVER": "10.9.1.2"})
    assert 'Environment="LAN_MODE=1"' in unit.text()
    assert gwguard.unit_env("LAN_MODE") == "1" and gwguard.lan_mode() is True
    assert gwguard.unit_env("RESOLVER") == "10.9.1.2"


def test_a_key_missing_from_an_old_unit_is_added_before_the_start_line(unit):
    """Обвязка старого образца — без строки PEER_HOME_NETS. Добавленная строка
    обязана стоять до ExecStart, иначе скрипт её не увидит."""
    unit.path.write_text(UNIT_TEXT.replace('Environment="PEER_HOME_NETS="\n', ""), encoding="utf-8")
    gwguard.unit_set_env({"PEER_HOME_NETS": "10.30.0.0/24"})
    text = unit.text()
    assert text.index('Environment="PEER_HOME_NETS=10.30.0.0/24"') < text.index("EnvironmentFile=")
    assert gwguard.unit_env("PEER_HOME_NETS") == "10.30.0.0/24"


def test_a_unit_without_a_start_line_is_left_alone(unit):
    broken = "[Service]\nEnvironment=LAN_MODE=1\n"
    unit.path.write_text(broken, encoding="utf-8")
    with pytest.raises(gwguard.GwGuardError):
        gwguard.unit_set_env({"HOME_SUBNETS": "192.168.70.0/24"})
    assert unit.text() == broken and unit.calls == []


def test_the_unit_writer_refuses_quotes_and_newlines_on_its_own(unit):
    """Вторая линия обороны: даже если проверку сообщения когда-нибудь ослабят,
    запись в юнит сама не пропустит кавычку и перевод строки."""
    for bad in ('192.168.1.0/24"\nExecStart=/bin/sh', "1;reboot", "a"):
        with pytest.raises(gwguard.GwGuardError):
            gwguard.unit_set_env({"HOME_SUBNETS": bad})
    assert unit.text() == UNIT_TEXT and unit.calls == []


def test_restoring_the_unit_brings_back_the_text_byte_for_byte(unit):
    before = gwguard.unit_set_env({"HOME_SUBNETS": "192.168.70.0/24"})
    gwguard.unit_restore(before)
    assert unit.text() == UNIT_TEXT
    assert unit.calls.count(["systemctl", "daemon-reload"]) == 2


# ── применение на агенте ─────────────────────────────────────────────────────

def test_new_settings_rewrite_the_unit_and_restart_it_exactly_once(svc, unit):
    """Норма: ВПС поменял подсети — юнит переписан, обвязка перезапущена один
    раз и стартовала уже с новыми значениями."""
    res = svc.apply_link_settings({**SAME, "HOME_SUBNETS": "192.168.70.0/24"})
    assert res == {"ok": True, "changed": ["HOME_SUBNETS"], "error": ""}
    assert unit.env()["HOME_SUBNETS"] == "192.168.70.0/24"
    assert unit.restarts == 1, f"обвязку перезапустили {unit.restarts} раз"
    assert unit.started_with[0]["HOME_SUBNETS"] == "192.168.70.0/24", (
        "перезапуск прошёл до записи юнита — скрипт стартовал со старыми значениями")
    reload_at = unit.calls.index(["systemctl", "daemon-reload"])
    restart_at = next(i for i, c in enumerate(unit.calls) if c[:2] == ["systemctl", "restart"])
    assert reload_at < restart_at, "рестарт до daemon-reload берёт старое окружение юнита"


def test_settings_matching_the_unit_touch_nothing(svc, unit):
    """Миграция: первое сообщение после включения канала несёт те же
    значения, что последний бандл. Ни записи, ни перезапуска — dnsmasq не
    рестартует, квартира ничего не замечает."""
    res = svc.apply_link_settings(dict(SAME))
    assert res == {"ok": True, "changed": [], "error": ""}
    assert unit.calls == [], f"при совпадении дёрнули systemctl: {unit.calls}"
    assert unit.text() == UNIT_TEXT


def test_settings_that_differ_only_in_spacing_touch_nothing(svc, unit):
    res = svc.apply_link_settings({k: f" {v} " for k, v in SAME.items()})
    assert res["changed"] == [] and unit.calls == []


def test_a_repeated_delivery_restarts_nothing_the_second_time(svc, unit):
    """Повтор того же набора (новая сессия после обрыва) — не повод второй раз
    перезапускать обвязку."""
    new = {**SAME, "ADMIN_IPS": "10.8.1.2 10.8.1.3"}
    assert svc.apply_link_settings(new)["changed"] == ["ADMIN_IPS"]
    assert svc.apply_link_settings(new) == {"ok": True, "changed": [], "error": ""}
    assert unit.restarts == 1


def test_a_failed_restart_puts_the_old_unit_back_and_restarts_again(svc, unit):
    """Скрипт обвязки не принял новые значения. Лучше жить со старыми
    настройками, чем с половиной новых: юнит — байт в байт прежний, и обвязка
    перезапущена ещё раз уже с ним."""
    unit.restart = [_cp(1, err=b"routing-gw-setup: LAN-interface not found"), _cp(0)]
    res = svc.apply_link_settings({**SAME, "HOME_SUBNETS": "192.168.70.0/24", "LAN_MODE": "1"})
    assert res["ok"] is False and res["changed"] == ["HOME_SUBNETS"]
    assert "LAN-interface not found" in res["error"], "человеку не сказали, почему не применилось"
    assert unit.text() == UNIT_TEXT, "откат вернул не тот текст юнита"
    assert unit.restarts == 2, "после отката обвязку не перезапустили — таблица осталась от новой попытки"
    assert unit.started_with[-1]["HOME_SUBNETS"] == "192.168.68.0/24"


def test_a_failed_rollback_restart_still_reports_the_first_error(svc, unit):
    unit.restart = [_cp(1, err=b"first failure"), _cp(1, err=b"second failure")]
    res = svc.apply_link_settings({**SAME, "RESOLVER": ""})
    assert res["ok"] is False and "first failure" in res["error"]
    assert unit.text() == UNIT_TEXT


@pytest.mark.parametrize("boom", [
    subprocess.TimeoutExpired(["systemctl", "restart"], 90),
    FileNotFoundError(2, "systemctl"),
])
def test_a_restart_that_hangs_or_cannot_run_still_rolls_back(svc, unit, boom):
    """Отказ внешней команды — не только ненулевой код. Скрипт обвязки, зависший
    дольше таймаута (apt, DNS), или systemctl, которого нет, обязаны закончиться
    тем же откатом и честным «не применилось». Иначе новые значения остаются в
    юните без перевыставленной таблицы, а следующая доставка видит «совпало» и
    отвечает «ок» — расхождение на шлюзе прячется до ребута."""
    unit.restart = [boom, _cp(0)]
    try:
        res = svc.apply_link_settings({**SAME, "HOME_SUBNETS": "192.168.70.0/24"})
    except Exception as e:                                 # noqa: BLE001
        pytest.fail(f"применение настроек выпустило исключение наружу: {e!r}")
    assert res["ok"] is False, "зависший перезапуск объявлен успехом"
    assert unit.text() == UNIT_TEXT, "юнит остался с новыми значениями после отказа"


@pytest.mark.parametrize("boom", [
    subprocess.TimeoutExpired(["systemctl", "daemon-reload"], 30),
    FileNotFoundError(2, "systemctl"),
])
def test_a_reload_that_fails_after_the_write_leaves_no_half_applied_unit(svc, unit, boom):
    """Юнит уже переписан, а `daemon-reload` завис или не запустился. Ответ
    «не применилось» при новых значениях в юните — худший из исходов: следующая
    доставка увидит «совпало», ответит «ок» и больше ничего не перезапустит, а
    обвязка так и будет жить со старой таблицей до ребута."""
    unit.reload = [boom, _cp(0)]
    try:
        res = svc.apply_link_settings({**SAME, "HOME_SUBNETS": "192.168.70.0/24"})
    except Exception as e:                                 # noqa: BLE001
        pytest.fail(f"применение настроек выпустило исключение наружу: {e!r}")
    assert res["ok"] is False
    assert unit.text() == UNIT_TEXT, (
        "сказано «не применилось», а в юните остались новые значения")


def test_neighbour_subnets_sent_by_the_server_are_not_written_to_the_unit(svc, unit):
    """Старый или взломанный ВПС прислал PEER_HOME_NETS каналом. В юнит это не
    ложится: без конфига линка половина функции B хуже, чем её отсутствие."""
    res = svc.apply_link_settings({**SAME, "PEER_HOME_NETS": "10.30.0.0/24"})
    assert res == {"ok": True, "changed": [], "error": ""}
    assert unit.text() == UNIT_TEXT and unit.calls == []


def test_invalid_settings_touch_nothing(svc, unit):
    res = svc.apply_link_settings({**SAME, "HOME_SUBNETS": "192.168.70.0/24\nExecStart=/bin/sh"})
    assert res["ok"] is False and res["changed"] == [] and res["error"]
    assert unit.calls == [] and unit.text() == UNIT_TEXT


def test_a_missing_unit_is_reported_and_nothing_is_restarted(svc, tmp_path, monkeypatch):
    """Обвязка не развёрнута (юнита нет): перезапускать нечего, и говорить
    «применено» нельзя."""
    host = _Unit(tmp_path, monkeypatch, text=None)
    res = svc.apply_link_settings({**SAME, "HOME_SUBNETS": "192.168.70.0/24"})
    assert res["ok"] is False and "юнит" in res["error"]
    assert host.restarts == 0


# ── общий замок с бандлом ────────────────────────────────────────────────────

def test_the_bundle_waits_for_channel_settings_to_finish(svc, unit, tmp_path, monkeypatch):
    """Бандл из чата и настройки из канала применяются одним скриптом через один
    юнит. Вперемешку они переписали бы юнит друг другу посреди прогона: бандл
    обязан дождаться, пока перезапуск с настройками канала закончится."""
    import base64
    import os
    import tempfile
    from awgbot.util import bundlecrypt as bc
    mine = base64.b64encode(os.urandom(32)).decode()
    conf = tmp_path / "awglink.conf"
    conf.write_text("[Interface]\nPrivateKey = " + mine + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(conf))
    monkeypatch.setattr(tempfile, "mkstemp", lambda **kw: (
        os.open(str(tmp_path / "b.sh"), os.O_RDWR | os.O_CREAT), str(tmp_path / "b.sh")))
    bundle_ran = threading.Event()
    monkeypatch.setattr(gwmod, "_run", lambda a, timeout=10: bundle_ran.set() or _cp(0, b"ok"))

    unit.gate = threading.Event()
    results: dict = {}
    t_set = threading.Thread(target=lambda: results.update(
        s=svc.apply_link_settings({**SAME, "HOME_SUBNETS": "192.168.70.0/24"})))
    t_set.start()
    assert unit.in_restart.wait(3), "применение настроек не дошло до перезапуска"
    t_bun = threading.Thread(target=lambda: results.update(
        b=svc.apply_bundle(bc.encrypt(b"#__GW_SETUP_BELOW__\n__LINK_CONF_EOF__\n", mine))))
    t_bun.start()
    try:
        assert not bundle_ran.wait(0.3), "бандл запустился посреди перезапуска с настройками канала"
    finally:
        unit.gate.set()
        t_set.join(5)
        t_bun.join(5)
    assert bundle_ran.is_set() and results["b"][0] is True, "бандл так и не применился после замка"
    assert results["s"]["ok"] is True


def test_channel_settings_wait_for_a_running_bundle(svc, unit):
    """Обратное направление: пока идёт бандл (он держит тот же замок), настройки
    из канала юнит не трогают."""
    done = threading.Event()
    with gwmod._APPLY_LOCK:
        t = threading.Thread(target=lambda: (svc.apply_link_settings(
            {**SAME, "HOME_SUBNETS": "192.168.70.0/24"}), done.set()))
        t.start()
        assert not done.wait(0.3)
        assert unit.text() == UNIT_TEXT and unit.calls == [], "юнит переписан посреди бандла"
    t.join(5)
    assert done.is_set() and unit.env()["HOME_SUBNETS"] == "192.168.70.0/24"


def test_nothing_to_change_does_not_wait_for_the_lock(svc, unit):
    """Совпадение — ответ сразу, без ожидания бандла: иначе ack «ничего не
    изменилось» висел бы до трёх минут прогона бандла."""
    done = threading.Event()
    with gwmod._APPLY_LOCK:
        t = threading.Thread(target=lambda: (svc.apply_link_settings(dict(SAME)), done.set()))
        t.start()
        assert done.wait(2), "пустое применение ждало замка бандла"
    t.join(5)


# ── уведомление человеку ─────────────────────────────────────────────────────

def test_the_note_names_what_changed_in_human_words(svc):
    ok = svc.link_settings_note({"ok": True, "changed": ["HOME_SUBNETS", "ADMIN_IPS"], "error": ""})
    assert ok == ("⚙️ Сервер AWG прислал новые настройки шлюза — применены: "
                  "локальные подсети, устройства админа.")
    fail = svc.link_settings_note({"ok": False, "changed": ["RESOLVER"], "error": "exit 1"})
    assert "резолвер" in fail and "не применились: exit 1" in fail and "Вернул прежние" in fail
    rejected = svc.link_settings_note({"ok": False, "changed": [], "error": "LAN_MODE: недопустимое значение"})
    assert rejected.startswith("⚠️") and "LAN_MODE: недопустимое значение" in rejected
