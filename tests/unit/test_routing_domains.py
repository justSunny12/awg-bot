"""Чистая логика условной маршрутизации: нормализация ввода и генерация конфига.

Тестируем то, что пользователь реально вставит в поле ввода, — URL из адресной
строки, кириллицу, мусор, — а не идеальные домены.
"""
import pytest

from awgbot.domain import routing

pytestmark = pytest.mark.unit


# ── normalize ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("sberbank.ru", "sberbank.ru"),
    ("  SberBank.RU  ", "sberbank.ru"),
    ("https://www.sberbank.ru/ru/person?x=1", "sberbank.ru"),
    ("http://tinkoff.ru", "tinkoff.ru"),
    ("www.ozon.ru", "ozon.ru"),
    ("ozon.ru.", "ozon.ru"),
    ("gosuslugi.ru:443", "gosuslugi.ru"),
    ("user:pass@lk.example.com", "lk.example.com"),
    ("mail.mos.ru/path/deep", "mail.mos.ru"),
    ("sub.domain.co.uk", "sub.domain.co.uk"),
    ("«vtb.ru»".replace("«", "").replace("»", ""), "vtb.ru"),
])
def test_normalize_accepts_real_world_input(raw, expected):
    assert routing.normalize(raw) == expected


def test_normalize_idn_to_punycode():
    """dnsmasq сравнивает домены байтами — кириллицу надо переводить в punycode."""
    assert routing.normalize("сбербанк.рф").startswith("xn--")
    assert routing.normalize("почта.рф").endswith(".xn--p1ai")


@pytest.mark.parametrize("raw", [
    "", "   ", "ru", "localhost",                      # без зоны
    "1.2.3.4", "192.168.1.1",                          # IP, а не домен
    "site.123",                                        # числовая зона
    "-bad.ru", "bad-.ru", "a..b.ru",                   # битые метки
    "имя_с_подчёркиванием.ru",
])
def test_normalize_rejects_garbage(raw):
    with pytest.raises(routing.DomainRejected):
        routing.normalize(raw)


def test_normalize_rejects_too_long():
    with pytest.raises(routing.DomainRejected):
        routing.normalize("a" * 250 + ".ru")


# ── денай-лист ───────────────────────────────────────────────────────────────

def test_is_denied_matches_suffix():
    """Запрет на домен закрывает и его поддомены — иначе mail.X проедет мимо."""
    deny = ["vpn.example.org", "imap.mail.ru"]
    assert routing.is_denied("vpn.example.org", deny)
    assert routing.is_denied("a.b.vpn.example.org", deny)
    assert not routing.is_denied("example.org", deny)      # родитель не запрещён
    assert not routing.is_denied("notvpn.example.org", deny)


# ── parse_batch ──────────────────────────────────────────────────────────────

def test_parse_batch_splits_and_reports_reasons():
    accepted, rejected = routing.parse_batch(
        "https://bank.com\nnetflix.com, мусор_тут", denylist=[])
    assert accepted == ["bank.com", "netflix.com"]
    assert "мусор_тут" in dict(rejected)


def test_parse_batch_dedupes_within_one_paste():
    accepted, rejected = routing.parse_batch(
        "bank.com www.bank.com https://bank.com/x", denylist=[])
    assert accepted == ["bank.com"]
    assert rejected == []


def test_parse_batch_honors_denylist():
    accepted, rejected = routing.parse_batch(
        "good.com bad.com", denylist=["bad.com"])
    assert accepted == ["good.com"]
    assert dict(rejected)["bad.com"] == "этот домен добавлять нельзя"


def test_parse_batch_empty_input():
    assert routing.parse_batch("", denylist=[]) == ([], [])


# ── build_dnsmasq_conf ───────────────────────────────────────────────────────

def test_conf_merges_base_into_each_user_set():
    """Пер-юзерный merge: базовый домен получает НАБОРЫ ВСЕХ включённых профилей
    в одной директиве. Разносить их нельзя — dnsmasq применяет для домена только
    одну директиву ipset=, и домен из обоих списков попал бы лишь в один набор."""
    out = routing.build_dnsmasq_conf(
        base_domains=["blocked.com"], domains_by_client={2: ["example.com"]},
        client_ids=[2, 7], set_user_prefix="vpn_u")
    assert "ipset=/blocked.com/vpn_u2,vpn_u7" in out
    assert "ipset=/example.com/vpn_u2" in out


