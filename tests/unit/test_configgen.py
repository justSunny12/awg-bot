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


def test_generate_embedded_conf_has_no_mtu():
    # встроенный в vpn:// конфиг воспроизводит эталон приложения — без строки MTU
    res = cg.generate("PRIVKEY", "PUBKEY", "10.8.1.4", _server_params())
    embedded = cg.decode_vpn(res["vpn"])["containers"][0]["awg"]["last_config"]
    assert "MTU = " not in embedded


def test_traffic_limit_device_ask_enrichment():
    """п.2: с лимитом профиля показываем N ГБ; безлимит — скобку опускаем."""
    from awgbot.bot import texts
    unlimited = texts.traffic_limit_device_ask(0)
    assert "в пределах лимита профиля" not in unlimited
    assert unlimited.endswith("без ограничения.")
    limited = texts.traffic_limit_device_ask(50 * 1024**3)
    assert "в пределах лимита профиля: 50.00 ГБ" in limited


def test_device_created_report_variants():
    """Отчёт создания устройства: имя профиля только для админа, лимиты по спеку."""
    from awgbot.bot import texts
    GB = 1024**3
    # админ, свой лимит устройства
    r = texts.device_created_report("Ноут", client_name="Вася", device_count=2,
                                    max_devices=5, dev_limit_bytes=50*GB, profile_limit_bytes=100*GB)
    assert "для профиля «Вася»" in r
    assert "Количество устройств: 2/5" in r
    assert "Лимит потребления: 50.00 ГБ" in r
    # без лимита устройства, профиль с лимитом
    r2 = texts.device_created_report("Тел", client_name="Вася", device_count=3,
                                     max_devices=5, dev_limit_bytes=0, profile_limit_bytes=100*GB)
    assert "в рамках лимита профиля не ограничено (100.00 ГБ/профиль)" in r2
    # без лимита устройства, профиль безлимит: ограничивать нечему — говорим
    # это одной фразой, без оговорки про лимит профиля, которого нет
    r3 = texts.device_created_report("П", client_name="Вася", device_count=1,
                                     max_devices=0, dev_limit_bytes=0, profile_limit_bytes=0)
    assert r3.endswith("Потребление не ограничено.")
    assert "лимита профиля" not in r3
    assert "1/∞" in r3
    # клиент/друг — без имени профиля
    r4 = texts.device_created_report("П", client_name=None, device_count=1, max_devices=3)
    assert "для профиля" not in r4


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
