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


# ── покрытие базой и денай-лист ──────────────────────────────────────────────

def test_covered_by_base_is_inert_after_inversion():
    """Базовый набор наполняется извне и по имени домена не проверяется —
    принадлежность видна только по адресу, уже после резолва."""
    assert not routing.covered_by_base("sberbank.ru", ["ru"])
    assert not routing.covered_by_base("example.com", [])


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
        "https://bank.com\nnetflix.com, мусор_тут", base_zones=(), denylist=[])
    assert accepted == ["bank.com", "netflix.com"]
    assert "мусор_тут" in dict(rejected)


def test_parse_batch_dedupes_within_one_paste():
    accepted, rejected = routing.parse_batch(
        "bank.com www.bank.com https://bank.com/x", base_zones=[], denylist=[])
    assert accepted == ["bank.com"]
    assert rejected == []


def test_parse_batch_honors_denylist():
    accepted, rejected = routing.parse_batch(
        "good.com bad.com", base_zones=[], denylist=["bad.com"])
    assert accepted == ["good.com"]
    assert dict(rejected)["bad.com"] == "этот домен добавлять нельзя"


def test_parse_batch_empty_input():
    assert routing.parse_batch("", base_zones=[], denylist=[]) == ([], [])


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
