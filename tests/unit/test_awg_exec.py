"""Unit: docker-exec обёртки awg.py с замоканными примитивами (_exec/_exec_sh/
_exec_i/_run). Фиксируем и разбор вывода, и КОМАНДЫ (валидация + конструкция).

Эти тесты НЕ используют fake_awg (он подменяет весь модуль) — работаем с
настоящими функциями awg, мокая только низкоуровневый ввод-вывод.
"""
import subprocess

import pytest

from awgbot.infra import awg

pytestmark = pytest.mark.unit

_VALID_PUB = "A" * 43 + "="            # проходит _RE_PUBKEY


def _cp(stdout=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=b"")


# ── валидация ────────────────────────────────────────────────────────────────
def test_validate_key_ok_and_bad():
    assert awg._validate_key(_VALID_PUB) == _VALID_PUB
    with pytest.raises(awg.AwgError):
        awg._validate_key("not-a-key")


def test_validate_ip_ok_and_bad():
    assert awg._validate_ip("10.8.0.5") == "10.8.0.5"
    for bad in ("10.8.0", "999.1.1.1", "abc"):
        with pytest.raises(awg.AwgError):
            awg._validate_ip(bad)


# ── чтение файлов ────────────────────────────────────────────────────────────
def test_read_file_decodes(monkeypatch):
    monkeypatch.setattr(awg, "_exec", lambda args, **k: _cp(b"hello \xff world"))
    assert "hello" in awg.read_file("/x")


# ── show_dump ────────────────────────────────────────────────────────────────
def test_show_dump_parses(monkeypatch):
    dump = ("IFACE_PRIV\tIFACE_PUB\t43125\toff\n"
            "pubkey\tpsk\t1.2.3.4:51820\t10.8.0.2/32\t1700000000\t100\t200\t25\n")
    monkeypatch.setattr(awg, "_exec", lambda args, **k: _cp(dump.encode()))
    peers = awg.show_dump()
    assert peers and peers[0]["rx"] == 100 and peers[0]["tx"] == 200


# ── iptables block/unblock/is_blocked (команды) ──────────────────────────────
def test_is_blocked_reads_returncode(monkeypatch):
    monkeypatch.setattr(awg, "_exec", lambda args, **k: _cp(returncode=0))
    assert awg.is_blocked("10.8.0.5") is True
    monkeypatch.setattr(awg, "_exec", lambda args, **k: _cp(returncode=1))
    assert awg.is_blocked("10.8.0.5") is False


def test_block_ip_inserts_drop_when_absent(monkeypatch):
    calls = []
    monkeypatch.setattr(awg, "is_blocked", lambda ip: False)
    monkeypatch.setattr(awg, "_exec", lambda args, **k: calls.append(args) or _cp())
    awg.block_ip("10.8.0.9")
    assert calls and calls[0][:3] == ["iptables", "-I", "FORWARD"]
    assert "10.8.0.9/32" in calls[0]


def test_block_ip_noop_when_already_blocked(monkeypatch):
    calls = []
    monkeypatch.setattr(awg, "is_blocked", lambda ip: True)
    monkeypatch.setattr(awg, "_exec", lambda args, **k: calls.append(args) or _cp())
    awg.block_ip("10.8.0.9")
    assert calls == []                                       # идемпотентно


def test_unblock_ip_deletes_drop(monkeypatch):
    calls = []
    monkeypatch.setattr(awg, "is_blocked", lambda ip: True)
    monkeypatch.setattr(awg, "_exec", lambda args, **k: calls.append(args) or _cp())
    awg.unblock_ip("10.8.0.9")
    assert calls and calls[0][:3] == ["iptables", "-D", "FORWARD"]


def test_block_ip_rejects_bad_ip(monkeypatch):
    with pytest.raises(awg.AwgError):
        awg.block_ip("nope")


# ── контейнер (docker inspect / restart) ─────────────────────────────────────
def test_container_running_and_started(monkeypatch):
    monkeypatch.setattr(awg, "_inspect", lambda fmt: "true")
    assert awg.container_running() is True
    monkeypatch.setattr(awg, "_inspect", lambda fmt: "2026-01-01T00:00:00Z")
    assert awg.container_started_at() == "2026-01-01T00:00:00Z"
    monkeypatch.setattr(awg, "_inspect", lambda fmt: "12345")
    assert awg.container_pid() == 12345


