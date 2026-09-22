"""Бандл для шлюза: сборка одним файлом из routing-link-setup.sh.

Бандл едет на ЧУЖУЮ машину, несёт приватный ключ линка и правит там systemd.
Ошибка в нём обнаруживается на домашнем шлюзе, куда ещё надо дойти. Поэтому
собираем его здесь настоящим скриптом и проверяем результат, а не исходник.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LINK = ROOT / "install" / "routing-link-setup.sh"
GW = ROOT / "install" / "routing-gw-setup.sh"

_CONF = """[Interface]
Address = 10.99.99.2/30
PrivateKey = SECRETPRIVKEY==
H1 = 12345-67890

[Peer]
PublicKey = SRVPUB==
PresharedKey = PSKPSK==
Endpoint = 203.0.113.10:443
AllowedIPs = 10.8.1.0/24, 10.99.99.0/30
PersistentKeepalive = 25
"""


def _emit_bundle(d: Path, *, conf_text: str = _CONF, env: dict | None = None) -> str:
    """Собирает бандл НАСТОЯЩИМ скриптом в каталоге d и отдаёт его текст.

    conf_text — конфиг шлюза, который уже лежит на ВПС (таким он был в момент
    выпуска ключей); env — надстройка над базовым окружением сборки.
    """
    inst = d / "install"; inst.mkdir()
    # копии рядом: emit_gw_bundle ищет gw-скрипт по соседству с собой
    src = LINK.read_text(encoding="utf-8").replace(
        '[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }', ":", 1)
    (inst / "routing-link-setup.sh").write_text(src, encoding="utf-8")
    (inst / "routing-gw-setup.sh").write_text(GW.read_text(encoding="utf-8"),
                                              encoding="utf-8")
    conf = d / "gw.conf"; conf.write_text(conf_text, encoding="utf-8")
    # живой конфиг линка ВПС: порт УЖЕ сменён (Endpoint в gw.conf отстал)
    confdir = d / "linkconf"; confdir.mkdir()
    (confdir / "awglink.conf").write_text(
        "[Interface]\nListenPort = 47231\nPrivateKey = X==\n", encoding="utf-8")
    out = d / "awg-gw-bundle.sh"

    penv = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CONF_DIR": str(confdir),
            # по умолчанию конфига бота у сборки нет — работают дефолты скрипта;
            # путь задаём явно, чтобы прогон не зависел от /etc машины
            "_APP_YAML": str(d / "no-such-app.yaml"),
            "GW_CONF_OUT": str(conf), "GW_BUNDLE_OUT": str(out),
            "ADMIN_IPS": "10.8.1.2 10.8.1.3; rm -rf /"}      # мусор обязан отсеяться
    penv.update(env or {})
    r = subprocess.run(
        ["sh", str(inst / "routing-link-setup.sh"), "--bundle"],
        cwd=d, capture_output=True, text=True, errors="replace", env=penv)
    assert r.returncode == 0, r.stderr
    assert out.exists(), r.stdout
    return out.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> str:
    """Бандл со сборки по умолчанию: app.yaml бота недоступен (его нет по
    дефолтному пути), значит работают дефолты скрипта."""
    return _emit_bundle(tmp_path_factory.mktemp("gwb"))


def test_bundle_is_valid_shell(bundle, tmp_path):
    f = tmp_path / "b.sh"; f.write_text(bundle, encoding="utf-8")
    r = subprocess.run(["sh", "-n", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_bundle_pins_the_client_subnet(bundle):
    """Бандл ВШИВАЕТ клиентскую подсеть и экспортирует её до запуска gw-скрипта.

    На шлюзе нет app.yaml — взять подсеть там неоткуда, а env-дефолт скрипта
    после смены подсети (переезд профилей) реассертил бы по ребуту правила для
    чужой. Подсеть — контракт между сторонами: сменилась — новый бандл.
    """
    assert re.search(r'^CLIENT_SUBNET="\$\{CLIENT_SUBNET:-\d+\.\d+\.\d+\.\d+/\d+\}"$',
                     bundle, re.M), "подсеть не вшита"
    assert "\nexport CLIENT_SUBNET\n" in bundle
    # экспорт обязан стоять ДО передачи управления gw-скрипту
    assert bundle.index("export CLIENT_SUBNET") < bundle.index('"$DEST/routing-gw-setup.sh" "${1:---apply}"')


def test_bundle_carries_the_link_config_verbatim(bundle):
    """Конфиг должен доехать байт в байт — кроме двух строк, которые сборка
    бандла СИНХРОНИЗИРУЕТ с конфигом ВПС: порта в Endpoint (иначе шлюз уезжает
    в закрытый порт, см. test_bundle_syncs_endpoint...) и PersistentKeepalive
    (см. test_bundle_refreshes_the_keepalive...)."""
    for line in _CONF.strip().splitlines():
        if line.startswith(("Endpoint", "PersistentKeepalive")):
            continue
        assert line in bundle, line


def test_bundle_syncs_endpoint_port_with_the_live_link(bundle):
    """Endpoint в конфиге шлюза замерзает при генерации, а порт линка — факт из
    живого конфига ВПС. Бандл обязан увезти ЖИВОЙ порт: рассинхрон означает
    линк, стучащийся в закрытую дверь, — и обнаруживается это уже на шлюзе,
    куда ещё надо дойти. Наступили при переносе 443 → 47231."""
    assert "Endpoint = 203.0.113.10:47231" in bundle, "порт не синхронизирован"
    assert ":443" not in bundle, "старый порт уехал в бандл"


def _keepalives(text: str) -> list:
    return re.findall(r"^PersistentKeepalive = (.+)$", text, re.M)


def test_bundle_refreshes_the_keepalive_of_a_gateway_issued_before_the_range(bundle):
    """PersistentKeepalive замерзает в конфиге шлюза при выпуске ключей.

    Шлюзы, выданные до того, как значение стало диапазоном, остались с
    прибитыми 25 секундами — а это метроном ванильного WireGuard, видимый на
    простаивающем линке без всякой расшифровки. Перевыпускать ради этого ключи
    нельзя (это новый линк и поход к малине), значит строку обязана подтягивать
    каждая сборка бандла — как порт Endpoint и AllowedIPs.
    """
    assert "PersistentKeepalive = 25" in _CONF, "фикстура должна изображать дореформенный шлюз"
    assert _keepalives(bundle) == ["25-35"], "в бандл уехало старое значение"


def test_link_keepalive_comes_from_the_same_key_as_client_peers(tmp_path):
    """Ритм линка и ритм клиентов задаёт один ключ конфига бота: поменял
    client_config.keepalive_seconds — поменялось и у линка, иначе линк остаётся
    единственным узнаваемым метрономом на сервере."""
    (tmp_path / "app.yaml").write_text(
        'client_config:\n  mtu: 1376\n  keepalive_seconds: "20-40"\n', encoding="utf-8")
    out = _emit_bundle(tmp_path, env={"_APP_YAML": str(tmp_path / "app.yaml")})
    assert _keepalives(out) == ["20-40"]


@pytest.mark.parametrize("raw, expect", [
    ("25", "25"),              # одиночное число законно: клиенты дореформенного поколения
    ("20-40", "20-40"),
    ("", "25-35"),             # ключа нет или он пуст
    ("много", "25-35"),        # мусор
    ("-5", "25-35"),           # диапазон без начала
    ("5-", "25-35"),           # диапазон без конца
    ("1-2-3", "25-35"),        # не диапазон вовсе
])
def test_broken_keepalive_in_the_config_falls_back_to_the_default_range(tmp_path, raw, expect):
    """Мусор в ключе не должен доехать до конфига шлюза: awg-quick на малине
    отвергнет неразбираемое значение целиком, линк не поднимется, и увидят это
    уже на той стороне. Непонятное значение — диапазон по умолчанию."""
    (tmp_path / "app.yaml").write_text(
        f'client_config:\n  keepalive_seconds: "{raw}"\n', encoding="utf-8")
    out = _emit_bundle(tmp_path, env={"_APP_YAML": str(tmp_path / "app.yaml")})
    assert _keepalives(out) == [expect], f"из {raw!r} получилось не то"


def test_keepalive_can_be_overridden_by_the_environment(tmp_path):
    """Окружение сильнее конфига — как у порта и подсети: так значение
    проверяют на живом линке, не правя конфиг бота."""
    (tmp_path / "app.yaml").write_text(
        'client_config:\n  keepalive_seconds: "20-40"\n', encoding="utf-8")
    out = _emit_bundle(tmp_path, env={"_APP_YAML": str(tmp_path / "app.yaml"),
                                      "LINK_KEEPALIVE": "31-47"})
    assert _keepalives(out) == ["31-47"]


def test_a_config_without_keepalive_does_not_grow_one(tmp_path):
    """Правка строки — именно правка: конфиг без PersistentKeepalive (пир без
    keepalive заводят намеренно) сборка не дополняет и не ломает."""
    conf = "\n".join(l for l in _CONF.splitlines()
                     if not l.startswith("PersistentKeepalive")) + "\n"
    out = _emit_bundle(tmp_path, conf_text=conf)
    assert _keepalives(out) == [], "строка появилась там, где её не было"
    assert "PresharedKey = PSKPSK==" in out, "остальной конфиг доехал"


def test_bundle_embeds_the_gw_script_byte_for_byte(bundle, tmp_path):
    """Вложенный скрипт извлекается тем же sed, что и на шлюзе."""
    f = tmp_path / "b.sh"; f.write_text(bundle, encoding="utf-8")
    r = subprocess.run(
        ["sh", "-c", f"sed -n '/^#__GW_SETUP_BELOW__$/,$p' {f} | tail -n +2"],
        capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout == GW.read_text(encoding="utf-8")


def test_bundle_installs_to_a_stable_path(bundle):
    """Скрипт настройки прописывает СЕБЯ в systemd-юнит по своему пути.

    Разложи его во временный каталог — юнит будет указывать на файл, которого
    после уборки нет. Автозапуск умрёт молча и обнаружится только после ребута,
    выглядя как «шлюз сам отвалился».
    """
    assert 'DEST="/opt/awg-gw"' in bundle
    # временный каталог есть только у --install (поставка распаковывается во
    # временный, установщик его убирает); сам скрипт обвязки — в постоянный
    body = bundle.split("# Раскладываем в ПОСТОЯННЫЙ каталог", 1)[1].split("#__GW_SETUP_BELOW__", 1)[0]
    assert "mktemp" not in body, "временный каталог ломает автозапуск"
    assert re.search(r'^"\$DEST/routing-gw-setup\.sh" ', bundle, re.M)


@pytest.mark.parametrize("argv, expect", [([], "--apply"), (["--rollback"], "--rollback")])
def test_bundle_defaults_to_apply_and_passes_rollback_through(bundle, tmp_path, argv, expect):
    """Без аргумента — применить; --rollback обязан доехать до скрипта. Прогоняем
    строку передачи управления из САМОГО бандла с подставным gw-скриптом."""
    handoff = next(line for line in bundle.splitlines()
                   if line.startswith('"$DEST/routing-gw-setup.sh"'))
    dest = tmp_path / "dest"; dest.mkdir()
    fake = dest / "routing-gw-setup.sh"
    fake.write_text('#!/bin/sh\necho "$1"\n', encoding="utf-8"); fake.chmod(0o755)
    r = subprocess.run(["sh", "-c", f'DEST="{dest}"; {handoff}', "bundle", *argv],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == expect


def test_bundle_keeps_the_root_check(bundle):
    """Без root он сделает половину и оставит шлюз в промежуточном состоянии."""
    assert 'id -u' in bundle and "нужен root" in bundle


def test_bundle_stamps_the_link_contract(bundle):
    """Штамп версии контракта — единственное, по чему человек на шлюзе поймёт,
    чем этот линк ставили. Автоматики тут нет намеренно: несовпадение обфускации
    ломает хендшейк, и об этом бот сообщает сам."""
    assert re.search(r"Контракт линка: \d+", bundle)
    assert re.search(r"# awg-bot: контракт линка \d+", bundle)


def test_bundle_is_marked_secret(bundle):
    """Внутри приватный ключ и psk — файл обязан себя объявить и сказать,
    что с ним делать после установки."""
    assert "ПРИВАТНЫЙ КЛЮЧ" in bundle
    assert "chmod 0600" in bundle


def test_bundle_carries_the_vps_hostname(bundle):
    """Имя ВПС едет в бандле: панель агента пишет «Линк до <имя>», а на шлюзе
    взять его больше неоткуда."""
    import re
    assert re.search(r'^SERVER_NAME="[A-Za-z0-9._-]{1,64}"$', bundle, re.M), \
        "в бандле нет SERVER_NAME"


def test_mail_line_lands_before_the_marker_line_not_inside_sed(bundle, services, monkeypatch, tmp_path):
    """Регресс 09.09.2026: вставка попадала в sed-выражение с тем же маркером,
    sed ломался, на шлюз ложился пустой скрипт обвязки."""
    import re, subprocess
    from awgbot.core import settings
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: bool(store.get(k, d)))
    services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    services.backup_set_passphrase("correct horse battery")
    out = services._bundle_with_mail(bundle.encode())
    text = out.decode()
    assert "sed -n '/^#__GW_SETUP_BELOW__$/,$p'" in text, "sed-выражение повреждено"
    m = re.search(r"^MAIL_B64=.*\n^BACKUP_B64=.*\n#__GW_SETUP_BELOW__$", text, re.M)
    assert m, "строки почты и фразы должны стоять прямо перед строкой-маркером"
    f = tmp_path / "b.sh"; f.write_text(text)
    extracted = subprocess.run(["sh", "-c", f"sed -n '/^#__GW_SETUP_BELOW__$/,$p' {f} | tail -n +2"],
                               capture_output=True, text=True).stdout
    assert extracted.strip() and "MAIL_B64" not in extracted, "скрипт обвязки извлекается целиком и без наших строк"
    assert '[ -s "$DEST/routing-gw-setup.sh" ]' in text, "бандл обязан отказать на пустом скрипте"


def test_bundle_carries_the_admin_devices_for_ssh(bundle):
    """SSH на шлюз через туннель — устройствам админа: список знает только бот
    ВПС, бандл вшивает его и экспортирует до gw-скрипта; чужие символы из
    окружения в бандл не попадают."""
    m = re.search(r'^ADMIN_IPS="\$\{ADMIN_IPS:-([^}]*)\}"$', bundle, re.M)
    assert m, "ADMIN_IPS не вшит"
    assert m.group(1).split() == ["10.8.1.2", "10.8.1.3", "/"], "остались только цифры, точки, слеши"
    assert "\nexport ADMIN_IPS\n" in bundle
    assert bundle.index("export ADMIN_IPS") < bundle.index('"$DEST/routing-gw-setup.sh" "${1:---apply}"')


def test_bundle_removes_itself_only_after_a_successful_apply(bundle, tmp_path):
    """Внутри приватный ключ: после успешного применения файл удаляет себя сам,
    при отказе — остаётся для повтора. Секреты установщика лежат ПОСЛЕ exit и
    при запуске не исполняются."""
    lines = bundle.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith('"$DEST/routing-gw-setup.sh"'))
    end = next(i for i, l in enumerate(lines) if l.startswith("exit "))
    tail = "\n".join(lines[start:end + 1])
    assert bundle.index("\n#__GW_SETUP_BELOW__\n") > bundle.index('exit "$_rc"'), "данные — после exit"
    dest = tmp_path / "dest"; dest.mkdir()
    fake = dest / "routing-gw-setup.sh"
    for rc, kept in ((0, False), (1, True)):
        fake.write_text(f"#!/bin/sh\nexit {rc}\n", encoding="utf-8"); fake.chmod(0o755)
        me = tmp_path / f"bundle{rc}.sh"
        me.write_text(f'#!/bin/sh\nDEST="{dest}"\n{tail}\necho НЕ_ИСПОЛНЯЕТСЯ\n', encoding="utf-8")
        r = subprocess.run(["sh", str(me)], capture_output=True, text=True)
        assert r.returncode == rc and "НЕ_ИСПОЛНЯЕТСЯ" not in r.stdout
        assert me.exists() == kept, f"rc={rc}: файл {'остался' if me.exists() else 'удалён'}"


# ── канал ВПС ↔ шлюз (концепт «канал линка»): рубильник едет бандлом ─────────

def _channel(text: str) -> tuple[str, str]:
    on = re.search(r'^LINK_CHANNEL="([^"]*)"$', text, re.M)
    port = re.search(r'^LINK_CHANNEL_PORT="([^"]*)"$', text, re.M)
    assert on and port, "строк канала в бандле нет"
    return on.group(1), port.group(1)


def test_bundle_turns_the_channel_on_and_exports_it(bundle):
    """Перевыпуск конфигурации — единственный рубильник канала: агент читает эти
    строки из юнита обвязки. Не доедут — канал не поднимется, и человек будет
    гадать, почему карточка слота молчит."""
    assert _channel(bundle) == ("1", "8787")
    assert "\nexport LINK_CHANNEL\n" in bundle and "\nexport LINK_CHANNEL_PORT\n" in bundle
    assert bundle.index("export LINK_CHANNEL") < bundle.index(
        '"$DEST/routing-gw-setup.sh" "${1:---apply}"'), "переменные встают до запуска обвязки"


def test_a_bundle_can_be_built_without_the_channel(tmp_path):
    """Откат функции — сборка конфигурации с выключенным каналом, а не правка
    кода на малине: агент, получивший 0, никуда не ходит."""
    out = _emit_bundle(tmp_path, env={"LINK_CHANNEL": "0"})
    assert _channel(out)[0] == "0"


def test_the_channel_port_comes_from_the_same_key_the_server_listens_on(tmp_path):
    """Порт брался из двух мест, и сменивший его в конфиге получал шлюз,
    стучащийся туда, где никто не слушает. Бандл обязан читать тот же ключ, что
    слушатель, — иначе канал молча не поднимается."""
    (tmp_path / "app.yaml").write_text(
        "routing:\n  link_channel_port: 9099\n", encoding="utf-8")
    out = _emit_bundle(tmp_path, env={"_APP_YAML": str(tmp_path / "app.yaml")})
    assert _channel(out)[1] == "9099"


def test_a_commented_out_key_is_not_a_value(tmp_path):
    """Ключ в поставочном app.yaml закомментирован. Прочитай сборка комментарий
    как значение — и любая правка соседней строки меняла бы порт канала."""
    (tmp_path / "app.yaml").write_text(
        "routing:\n  # link_channel_port: 9099\n", encoding="utf-8")
    out = _emit_bundle(tmp_path, env={"_APP_YAML": str(tmp_path / "app.yaml")})
    assert _channel(out)[1] == "8787"


def test_the_environment_wins_over_the_config_for_the_channel_port(tmp_path):
    """Окружение сильнее конфига — как у подсети и keepalive: бот передаёт порт
    из того же ключа, на котором поднял слушатель, и его значение главнее."""
    (tmp_path / "app.yaml").write_text(
        "routing:\n  link_channel_port: 9099\n", encoding="utf-8")
    out = _emit_bundle(tmp_path, env={"_APP_YAML": str(tmp_path / "app.yaml"),
                                      "LINK_CHANNEL_PORT": "7171"})
    assert _channel(out)[1] == "7171"


def test_junk_in_the_channel_port_does_not_reach_the_gateway(tmp_path):
    """Значение едет в юнит на чужой машине: мусор там сломал бы разбор у агента
    или подменил строку Environment=."""
    out = _emit_bundle(tmp_path, env={"LINK_CHANNEL_PORT": "9 0 9 9; rm -rf /",
                                      "LINK_CHANNEL": "да"})
    on, port = _channel(out)
    assert port == "9099" and on == "", "в бандл уехало непроверенное значение"
