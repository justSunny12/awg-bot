"""Unit: awgbot.domain.configgen — vpn:// кодек и генерация конфигов (чистая логика)."""
import json

import pytest

from awgbot.core import config
from awgbot.core import settings
from awgbot.domain import configgen as cg

pytestmark = pytest.mark.unit


def _server_params():
    obf = {k: str(i) for i, k in enumerate(cg._OBF_ORDER)}
    return {"obfuscation": obf, "listen_port": 43125,
            "server_pubkey": "SRVPUB==", "psk": "PSK=="}


# ── vpn:// кодек ─────────────────────────────────────────────────────────────
def test_encode_decode_roundtrip():
    obj = {"a": 1, "b": [1, 2, "x"], "nested": {"k": "значение"}}
    link = cg.encode_vpn(obj)
    assert link.startswith("vpn://")
    assert cg.decode_vpn(link) == obj


def test_decode_rejects_non_vpn():
    with pytest.raises(ValueError):
        cg.decode_vpn("https://example.com")


def test_decode_rejects_garbage_body():
    with pytest.raises(ValueError):
        cg.decode_vpn("vpn://@@@not-base64@@@")


# ── generate: .conf + vpn:// ─────────────────────────────────────────────────
def test_generate_conf_contents():
    res = cg.generate("PRIVKEY", "PUBKEY", "10.8.1.4", _server_params())
    assert set(res) == {"conf", "vpn"}
    conf = res["conf"]
    assert "[Interface]" in conf and "[Peer]" in conf
    assert "PrivateKey = PRIVKEY" in conf
    assert f"MTU = {config.MTU}" in conf                       # standalone .conf несёт MTU
    assert f"Endpoint = {config.SERVER_HOST}:43125" in conf
    assert "PublicKey = SRVPUB==" in conf
    assert "PresharedKey = PSK==" in conf


def test_generate_vpn_is_parseable_and_roundtrips_keys():
    res = cg.generate("PRIVKEY", "PUBKEY", "10.8.1.4", _server_params())
    assert res["vpn"].startswith("vpn://")
    decoded = cg.decode_vpn(res["vpn"])
    assert decoded["containers"][0]["container"] == config.CONTAINER
    # приложение читает ключи именно из last_config — проверяем то, что оно увидит
    lc = json.loads(decoded["containers"][0]["awg"]["last_config"])
    assert lc["client_priv_key"] == "PRIVKEY"
    assert lc["client_pub_key"] == "PUBKEY"
    assert lc["client_ip"] == "10.8.1.4"


def test_vpn_names_the_subnet_of_the_device_not_the_global_one(monkeypatch):
    """Переехавшему устройству ссылка называет ЕГО подсеть.

    Поле вморожено в выданную ссылку, а интерфейсов в окне переезда два, и у
    второго своя сеть. Глобальная настройка назвала бы двойнику из 10.9.1.0/24
    чужую 10.8.1.0 — и назвала бы навсегда.
    """
    monkeypatch.setattr(config, "SUBNET_PREFIX", "10.8.1")
    twin = cg.decode_vpn(cg.generate("P", "U", "10.9.1.2", _server_params())["vpn"])
    assert twin["containers"][0]["awg"]["subnet_address"] == "10.9.1.0"

    # для старой подсети выдача остаётся ровно прежней — правка инертна
    old = cg.decode_vpn(cg.generate("P", "U", "10.8.1.4", _server_params())["vpn"])
    assert old["containers"][0]["awg"]["subnet_address"] == "10.8.1.0"


def test_generate_embedded_conf_has_no_mtu():
    # встроенный в vpn:// конфиг воспроизводит эталон приложения — без строки MTU
    res = cg.generate("PRIVKEY", "PUBKEY", "10.8.1.4", _server_params())
    embedded = cg.decode_vpn(res["vpn"])["containers"][0]["awg"]["last_config"]
    assert "MTU = " not in embedded


def test_pause_available_wording():
    """Приостановка показывает доступные (оставшиеся) дни, не использованные."""
    # 21 использовано из 28 → доступно 7
    assert settings.get_int("pause.pause_max_total_days", 28) - 21 == 7


# ── пустые обфускейт-поля ────────────────────────────────────────────────────