def test_container_running_swallows_error(monkeypatch):
    def boom(fmt):
        raise awg.AwgError("down")
    monkeypatch.setattr(awg, "_inspect", boom)
    assert awg.container_running() is False
    assert awg.container_started_at() is None
    assert awg.container_pid() is None


def test_awg_responding(monkeypatch):
    monkeypatch.setattr(awg, "_exec", lambda args, **k: _cp())
    assert awg.awg_responding() is True

    def boom(args, **k):
        raise awg.AwgError("no daemon")
    monkeypatch.setattr(awg, "_exec", boom)
    assert awg.awg_responding() is False


def test_restart_container_runs_docker(monkeypatch):
    import awgbot.core.config as _cfg_rt; monkeypatch.setattr(_cfg_rt, "AWG_RUNTIME", "docker")
    calls = []
    monkeypatch.setattr(awg, "_run", lambda args, **k: calls.append(args) or _cp())
    awg.restart_container()
    assert calls[0][:2] == ["docker", "restart"]


# ── ключи ────────────────────────────────────────────────────────────────────
def test_gen_keypair(monkeypatch):
    monkeypatch.setattr(awg, "_exec_sh", lambda script, **k: _cp(f"PRIVKEY\n{_VALID_PUB}\n".encode()))
    priv, pub = awg.gen_keypair()
    assert priv == "PRIVKEY" and pub == _VALID_PUB


def test_gen_keypair_bad_output(monkeypatch):
    monkeypatch.setattr(awg, "_exec_sh", lambda script, **k: _cp(b"only-one-line\n"))
    with pytest.raises(awg.AwgError):
        awg.gen_keypair()


def test_pubkey_of(monkeypatch):
    monkeypatch.setattr(awg, "_exec_i", lambda args, input_data, **k: _cp(b"DERIVEDPUB\n"))
    assert awg.pubkey_of("somepriv") == "DERIVEDPUB"


# ── add_peer / remove_peer (оркестрация конфига) ─────────────────────────────
def _stub_conf_io(monkeypatch, conf_holder):
    monkeypatch.setattr(awg, "read_file", lambda p: conf_holder["conf"])
    monkeypatch.setattr(awg, "write_file", lambda p, c: conf_holder.__setitem__("conf", c))
    # Сигнатуры ровно как у настоящих: интерфейс необязателен, но принимается.
    # Двойник без него молча разошёлся бы с боевым кодом ровно на той правке,
    # ради которой интерфейс и появился.
    monkeypatch.setattr(awg, "_backup_conf", lambda iface=None: None)
    monkeypatch.setattr(awg, "apply_config", lambda iface=None: None)


def test_add_peer_appends_and_applies(monkeypatch):
    holder = {"conf": "[Interface]\nPrivateKey = X\nListenPort = 51820\n"}
    _stub_conf_io(monkeypatch, holder)
    psk = "B" * 43 + "="
    awg.add_peer(_VALID_PUB, psk, "10.8.0.7")
    assert _VALID_PUB in holder["conf"] and "10.8.0.7/32" in holder["conf"]


def test_add_peer_idempotent(monkeypatch):
    existing = f"[Interface]\nPrivateKey = X\n\n[Peer]\nPublicKey = {_VALID_PUB}\nAllowedIPs = 10.8.0.7/32\n"
    holder = {"conf": existing}
    _stub_conf_io(monkeypatch, holder)
    awg.add_peer(_VALID_PUB, "B" * 43 + "=", "10.8.0.8")
    assert holder["conf"] == existing                       # дубликат не добавлен


def test_remove_peer_drops_block(monkeypatch):
    conf = f"[Interface]\nPrivateKey = X\n\n[Peer]\nPublicKey = {_VALID_PUB}\nAllowedIPs = 10.8.0.7/32\n"
    holder = {"conf": conf}
    _stub_conf_io(monkeypatch, holder)
    awg.remove_peer(_VALID_PUB)
    assert _VALID_PUB not in holder["conf"]


def test_remove_peer_idempotent(monkeypatch):
    conf = "[Interface]\nPrivateKey = X\n"
    holder = {"conf": conf}
    _stub_conf_io(monkeypatch, holder)
    awg.remove_peer(_VALID_PUB)
    assert holder["conf"] == conf