def test_conf_domain_in_both_lists_gets_single_directive():
    """Домен и в базе, и в личном списке — ОДНА директива без дублей набора.
    Раньше такой домен ломал маршрутизацию остальным: он уходил в личный набор
    и переставал попадать в общий."""
    out = routing.build_dnsmasq_conf(
        base_domains=["example.com"], domains_by_client={2: ["example.com"]},
        client_ids=[2, 7], set_user_prefix="vpn_u")
    lines = [l for l in out.splitlines() if "example.com" in l]
    assert lines == ["ipset=/example.com/vpn_u2,vpn_u7"], lines


def test_conf_without_clients_is_empty():
    """Никто не включён — директив нет: наборов, в которые писать, не существует."""
    out = routing.build_dnsmasq_conf(
        base_domains=["blocked.com"], domains_by_client={}, client_ids=[],
        set_user_prefix="vpn_u")
    assert "ipset=" not in out


def test_conf_is_deterministic():
    """Один и тот же вход → байт-в-байт тот же файл: иначе каждая реконсиляция
    выглядела бы как изменение и дёргала рестарт dnsmasq."""
    kw = dict(base_domains=["b.com", "a.com"], set_user_prefix="vpn_u",
              domains_by_client={7: ["z.com"], 3: ["c.com"]}, client_ids=[3, 7])
    assert routing.build_dnsmasq_conf(**kw) == routing.build_dnsmasq_conf(**kw)


# ── имена наборов — константы, не настройка ──────────────────────────────────

def test_set_names_are_constants_not_yaml():
    """Имена наборов и цепочек НЕ должны читаться из conf.

    Боевой app.yaml при обновлении не мигрирует — seed_conf копирует только
    отсутствующие файлы целиком. Поэтому однажды прописанное имя переживает смену
    схемы, и бот начинает искать набор, которого больше никто не создаёт:
    инфраструктура исправна, а функция молча не работает. Ровно это и случилось
    при переходе ru_base → vpn_base.
    """
    import pathlib
    from awgbot.core import config

    src = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    for key in ("set_base", "set_user_prefix", "set_src_prefix",
                '"chain"', '"nat_chain"'):
        assert f'_rt.get({key}' not in src, f"{key} снова читается из yaml"

    assert config.ROUTING_SET_USER_PREFIX == "vpn_u"
    assert config.ROUTING_CHAIN == "AWGBOT_RT"
    assert config.ROUTING_NAT_CHAIN == "AWGBOT_RTNAT"


# ── живость шлюза: измеритель не должен мигать ───────────────────────────────

def _svc_with_age(monkeypatch, age, stale=None):
    """Сервис с подставленным возрастом хендшейка."""
    from awgbot.domain import services as S
    monkeypatch.setattr(S.routing, "available", lambda: True)
    monkeypatch.setattr(S.routing, "link_handshake_age", lambda: age)
    if stale is not None:
        from awgbot.core import settings
        monkeypatch.setattr(settings, "get",
                            lambda k, d=None: stale if "link_stale" in k else d)
    return S


def test_link_stale_floor_survives_wireguard_rekey(monkeypatch):
    """Хендшейк обновляется при рекее (~120 с), а сессия действительна до 180 с,
    поэтому возраст 170 с — норма, а не отвал. Старый порог 180 стоял ровно в
    этой зоне и гасил маркировку у живого шлюза."""
    from awgbot.domain.services import Services
    S = _svc_with_age(monkeypatch, 170, stale=180)     # старое значение из conf
    svc = Services.__new__(Services)
    assert svc.routing_link_ok() is True, "живой шлюз объявлен мёртвым"


def test_link_really_dead_is_still_detected(monkeypatch):
    """Пол поднимает порог, но не отменяет детект: молчание сильно за окном
    рекея по-прежнему считается отвалом."""
    from awgbot.domain.services import Services
    _svc_with_age(monkeypatch, 900, stale=180)
    svc = Services.__new__(Services)
    assert svc.routing_link_ok() is False
