"""Правки по ревью домена основной роли: учёт трафика, фиды подсетей сервисов,
ключи устройств, подписки.

- опрос трафика сериализован: дамп и сравнение с сэмплом — под одним замком,
  второй опрос (кнопка «🔄», передача резерву, гость) снимает дамп только после
  записи первого;
- фиды подсетей сервисов для шлюзов: сбор, где часть источников не ответила, не
  публикуется поверх прежнего фида — прежний остаётся, повтор через полчаса; без
  прежнего неполный лучше пустого, но повтор тоже скорый.

Цена ошибки: дамп, снятый раньше, записывался после более позднего — дельта
выходила отрицательной, принималась за рестарт awg, и весь счётчик интерфейса
(десятки гигабайт с подъёма) уходил в трафик месяца — лимит срабатывал зря;
неполный фид уходил шлюзам как полный — подсети Telegram или Google выпадали из
VPN в локальной сети до следующего похода (до восьми часов).
"""
from __future__ import annotations

import threading
import time

import pytest

from awgbot.infra import awg
from tests.integration.test_gwlink_lists_role_diag import ITDOG, _Net

pytestmark = pytest.mark.integration


# ── опрос трафика: дамп и запись под одним замком ────────────────────────────

def test_poll_traffic_second_dump_waits_for_first_commit(services, make_active_client, monkeypatch):
    """Первый опрос снял дамп и задержался — второй не снимает свой, пока первый
    не записал сэмпл; итог месяца — ровно прирост, без «рестарта»."""
    c = make_active_client(name="a", tg_id=7001)
    d = services.add_device(c.id, "Тел")
    pub = services.db.get_device(d.device_id).public_key
    base = 10 * 2**30
    rx = {"v": base}
    first_dumped, release_first = threading.Event(), threading.Event()
    calls: list[int] = []

    def dump(name=None):
        v = rx["v"]
        calls.append(v)
        if len(calls) == 2:              # дамп опроса A — снят, но запись задержана
            first_dumped.set()
            assert release_first.wait(5)
        return [dict(public_key=pub, rx=v, tx=0, last_handshake=None, allowed_ips="10.8.1.2/32")]

    monkeypatch.setattr(awg, "show_dump", dump)
    monkeypatch.setattr(awg, "read_file", lambda p: "")
    services.poll_traffic()                                  # первая база
    rx["v"] = base + 1000
    a = threading.Thread(target=services.poll_traffic)
    a.start()
    assert first_dumped.wait(5)
    rx["v"] = base + 2000                                    # опрос B пришёл позже A
    b = threading.Thread(target=services.poll_traffic)
    b.start()
    b.join(0.3)
    assert len(calls) == 2, "второй опрос снял дамп до записи первого"
    release_first.set()
    a.join(5)
    b.join(5)
    assert calls == [base, base + 1000, base + 2000]
    assert services.db.get_device(d.device_id).traffic_rx_month == 2000


# ── фиды подсетей: неполный сбор не заменяет прежний ─────────────────────────

@pytest.fixture()
def lan_slot(services, make_active_client):
    """Слот 1 с режимом «за шлюзом — без VPN»: фиды кому-то нужны."""
    admin = make_active_client(name="Админ", tg_id=7100, device_limit=0)
    pi = services.add_device(admin.id, "Шлюз")
    services.db.gateway_add(pi.device_id, "awglink", 443, "127.0.0.0/30", slot_id=1)
    services.gateway_set_lan_subnets(1, "192.168.68.0/24")
    services.gateway_set_lan_mode(1, True)
    return services


@pytest.mark.parametrize("missing", [f"{ITDOG}/Subnets/IPv4/telegram.lst",
                                     "https://www.gstatic.com/ipranges/goog.json"])
def test_partial_nets_keep_the_previous_feeds(lan_slot, monkeypatch, missing):
    """Список одного сервиса не пришёл, остальные — с новым содержимым: прежний
    фид остаётся целиком, сверка не засчитана, следующий поход — через полчаса."""
    services = lan_slot
    net = _Net(monkeypatch)
    assert services.gwlink_lan_feeds_update(force=True) is True
    before = services.gwlink_lan_feeds()
    checked = services.gwlink_lan_feeds_checked()
    net.body[f"{ITDOG}/Subnets/IPv4/meta.lst"] = "31.13.24.0/21\n157.240.0.0/16\n"   # новое у другого сервиса
    del net.body[missing]
    now = int(time.time())
    assert services.gwlink_lan_feeds_update(force=True) is False
    assert services.gwlink_lan_feeds() == before, "неполный сбор заменил фид — подсети сервиса ушли со шлюзов"
    assert services.gwlink_lan_feeds_checked() == checked, "неполный сбор засчитан как сверка"
    nxt = int(services.db.get_state(services._LAN_FEEDS_AT_KEY))
    assert nxt - now <= int(1800 * 1.4) + 2, f"повтор через {nxt - now} с, а не скоро"


def test_partial_nets_without_previous_feeds_are_published_and_retried_soon(lan_slot, monkeypatch):
    """Фидов нет вовсе (новый ВПС): неполный лучше пустого — публикуется, но
    повтор скорый и сверка не засчитана; полный следующий сбор заменяет его."""
    services = lan_slot
    net = _Net(monkeypatch)
    del net.body[f"{ITDOG}/Subnets/IPv4/telegram.lst"]
    now = int(time.time())
    assert services.gwlink_lan_feeds_update(force=True) is True
    assert "31.13.24.0/21" in services.gwlink_lan_feeds()["nets"]
    assert services.gwlink_lan_feeds_checked() == ""
    assert int(services.db.get_state(services._LAN_FEEDS_AT_KEY)) - now <= int(1800 * 1.4) + 2
    net.body[f"{ITDOG}/Subnets/IPv4/telegram.lst"] = "91.108.4.0/22\n"
    services.db.set_state(services._LAN_FEEDS_AT_KEY, str(now - 1))         # срок повтора настал
    assert services.gwlink_lan_feeds_update() is True, "полный сбор после неполного не заменил фид"
    assert "91.108.4.0/22" in services.gwlink_lan_feeds()["nets"]