def _params_with_empty_i():
    """Ровно то, что отдаёт боевой сервер: I1 заполнен, I2..I5 пусты."""
    obf = {k: str(i) for i, k in enumerate(cg._OBF_ORDER)}
    obf["I1"] = "<r 2><b 0xdeadbeef>"
    for k in ("I2", "I3", "I4", "I5"):
        obf[k] = ""
    return {"obfuscation": obf, "listen_port": 43125,
            "server_pubkey": "SRVPUB==", "psk": "PSK=="}


def test_standalone_conf_omits_empty_obfuscation_fields():
    """`awg setconf` падает на `I2 = ` с «Line unrecognized».

    Эталон приложения содержит эти поля пустыми, и телефон их глотает. Но
    выданный ботом .conf импортируют и на линуксе — шлюз, роутер, второй
    сервер, — и там он не поднимал интерфейс вовсе. Отсутствие ключа и пустое
    значение для awg равнозначны, поэтому пропуск ничего не меняет по смыслу.
    """
    import re
    conf = cg.generate("PRIV==", "PUB==", "10.8.1.7", _params_with_empty_i())["conf"]
    # Ключ с ПУСТЫМ значением, а не «строка кончается на =»: приватные ключи
    # base64 тоже кончаются на «=», и наивная проверка ловила бы их.
    empty = [l for l in conf.splitlines() if re.fullmatch(r"\S+\s*=\s*", l)]
    assert empty == [], f"пустые поля вернулись: {empty}"
    assert "I1 = <r 2><b 0xdeadbeef>" in conf, "непустое поле обязано остаться"
    for k in ("I2", "I3", "I4", "I5"):
        assert f"{k} =" not in conf


def test_embedded_vpn_config_keeps_them():
    """Во встроенном конфиге поля остаются: формат vpn:// заморожен и обязан
    воспроизводить эталон приложения байт в байт — там их ждут."""
    link = cg.generate("PRIV==", "PUB==", "10.8.1.7", _params_with_empty_i())["vpn"]
    inner = cg.decode_vpn(link)
    text = json.dumps(inner, ensure_ascii=False)
    for k in ("I2", "I3", "I4", "I5"):
        assert f"{k} = " in text, f"{k} пропал из встроенного конфига"


# ── ключи поколения 3.1 (предусловие переезда) ───────────────────────────────

