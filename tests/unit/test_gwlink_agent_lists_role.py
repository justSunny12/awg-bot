"""
Агент шлюза, этапы 3 и 4 концепта «канал линка»: фиды локальной сети,
привезённые каналом; роль слота, сказанная сервером.

Фиды приходят с ВПС, который может быть взломан. Цена ошибки: сжатая «бомба»
съедает память малины, чужой фид под видом нашего ложится в dnsmasq всей
квартиры.
И обратная сторона: пока канал возит фиды, адрес квартиры не должен сам ходить
за ними на GitHub и в Google — ради этого этап и затевался.

Хост подменён: скрипт списков — функцией, которая записывает, откуда её
попросили брать фиды; юнит обвязки — классом `_Unit` из тестов применения
настроек (режим без VPN читается из него, как на малине).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import zlib

import pytest

from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from tests.unit.test_gwlink_settings_apply import UNIT_TEXT, _Unit

pytestmark = pytest.mark.unit

DOMAINS = "".join(f"ipset=/site{i}.org/vpn_domains\n" for i in range(20))
NETS = "91.108.4.0/22\n149.154.160.0/20\n"


def _digest(domains: str, nets: str) -> str:
    return hashlib.sha256((domains + "\n--\n" + nets).encode()).hexdigest()


def _pack(domains: str = DOMAINS, nets: str = NETS, **extra) -> str:
    body = {"domains": domains, "nets": nets, **extra}
    return base64.b64encode(zlib.compress(json.dumps(body).encode(), 9)).decode()


class _Lists:
    """Скрипт списков на малине: что ему велели и чем он ответил."""

    def __init__(self, tmp_path, monkeypatch):
        self.calls: list[str] = []                 # from_dir каждого запуска; "" — качать самому
        self.result = (True, "")
        self.seen: dict[str, str] = {}             # что лежало в каталоге фидов при запуске
        script = tmp_path / "awg-lan-lists.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(gwguard, "LAN_LISTS_SCRIPT", str(script))
        self.dir = tmp_path / "feed"
        monkeypatch.setattr(gwguard, "LAN_FEED_DIR", str(self.dir))

        def run(timeout: int = 600, from_dir: str = ""):
            self.calls.append(from_dir)
            if from_dir:
                self.seen = {n: open(os.path.join(from_dir, n), encoding="utf-8").read()
                             for n in ("domains.lst", "nets.lst")}
            return self.result

        monkeypatch.setattr(gwguard, "run_lan_lists", run)

    @property
    def downloads(self) -> int:
        return len([c for c in self.calls if not c])


@pytest.fixture()
def svc(tmp_path):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    yield GatewayServices(d)
    d.close()


@pytest.fixture()
def lan(tmp_path, monkeypatch):
    """Малина с включённым режимом без VPN и скриптом списков."""
    _Unit(tmp_path, monkeypatch)                          # в UNIT_TEXT стоит LAN_MODE=1
    return _Lists(tmp_path, monkeypatch)


# ── фиды из канала ───────────────────────────────────────────────────────────

def test_channel_feeds_are_handed_to_the_lists_script_instead_of_the_network(svc, lan):
    """Норма: фиды легли в каталог, скрипт списков взял их оттуда, а не пошёл
    на GitHub; отпечаток запомнен, и своё скачивание по расписанию молчит."""
    res = svc.apply_lan_feeds(_digest(DOMAINS, NETS), _pack())
    assert res == {"ok": True, "error": ""}
    assert lan.calls == [str(lan.dir)], "скрипт списков не получил каталог с фидами канала"
    assert lan.seen == {"domains.lst": DOMAINS, "nets.lst": NETS}
    assert svc.lan_feeds_applied_hash() == _digest(DOMAINS, NETS)
    assert svc.lan_feeds_from_channel_fresh() is True


def test_the_same_feeds_again_do_not_rerun_the_script(svc, lan):
    """Повтор того же набора (переподключение) — только отметка свежести:
    лишний прогон скрипта — лишний рестарт dnsmasq, а он роняет кэш всей сети."""
    d = _digest(DOMAINS, NETS)
    svc.apply_lan_feeds(d, _pack())
    svc.db.set_state(svc._LAN_CHANNEL_AT_KEY, str(int(time.time()) - 13 * 3600))
    assert svc.lan_feeds_from_channel_fresh() is False
    assert svc.apply_lan_feeds(d, _pack()) == {"ok": True, "error": ""}
    assert lan.calls == [str(lan.dir)], "тот же фид прогнал скрипт второй раз"
    assert svc.lan_feeds_from_channel_fresh() is True, "свежесть не обновилась"


@pytest.mark.parametrize("packed,why", [
    ("не base64!!", "мусор вместо base64"),
    (base64.b64encode(b"not zlib at all").decode(), "не сжатое"),
    (base64.b64encode(zlib.compress(b"[1, 2, 3]")).decode(), "не объект"),
    (base64.b64encode(zlib.compress(b'{"domains": "x"}')).decode(), "нет подсетей"),
    (base64.b64encode(zlib.compress(b"\xff\xfe")).decode(), "не текст"),
])
def test_a_broken_feed_is_refused_and_nothing_is_written(svc, lan, packed, why):
    res = svc.apply_lan_feeds("0" * 64, packed)
    assert res["ok"] is False and res["error"], why
    assert lan.calls == [] and not lan.dir.exists(), f"{why}: до скрипта дошло"
    assert svc.lan_feeds_applied_hash() == ""


def test_a_feed_that_does_not_match_its_fingerprint_is_refused(svc, lan):
    """Отпечаток — единственное, что связывает присланное с тем, что ВПС
    объявил. Не сошёлся — это не наш фид, и в dnsmasq квартиры он не ложится."""
    res = svc.apply_lan_feeds(_digest(DOMAINS, NETS), _pack(domains=DOMAINS + "ipset=/evil.org/vpn_domains\n"))
    assert res["ok"] is False and "отпечаток" in res["error"]
    assert lan.calls == [] and svc.lan_feeds_applied_hash() == ""


def test_a_compressed_bomb_is_refused_without_unpacking_it_whole(svc, lan):
    """Двадцать мегабайт нулей сжимаются в десятки килобайт. Распаковка без
    потолка съела бы память малины — отказ обязан случиться на потолке."""
    bomb = json.dumps({"domains": "0" * (20 * 1024 * 1024), "nets": ""}).encode()
    packed = base64.b64encode(zlib.compress(bomb, 9)).decode()
    assert len(packed) < 100_000, "бомба получилась не бомбой — тест ничего не проверит"
    res = svc.apply_lan_feeds(_digest("0" * (20 * 1024 * 1024), ""), packed)
    assert res["ok"] is False and "предела" in res["error"]
    assert lan.calls == []


def test_an_oversized_message_is_refused_before_decoding(svc, lan):
    res = svc.apply_lan_feeds("0" * 64, "A" * (svc._LAN_FEED_MAX + 4))
    assert res["ok"] is False and "предела" in res["error"]


def test_feeds_are_refused_when_the_gateway_has_no_local_network_mode(svc, tmp_path, monkeypatch):
    """Режим без VPN выключен — dnsmasq квартиры не наш, и класть в него фиды
    нельзя, что бы ни прислал сервер."""
    _Unit(tmp_path, monkeypatch, text=UNIT_TEXT.replace("LAN_MODE=1", "LAN_MODE=0"))
    lists = _Lists(tmp_path, monkeypatch)
    res = svc.apply_lan_feeds(_digest(DOMAINS, NETS), _pack())
    assert res["ok"] is False and "выключен" in res["error"]
    assert lists.calls == [] and not lists.dir.exists()


def test_feeds_without_the_lists_script_ask_for_the_configuration(svc, lan, monkeypatch, tmp_path):
    monkeypatch.setattr(gwguard, "LAN_LISTS_SCRIPT", str(tmp_path / "нет-такого"))
    res = svc.apply_lan_feeds(_digest(DOMAINS, NETS), _pack())
    assert res["ok"] is False and "конфигурацию" in res["error"]
    assert lan.calls == []


def test_a_feed_the_script_rejected_is_not_remembered_as_applied(svc, lan):
    """Скрипт отверг фид (dnsmasq --test, короткий фид) и откатился. Запомни агент
    отпечаток — сервер больше не прислал бы исправленный фид с тем же хэшем, а
    свежесть заглушила бы и собственное скачивание."""
    lan.result = (False, "домены: фид подозрительно короткий")
    res = svc.apply_lan_feeds(_digest(DOMAINS, NETS), _pack())
    assert res == {"ok": False, "error": "домены: фид подозрительно короткий"}
    assert svc.lan_feeds_applied_hash() == ""
    assert svc.lan_feeds_from_channel_fresh() is False


def test_the_scheduled_download_stays_home_while_the_channel_brings_feeds(svc, lan):
    """Смысл этапа 3: пока фиды привозит канал, адрес квартиры за ними никуда
    не ходит. Канал замолчал на полсуток — задача сама возвращается к сети."""
    svc.apply_lan_feeds(_digest(DOMAINS, NETS), _pack())
    assert svc.lan_lists_update() == []
    assert lan.downloads == 0, "фиды привёз канал, а агент всё равно пошёл за ними в сеть"
    svc.db.set_state(svc._LAN_CHANNEL_AT_KEY, str(int(time.time()) - 13 * 3600))
    svc.lan_lists_update()
    assert lan.downloads == 1, "канал молчит полсуток, а агент так и сидит без свежих фидов"


def test_a_confirmation_for_other_feeds_does_not_extend_the_grace(svc, lan):
    """«Те же фиды» от сервера продлевает запас, только если отпечаток совпал с
    применённым: иначе застывшие старые фиды считались бы подтверждёнными."""
    d = _digest(DOMAINS, NETS)
    svc.apply_lan_feeds(d, _pack())
    old = str(int(time.time()) - 13 * 3600)
    svc.db.set_state(svc._LAN_CHANNEL_AT_KEY, old)
    svc.lan_feeds_touch("f" * 64)
    assert svc.db.get_state(svc._LAN_CHANNEL_AT_KEY) == old
    svc.lan_feeds_touch(d)
    assert svc.lan_feeds_from_channel_fresh() is True


def test_a_confirmation_without_channel_feeds_changes_nothing(svc, lan):
    """Фиды из канала ни разу не применялись — подтверждать нечего, и своё
    скачивание не должно замолчать от одной отметки."""
    svc.lan_feeds_touch()
    svc.lan_feeds_touch("f" * 64)
    assert svc.lan_feeds_from_channel_fresh() is False
    svc.lan_lists_update()
    assert lan.downloads == 1


def test_an_agent_without_the_channel_keeps_downloading_by_itself(svc, lan):
    """Канала нет (старый ВПС, выключен бандлом) — всё как до этапа 3."""
    svc.lan_lists_update()
    assert lan.downloads == 1


# ── роль слота ───────────────────────────────────────────────────────────────

def test_the_role_told_by_the_server_is_remembered(svc):
    assert svc.link_role() == "", "сервер ещё не сообщал — роли нет, а не «резерв»"
    svc.set_link_role(True)
    assert svc.link_role() == "active"
    svc.set_link_role(False)
    assert svc.link_role() == "standby"