# ── read_server_params (кэш/инвалидация) ─────────────────────────────────────
def test_read_server_params_caches_and_invalidates(monkeypatch):
    sep = awg._SEP
    payload = ("[Interface]\nListenPort = 51820\nPrivateKey = SRVPRIV==\n"
               + sep + "PSK==").encode()
    calls = {"n": 0}

    def fake_sh(script, **k):
        calls["n"] += 1
        return _cp(payload)
    monkeypatch.setattr(awg, "_exec_sh", fake_sh)
    monkeypatch.setattr(awg, "pubkey_of", lambda priv: "SRVPUB==")
    awg.invalidate_server_params()
    r1 = awg.read_server_params()
    assert r1["listen_port"] == 51820 and r1["server_pubkey"] == "SRVPUB=="
    awg.read_server_params()                                # в пределах TTL → кэш
    assert calls["n"] == 1
    awg.read_server_params(force=True)                      # форс → новый exec
    assert calls["n"] == 2
    awg.invalidate_server_params()
    awg.read_server_params()
    assert calls["n"] == 3
    awg.invalidate_server_params()


def test_server_pubkey_is_derived_not_read_from_a_file(monkeypatch):
    """Публичный ключ сервера обязан выводиться из PrivateKey живого конфига.

    Пока его брали из wireguard_server_public_key.key (файла контейнера
    Amnezia), файл и конфиг могли разойтись: смена серверного ключа без правки
    файла разослала бы всем конфиги с ЧУЖИМ публичным ключом, и заметили бы это
    только при первом переподключении.
    """
    sep = awg._SEP
    monkeypatch.setattr(awg, "_exec_sh", lambda script, **k: _cp(
        ("[Interface]\nPrivateKey = THEPRIV==\nListenPort = 443\n" + sep + "PSK==").encode()))
    monkeypatch.setattr(awg, "pubkey_of",
                        lambda priv: "DERIVED" if priv == "THEPRIV==" else "WRONG")
    awg.invalidate_server_params()
    assert awg.read_server_params()["server_pubkey"] == "DERIVED"
    awg.invalidate_server_params()


def test_server_params_refuse_conf_without_private_key(monkeypatch):
    """Нет PrivateKey — отказ, а не пустой публичный ключ в конфигах клиентов."""
    sep = awg._SEP
    monkeypatch.setattr(awg, "_exec_sh", lambda script, **k: _cp(
        ("[Interface]\nListenPort = 443\n" + sep + "PSK==").encode()))
    awg.invalidate_server_params()
    with pytest.raises(awg.AwgError, match="PrivateKey"):
        awg.read_server_params()
    awg.invalidate_server_params()


# ── приватный ключ сервера не должен светиться на диске ──────────────────────

def test_apply_config_does_not_use_a_predictable_tmp_path(monkeypatch):
    """В распакованном конфиге лежит ПРИВАТНЫЙ КЛЮЧ СЕРВЕРА.

    Пока это был /tmp контейнера (свой mount namespace, кроме root никого),
    риска не было. После переезда на хост тот же код стал ронять ключ в общий
    /tmp — редирект создаёт файл с обычной маской, читаемой всеми, а
    предсказуемое имя там же открывает подмену симлинком.
    """
    seen = {}
    monkeypatch.setattr(awg, "_exec_sh", lambda s, **k: seen.setdefault("s", s))
    awg.apply_config()
    script = seen["s"]

    assert "/tmp/" not in script, "временный файл с ключом не должен жить в /tmp"
    assert "umask 077" in script, "без umask файл создастся читаемым всеми"
    assert "mktemp" in script, "фиксированное имя открывает подмену симлинком"
    assert "rm -f" in script, "файл обязан убираться в любом исходе"


def test_shell_scripts_quote_config_paths(monkeypatch):
    """Пути приходят из yaml, который правят руками.

    Пробел или точка с запятой в awg_dir без кавычек превратились бы в
    исполнение произвольной команды от root.
    """
    import pathlib
    src = pathlib.Path(awg.__file__).read_text(encoding="utf-8")
    for bad in ('cat > {path}', 'cat {config.CONF_PATH};',
                'strip {config.CONF_PATH}'):
        assert bad not in src, f"незакавыченный путь в shell-строке: {bad}"


# ── адресация по интерфейсу (переезд профилей) ───────────────────────────────

def test_iface_of_resolves_empty_to_default():
    """Пустое значение — это «интерфейс по умолчанию», а не «неизвестно».

    На конвенции держится миграция БД без бэкфилла: старые строки хранят пустую
    строку и продолжают работать. Разрешать её обязана одна функция, иначе
    трактовка расползётся по вызовам и однажды разойдётся.
    """
    from awgbot.core import config
    assert awg.iface_of(None) == config.AWG_INTERFACE
    assert awg.iface_of("") == config.AWG_INTERFACE
    assert awg.iface_of("awg1") == "awg1"


