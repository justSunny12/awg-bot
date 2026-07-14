"""Пер-пирный SSH-к-хосту: деривация target-адресов, сборка правил в контейнере,
и врезки в поток (создание админского устройства, реассерт по админским адресам).
"""
import subprocess

from awgbot.infra import awg
import awgbot.core.config as cfg


# ── деривация адресов хоста (host_ssh_targets) ───────────────────────────────

def test_host_ssh_targets_parses_gateways_and_egress(monkeypatch):
    """Шлюзы docker-сетей контейнера + egress-IP из `ip route get`, без дублей и
    мусора; порядок сохранён, невалидное отсеяно."""
    def fake_run(args, **kw):
        if args[:2] == ["docker", "inspect"]:
            out = b"172.17.0.1\n172.29.172.1\n\n"          # + пустая строка
        elif args[:3] == ["ip", "route", "get"]:
            out = b"1.1.1.1 via 88.218.78.1 dev eth0 src 88.218.78.157 uid 0\n"
        else:
            out = b""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr=b"")

    monkeypatch.setattr(awg, "_run", fake_run)
    targets = awg.host_ssh_targets()
    assert targets == ["172.17.0.1", "172.29.172.1", "88.218.78.157"]


def test_host_ssh_targets_survives_docker_failure(monkeypatch):
    """Если docker недоступен — не падаем, возвращаем что смогли (egress)."""
    def fake_run(args, **kw):
        if args[:2] == ["docker", "inspect"]:
            raise awg.AwgError("no docker")
        return subprocess.CompletedProcess(
            args, 0, stdout=b"1.1.1.1 dev eth0 src 88.218.78.157\n", stderr=b"")
    monkeypatch.setattr(awg, "_run", fake_run)
    assert awg.host_ssh_targets() == ["88.218.78.157"]


# ── сборка правил в контейнере (ssh_reconcile) ───────────────────────────────

def test_ssh_reconcile_emits_expected_chain(monkeypatch):
    """Проверяем последовательность iptables: цепочка → джамп → flush →
    ACCEPT (admin×target) → DROP (target). Порт берётся из config.SSH_PORT."""
    calls = []

    def fake_exec(args, **kw):
        calls.append(args)
        # -C FORWARD -j CHAIN → «джампа нет» (код 1), чтобы код его вставил
        rc = 1 if args[:2] == ["iptables", "-C"] else 0
        return subprocess.CompletedProcess(args, rc, stdout=b"", stderr=b"")

    monkeypatch.setattr(awg, "_exec", fake_exec)
    monkeypatch.setattr(cfg, "SSH_PORT", 2222)

    awg.ssh_reconcile(["10.8.1.5", "10.8.1.6"], ["172.29.172.1", "88.218.78.157"])

    # цепочка создаётся, джамп вставляется в начало FORWARD, затем flush
    assert ["iptables", "-N", "AWGBOT_SSH"] in calls
    assert ["iptables", "-I", "FORWARD", "1", "-j", "AWGBOT_SSH"] in calls
    assert ["iptables", "-F", "AWGBOT_SSH"] in calls

    accepts = [c for c in calls if "-A" in c and "ACCEPT" in c]
    drops = [c for c in calls if "-A" in c and "DROP" in c]
    # 2 админа × 2 target = 4 ACCEPT; 2 target = 2 DROP
    assert len(accepts) == 4 and len(drops) == 2
    # порт и адрес источника попали в правило
    assert all("2222" in c for c in accepts + drops)
    assert any("10.8.1.5/32" in c for c in accepts)
    # ACCEPT-и идут раньше DROP-ов (иначе админа зарежет catch-all)
    assert calls.index(accepts[-1]) < calls.index(drops[0])


def test_ssh_reconcile_no_targets_is_noop(monkeypatch):
    """Пустой targets → ни одной команды (безопаснее не трогать, чем криво)."""
    calls = []
    monkeypatch.setattr(awg, "_exec",
                        lambda args, **kw: calls.append(args) or
                        subprocess.CompletedProcess(args, 0, b"", b""))
    awg.ssh_reconcile(["10.8.1.5"], [])
    assert calls == []