def _params(**extra):
    obf = {k: str(i + 1) for i, k in enumerate(
        ["Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "I1"])}
    obf.update({k: "" for k in ("I2", "I3", "I4", "I5")})
    obf.update(extra)
    return {"obfuscation": obf, "listen_port": 42755,
            "server_pubkey": "SRVPUB==", "psk": "PSK=="}


def test_new_keys_are_inert_until_the_server_sets_them():
    """Пока сервер не задаёт ключи 3.1, выдача побайтово прежняя.

    Это и делает правку безопасной для выкатки до переезда: формат vpn://
    заморожен и обязан воспроизводить эталон приложения, а лишняя пара
    `"RandomTrailers": ""` изменила бы ссылку у ВСЕХ, ничего не дав взамен.
    """
    res = cg.generate("PRIV", "PUB", "10.8.1.5", _params())
    awg_block = cg.decode_vpn(res["vpn"])["containers"][0]["awg"]
    for key in ("RandomTrailers", "HeaderProtectionKey"):
        assert key not in res["conf"]
        assert key not in awg_block
        assert f'"{key}"' not in awg_block["last_config"]
    # пустые I2–I5 эталон печатает — их поведение трогать было нельзя
    assert '"I2": ""' in awg_block["last_config"]


def test_symmetric_keys_reach_both_forms_of_the_config():
    """RandomTrailers и HeaderProtectionKey СИММЕТРИЧНЫ: приёмник берёт пакет
    длиннее ожидаемого, только если фича включена у него самого.

    Значит обе формы обязаны нести их одинаково. Половина людей импортирует
    ссылкой, половина файлом, и разный набор ключей означал бы, что половина
    просто не подключится.
    """
    res = cg.generate("PRIV", "PUB", "10.8.1.5",
                      _params(RandomTrailers="on", HeaderProtectionKey="HPK=="))
    awg_block = cg.decode_vpn(res["vpn"])["containers"][0]["awg"]
    assert "RandomTrailers = on" in res["conf"]
    assert "HeaderProtectionKey = HPK==" in res["conf"]
    assert "RandomTrailers = on" in awg_block["last_config"]
    assert awg_block["RandomTrailers"] == "on"
    assert awg_block["HeaderProtectionKey"] == "HPK=="


def test_key_set_matches_the_application_sources():
    """Набор и порядок ключей взяты из исходников приложения 5.0.1.5
    (configKeys.h, awgProtocolKeys), а не из одного образца конфига: образец
    показывает один случай, список в коде — все.

    Проверяем ПОРЯДОК, потому что формат vpn:// обязан воспроизводить эталон, а
    порядок в нём значим. И полноту: не прочитать ключ, который сервер задал,
    значит выдать конфиг, который молча не подключается.
    """
    assert cg._OBF_ORDER == [
        "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
        "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5",
        "HeaderProtectionKey", "ContentPaddingAddition",
        "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime",
        "KeepaliveTimeout", "MaxHandshakeAttempts",
        "RandomTrailers", "DisableCookies",
    ]


def test_optional_keys_appear_only_when_the_server_sets_them():
    """Приложение печатает эти ключи только непустыми — `if (!x.isEmpty())` на
    каждом в AwgClientConfig::toJson. Делаем так же, иначе конфиг разошёлся бы
    с эталоном на пустых значениях."""
    res = cg.generate("PRIV", "PUB", "10.8.1.5", _params(RejectAfterTime="180"))
    awg_block = cg.decode_vpn(res["vpn"])["containers"][0]["awg"]
    assert "RejectAfterTime = 180" in res["conf"]
    assert awg_block["RejectAfterTime"] == "180"
    # соседи по списку остались невидимыми
    for absent in ("RekeyAfterTime", "KeepaliveTimeout", "DisableCookies"):
        assert absent not in res["conf"]
        assert absent not in awg_block


def test_reader_and_writer_know_the_same_keys():
    """Множества ключей у читателя (awg) и писателя (configgen) обязаны
    совпадать: прочитанный, но не записанный ключ теряется молча, а записанный
    без чтения всегда пуст. Общего источника нет намеренно — там важен ПОРЯДОК,
    здесь множество, — поэтому расхождение ловим тестом."""
    from awgbot.infra import awg
    assert set(awg._OBFUSCATION_KEYS) == set(cg._OBF_ORDER)


def test_keepalive_passes_through_as_written():
    """PersistentKeepalive уходит в конфиг тем, чем задан, — числом или
    диапазоном.

    Диапазон здесь не украшение: фиксированные 25 секунд это метроном
    ванильного WireGuard, видимый на простаивающем туннеле без расшифровки.
    Прочие таймеры джиттерит сервер, а этот приходит из конфига бота — и,
    оставшись числом, обнулял бы всю ломку ритма самым заметным сигналом.
    """
    from awgbot.core import config, settings
    import json as _json

    for value in ("25-35", 25):
        with pytest.MonkeyPatch.context() as mp:
            # Значение живое: правка из чата применяется к СЛЕДУЮЩЕЙ выданной
            # ссылке, а не после рестарта. Константа config — лишь дефолт.
            mp.setattr(settings, "get", lambda k, d=None, _v=value:
                       _v if k == "app.client_config.keepalive_seconds" else d)
            res = cg.generate("PRIV", "PUB", "10.8.1.5", _params())
            assert f"PersistentKeepalive = {value}" in res["conf"]
            lc = _json.loads(
                cg.decode_vpn(res["vpn"])["containers"][0]["awg"]["last_config"])
            assert lc["persistent_keep_alive"] == str(value)
    with pytest.MonkeyPatch.context() as mp:           # ключа нет — берём константу
        mp.setattr(settings, "get", lambda k, d=None: d)
        mp.setattr(config, "KEEPALIVE_SECONDS", "40-50")
        assert "PersistentKeepalive = 40-50" in cg.generate(
            "PRIV", "PUB", "10.8.1.5", _params())["conf"]


# ── живые деплой-значения ────────────────────────────────────────────────────

@pytest.mark.parametrize("key, value, where", [
    ("app.network.server_host", "vpn.example.org", "hostName"),
    ("app.client_config.server_name", "Мой сервер", "description"),
    ("app.client_config.dns1", "10.8.1.1", "dns1"),
    ("app.client_config.dns2", "10.8.1.1", "dns2"),
])
def test_server_settings_are_read_at_issue_time_not_at_start(key, value, where, monkeypatch):
    """Раздел «🖥 Сервер» правится из чата и обязан применяться к СЛЕДУЮЩЕЙ
    выданной ссылке. Читай мы константы config, экран показывал бы одно, а
    ссылка, выданная через минуту, несла другое — и заметил бы это только
    человек, у которого не подключается."""
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get", lambda k, d=None, _k=key, _v=value: _v if k == _k else d)
    doc = cg.decode_vpn(cg.generate("PRIV", "PUB", "10.8.1.5", _params())["vpn"])
    assert doc[where] == value


def test_mtu_is_read_at_issue_time(monkeypatch):
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get",
                        lambda k, d=None: 1280 if k == "app.client_config.mtu" else d)
    res = cg.generate("PRIV", "PUB", "10.8.1.5", _params())
    assert "MTU = 1280" in res["conf"]


# ── резолвер админа («SMB в туннеле») ───────────────────────────────────────

ADMIN_DNS = "10.255.53.53"
SPLIT = "10.8.1.0/24, 192.168.1.0/24"


def _lines(conf: str, key: str) -> list[str]:
    return [ln.split("=", 1)[1].strip() for ln in conf.splitlines() if ln.startswith(f"{key} =")]


def _both_forms(res) -> tuple[str, str, dict]:
    """(.conf, embedded conf из vpn://, JSON ссылки) — человек импортирует любое."""
    doc = cg.decode_vpn(res["vpn"])
    lc = json.loads(doc["containers"][0]["awg"]["last_config"])
    return res["conf"], lc["config"], doc


def test_admin_dns_goes_to_both_dns_fields_of_every_form(monkeypatch):
    """Оба поля — резолвер админа: публичный вторым номером возвращал бы часть
    запросов мимо него, и Finder видел бы имена через раз."""
    res = cg.generate("PRIV", "PUB", "10.8.1.2", _params(), admin_dns=ADMIN_DNS)
    conf, embedded, doc = _both_forms(res)
    assert _lines(conf, "DNS") == [f"{ADMIN_DNS}, {ADMIN_DNS}"]
    assert _lines(embedded, "DNS") == [f"{ADMIN_DNS}, {ADMIN_DNS}"], "ссылка vpn:// несёт прежний DNS"
    assert doc["dns1"] == ADMIN_DNS and doc["dns2"] == ADMIN_DNS


def test_split_tunnel_admin_config_carries_the_admin_resolver_32(monkeypatch):
    """Раздельный туннель без пространства: адрес резолвера админа вне подсети
    клиентов, без /32 в AllowedIPs запрос DNS ушёл бы мимо туннеля."""
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", SPLIT)
    conf, embedded, _ = _both_forms(cg.generate("PRIV", "PUB", "10.8.1.2", _params(), admin_dns=ADMIN_DNS))
    assert _lines(conf, "AllowedIPs") == [f"{SPLIT}, {ADMIN_DNS}/32"]
    assert _lines(embedded, "AllowedIPs") == [f"{SPLIT}, {ADMIN_DNS}/32"], "в ссылке vpn:// /32 нет"


SPACE = "10.240.0.0/12"


def test_split_tunnel_admin_config_carries_the_space_and_no_resolver_32_inside_it(monkeypatch):
    """Адреса в туннеле локальных сетей шлюзов — одной строкой пространства;
    резолвер админа в служебной /16 пространства ею покрыт, /32 не нужен."""
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", SPLIT)
    conf, embedded, _ = _both_forms(cg.generate("PRIV", "PUB", "10.8.1.2", _params(), admin_dns=ADMIN_DNS,
                                                extra_nets=[SPACE]))
    assert _lines(conf, "AllowedIPs") == [f"{SPLIT}, {SPACE}"]
    assert _lines(embedded, "AllowedIPs") == [f"{SPLIT}, {SPACE}"], "в ссылке vpn:// пространства нет"


def test_a_resolver_outside_the_space_keeps_its_32(monkeypatch):
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", SPLIT)
    assert cg.allowed_ips_for("10.53.0.53", [SPACE]) == f"{SPLIT}, {SPACE}, 10.53.0.53/32"
    assert cg.allowed_ips_for("", [SPACE]) == f"{SPLIT}, {SPACE}", "без резолвера админа — только пространство"


@pytest.mark.parametrize("allowed", ["0.0.0.0/0, ::/0", "0.0.0.0/0", "10.0.0.0/8, ::/0"])
def test_a_covering_line_stays_byte_for_byte_with_the_space_too(monkeypatch, allowed):
    """Полный туннель (и любая база, уже покрывающая пространство) — строка
    прежняя: конфиг байт в байт тот же, что до адресов в туннеле."""
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", allowed)
    before = cg.generate("PRIV", "PUB", "10.8.1.2", _params(), admin_dns=ADMIN_DNS)
    after = cg.generate("PRIV", "PUB", "10.8.1.2", _params(), admin_dns=ADMIN_DNS, extra_nets=[SPACE])
    assert before == after


@pytest.mark.parametrize("allowed, split", [
    ("0.0.0.0/0, ::/0", False),
    ("0.0.0.0/0", False),
    ("::/0, 0.0.0.0/0", False),
    ("10.8.1.0/24", True),
    ("10.8.1.0/24, 192.168.1.0/24", True),
    ("0.0.0.0/1, 128.0.0.0/1", True),   # покрывает всё, но не одной строкой — честно «раздельный»
    ("::/0", True),
])
def test_split_is_told_by_the_allowed_ips_line(monkeypatch, allowed, split):
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", allowed)
    assert cg.split_tunnel() is split


@pytest.mark.parametrize("allowed", ["0.0.0.0/0, ::/0", "0.0.0.0/0", "10.0.0.0/8, ::/0"])
def test_a_covering_allowed_ips_line_stays_byte_for_byte(monkeypatch, allowed):
    """Полный туннель (и любой список, уже покрывающий адрес) — строка прежняя:
    лишний /32 менял бы конфиг админа без смысла."""
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", allowed)
    conf, embedded, _ = _both_forms(cg.generate("PRIV", "PUB", "10.8.1.2", _params(), admin_dns=ADMIN_DNS))
    assert _lines(conf, "AllowedIPs") == [allowed] and _lines(embedded, "AllowedIPs") == [allowed]


def test_without_admin_dns_the_config_is_exactly_the_old_one(monkeypatch):
    """Клиенту, шлюзовому устройству и админу без резолвера админа (docker) —
    конфиг байт в байт тот же, что до «SMB в туннеле»."""
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", SPLIT)
    plain = cg.generate("PRIV", "PUB", "10.8.1.2", _params())
    empty = cg.generate("PRIV", "PUB", "10.8.1.2", _params(), admin_dns="")
    assert plain == empty
    conf, embedded, doc = _both_forms(plain)
    assert _lines(conf, "AllowedIPs") == [SPLIT] and ADMIN_DNS not in conf and ADMIN_DNS not in embedded
    assert doc["dns1"] == cg.dns_for()[0] and doc["dns2"] == cg.dns_for()[1]


def test_a_garbage_admin_dns_does_not_reach_allowed_ips(monkeypatch):
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", SPLIT)
    assert cg.allowed_ips_for("10.255.53") == SPLIT
    assert cg.allowed_ips_for("") == SPLIT


def test_a_sloppy_split_list_is_tidied_only_for_the_admin(monkeypatch):
    """«a, » в app.yaml: админу — ровная строка с /32 (пустой элемент сломал бы
    импорт), остальным — байт в байт как всегда."""
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", "10.8.1.0/24 ,192.168.1.0/24, ")
    assert cg.allowed_ips_for(ADMIN_DNS) == f"10.8.1.0/24, 192.168.1.0/24, {ADMIN_DNS}/32"
    assert cg.allowed_ips_for("") == "10.8.1.0/24 ,192.168.1.0/24, "


@pytest.mark.parametrize("allowed", ["0.0.0.0/0,::/0", " 0.0.0.0/0 , ::/0", "10.0.0.0/8,  192.168.1.0/24"])
def test_nothing_added_and_no_resolver_leaves_the_raw_line(monkeypatch, allowed):
    """Пространство покрыто, резолвера админа нет — дописывать нечего, и строка
    из настройки идёт как есть, байт в байт (даже неаккуратная)."""
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", allowed)
    assert cg.allowed_ips_for("", [SPACE]) == allowed
    assert cg.extra_needed([SPACE]) == []
    monkeypatch.setattr(config, "CLIENT_ALLOWED_IPS", "10.8.1.0/24")
    assert cg.extra_needed([SPACE]) == [SPACE]