def test_conf_path_keeps_default_override(monkeypatch):
    """У дефолтного интерфейса путь берётся из config.CONF_PATH, а не собирается
    по шаблону: боевой конфиг может лежать не там, где подсказывает имя, и
    сборка «по шаблону» увела бы бота от живого файла."""
    from awgbot.core import config
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "AWG_DIR", "/opt/awg")
    monkeypatch.setattr(config, "CONF_PATH", "/etc/elsewhere/awg0.conf")
    assert awg.conf_path() == "/etc/elsewhere/awg0.conf"
    assert awg.conf_path("awg0") == "/etc/elsewhere/awg0.conf"
    assert awg.conf_path("awg1") == "/opt/awg/awg1.conf"


def test_server_params_are_cached_per_interface(monkeypatch):
    """Кэш параметров — ПО интерфейсу.

    Общий кэш отдавал бы параметры старого сервера для конфигов нового: чужой
    порт, чужой серверный ключ, чужая обфускация. Отказ молчаливый — превью
    выглядит нормально, а не подключается никто. Это опаснее всех прочих мест,
    где интерфейс имеет значение.
    """
    from awgbot.core import config
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "AWG_DIR", "/opt/awg")
    monkeypatch.setattr(config, "CONF_PATH", "/opt/awg/awg0.conf")
    monkeypatch.setattr(config, "PSK_PATH", "/opt/awg/psk")
    monkeypatch.setattr(awg, "pubkey_of", lambda priv: f"pub-of-{priv}")

    confs = {
        "/opt/awg/awg0.conf": "[Interface]\nPrivateKey = OLD\nListenPort = 42755\nJc = 4\n",
        "/opt/awg/awg1.conf": "[Interface]\nPrivateKey = NEW\nListenPort = 443\nJc = 9\n",
    }

    def fake_exec_sh(script, **kw):
        path = [p for p in confs if p in script][0]
        import types
        return types.SimpleNamespace(
            stdout=(confs[path] + awg._SEP + "PSK==").encode())

    monkeypatch.setattr(awg, "_exec_sh", fake_exec_sh)
    awg.invalidate_server_params()

    old = awg.read_server_params()
    new = awg.read_server_params(iface="awg1")
    assert old["listen_port"] == 42755 and new["listen_port"] == 443
    assert old["server_pubkey"] != new["server_pubkey"], "серверный ключ взят от чужого интерфейса"
    assert old["obfuscation"]["Jc"] == "4" and new["obfuscation"]["Jc"] == "9"

    # повторный вызов идёт из кэша своего интерфейса, не подменяя соседний
    assert awg.read_server_params()["listen_port"] == 42755
    awg.invalidate_server_params("awg1")
    assert awg.read_server_params()["listen_port"] == 42755, "сброс соседа затронул чужой кэш"
    awg.invalidate_server_params()


def test_remove_peer_touches_the_named_interface(monkeypatch):
    """Удаление идёт по conf УКАЗАННОГО интерфейса.

    Во время переезда старый пир живёт в конфиге старого интерфейса. Удаление по
    дефолтному пути оставило бы его работать: человек видит устройство удалённым,
    а доступ у него остался.
    """
    from awgbot.core import config
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "AWG_DIR", "/opt/awg")
    monkeypatch.setattr(config, "CONF_PATH", "/opt/awg/awg0.conf")
    files = {
        "/opt/awg/awg0.conf": f"[Interface]\nPrivateKey = X\n\n[Peer]\nPublicKey = {_VALID_PUB}\nAllowedIPs = 10.8.1.5/32\n",
        "/opt/awg/awg1.conf": f"[Interface]\nPrivateKey = Y\n\n[Peer]\nPublicKey = {_VALID_PUB}\nAllowedIPs = 10.9.1.5/32\n",
    }
    applied: list = []
    monkeypatch.setattr(awg, "read_file", lambda p: files[p])
    monkeypatch.setattr(awg, "write_file", lambda p, c: files.__setitem__(p, c))
    monkeypatch.setattr(awg, "_backup_conf", lambda iface=None: None)
    monkeypatch.setattr(awg, "apply_config", lambda iface=None: applied.append(iface))

    awg.remove_peer(_VALID_PUB, iface="awg1")
    assert _VALID_PUB not in files["/opt/awg/awg1.conf"]
    assert _VALID_PUB in files["/opt/awg/awg0.conf"], "снесли пира на чужом интерфейсе"
    assert applied == ["awg1"], "применили конфиг не того интерфейса"