def test_ssh_reconcile_skips_jump_if_present(monkeypatch):
    """Если джамп уже есть (-C код 0) — повторно не вставляем."""
    calls = []

    def fake_exec(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")  # всё «есть»

    monkeypatch.setattr(awg, "_exec", fake_exec)
    awg.ssh_reconcile(["10.8.1.5"], ["172.29.172.1"])
    assert ["iptables", "-I", "FORWARD", "1", "-j", "AWGBOT_SSH"] not in calls


# ── врезки в поток (services) ────────────────────────────────────────────────

def test_admin_device_creation_reconciles_ssh(services, fake_awg, make_active_client):
    """Создание устройства АДМИНА (tg_id == ADMIN_ID) сразу накладывает SSH-фильтр
    с его адресом."""
    admin = make_active_client(name="Админ", tg_id=cfg.ADMIN_ID)
    dc = services.add_device(admin.id, "laptop")
    assert fake_awg.ssh_rules is not None
    admin_ips, targets = fake_awg.ssh_rules
    assert dc.address in admin_ips
    assert targets == fake_awg.ssh_targets


def test_nonadmin_device_creation_does_not_reconcile_ssh(services, fake_awg,
                                                         make_active_client):
    """Создание устройства обычного клиента SSH-фильтр не трогает."""
    user = make_active_client(name="Юзер", tg_id=1000)
    services.add_device(user.id, "phone")
    assert fake_awg.ssh_rules is None


def test_reconcile_ssh_access_collects_only_admin_addresses(
        services, fake_awg, make_active_client):
    """reconcile_ssh_access собирает адреса ТОЛЬКО админских устройств."""
    admin = make_active_client(name="Админ", tg_id=cfg.ADMIN_ID)
    user = make_active_client(name="Юзер", tg_id=2000)
    a1 = services.add_device(admin.id, "laptop")
    a2 = services.add_device(admin.id, "phone")
    services.add_device(user.id, "user-phone")     # не должен попасть

    fake_awg.ssh_rules = None
    services.reconcile_ssh_access()
    admin_ips, _ = fake_awg.ssh_rules
    assert set(admin_ips) == {a1.address, a2.address}


def test_ssh_reconcile_diff_skip(monkeypatch):
    """Если текущее содержимое цепочки уже совпадает с желаемым (и джамп есть) —
    ни одного мутирующего вызова (флаша/добавлений)."""
    import awgbot.core.config as cfg
    monkeypatch.setattr(cfg, "SSH_PORT", 22)
    desired_dump = (
        "-N AWGBOT_SSH\n"
        "-A AWGBOT_SSH -s 10.8.1.5/32 -d 172.29.172.1/32 -i awg0 "
        "-p tcp -m tcp --dport 22 -j ACCEPT\n"
        "-A AWGBOT_SSH -d 172.29.172.1/32 -i awg0 "
        "-p tcp -m tcp --dport 22 -j DROP\n")
    calls = []

    def fake_exec(args, **kw):
        calls.append(args)
        if args[:2] == ["iptables", "-S"]:
            return subprocess.CompletedProcess(args, 0, desired_dump.encode(), b"")
        if args[:2] == ["iptables", "-C"]:
            return subprocess.CompletedProcess(args, 0, b"", b"")   # джамп есть
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(awg, "_exec", fake_exec)
    awg.ssh_reconcile(["10.8.1.5"], ["172.29.172.1"])
    mutating = [c for c in calls if c[1] in ("-F", "-A", "-I", "-N")]
    assert mutating == []                                 # только -S и -C


def test_admin_addresses_exclude_service_client(services, fake_awg, make_active_client):
    """Устройства служебного клиента (без профиля) не попадают в SSH-вайтлист,
    даже теоретически — запрос исключает is_service."""
    import awgbot.core.config as cfg
    admin = make_active_client(name="Админ", tg_id=cfg.ADMIN_ID)
    a1 = services.add_device(admin.id, "laptop")
    # устройство на служебном клиенте («без профиля»)
    service_id = services.db.get_service_client_id()
    services.db.create_device(service_id, "orphan", "pubX", "pskX", "10.8.1.200",
                              private_key=None)
    ips = services.db.admin_device_addresses(cfg.ADMIN_ID)
    assert a1.address in ips and "10.8.1.200" not in ips


def test_ssh_failsafe_injects_postup_idempotent(monkeypatch):
    """ensure_ssh_failsafe вставляет PostUp с AWGBOT_SSH в [Interface] один раз;
    повторный вызов (маркер уже есть) — no-op."""
    import awgbot.infra.awg as awgmod
    store = {"conf": "[Interface]\nAddress = 10.8.1.0/24\nListenPort = 42755\n\n"
                     "[Peer]\nPublicKey = abc\nAllowedIPs = 10.8.1.2/32\n"}
    monkeypatch.setattr(awgmod, "read_file", lambda p: store["conf"])
    monkeypatch.setattr(awgmod, "write_file", lambda p, t: store.__setitem__("conf", t))
    monkeypatch.setattr(awgmod, "_backup_conf", lambda: None)
    import contextlib
    monkeypatch.setattr(awgmod, "writing", contextlib.nullcontext)

    assert awgmod.ensure_ssh_failsafe() is True           # вставил
    assert "PostUp" in store["conf"] and "AWGBOT_SSH" in store["conf"]
    assert "[Peer]" in store["conf"]                       # пиры не потеряны
    assert awgmod.ensure_ssh_failsafe() is False           # уже есть — no-op


def test_ssh_failsafe_postup_always_exits_zero():
    """Строка PostUp завершается 'true' — awg-quick не оборвёт подъём awg0
    из-за ненулевого кода iptables."""
    import awgbot.infra.awg as awgmod
    line = awgmod._ssh_failsafe_postup()
    assert line.startswith("PostUp = ")
    assert line.rstrip().endswith("true")
    assert "--dport 22 -j DROP" in line                    # именно fail-closed DROP


def test_ssh_failsafe_postup_uses_config_port(monkeypatch):
    """Fail-closed DROP ставится на config.SSH_PORT, а не на захардкоженный 22."""
    import awgbot.core.config as cfg
    import awgbot.infra.awg as awgmod
    monkeypatch.setattr(cfg, "SSH_PORT", 2222)
    line = awgmod._ssh_failsafe_postup()
    assert "--dport 2222" in line and "--dport 22 " not in line
