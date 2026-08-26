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
            out = b"1.1.1.1 via 203.0.113.1 dev eth0 src 203.0.113.10 uid 0\n"
        else:
            out = b""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr=b"")

    monkeypatch.setattr(awg, "_run", fake_run)
    targets = awg.host_ssh_targets()
    assert targets == ["172.17.0.1", "172.29.172.1", "203.0.113.10"]


def test_host_ssh_targets_survives_docker_failure(monkeypatch):
    """Если docker недоступен — не падаем, возвращаем что смогли (egress)."""
    def fake_run(args, **kw):
        if args[:2] == ["docker", "inspect"]:
            raise awg.AwgError("no docker")
        return subprocess.CompletedProcess(
            args, 0, stdout=b"1.1.1.1 dev eth0 src 203.0.113.10\n", stderr=b"")
    monkeypatch.setattr(awg, "_run", fake_run)
    assert awg.host_ssh_targets() == ["203.0.113.10"]


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

    awg.ssh_reconcile(["10.8.1.5", "10.8.1.6"], ["172.29.172.1", "203.0.113.10"])

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


def test_ssh_rules_cover_the_migration_interface(monkeypatch):
    """Правила ставятся на КАЖДЫЙ интерфейс, а не только на дефолтный.

    Админское устройство переезжает первым — оно и есть проверка всей затеи.
    Пакет с нового интерфейса не подходил ни под один ACCEPT и ни под один DROP,
    проваливался сквозь цепочку и упирался в общий фильтр хоста: админ терял SSH
    ровно после собственного переезда. Цепочка при этом выглядит исправной, у
    правил нулевые счётчики, и связать отказ с переездом неоткуда — тот же
    класс, что был у маркировки условной маршрутизации.
    """
    calls = []
    monkeypatch.setattr(awg, "_exec", lambda args, **kw: (
        calls.append(args),
        subprocess.CompletedProcess(
            args, 1 if args[:2] == ["iptables", "-C"] else 0, stdout=b"", stderr=b"")
    )[1])
    monkeypatch.setattr(cfg, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(cfg, "MIGRATION_INTERFACE", "awg1")

    awg.ssh_reconcile(["10.8.1.5", "10.9.1.2"], ["203.0.113.10"])

    rules = [c for c in calls if "-A" in c and c[1] == "-A"]
    for iface in ("awg0", "awg1"):
        got = [c for c in rules if iface in c]
        assert any("ACCEPT" in c and "10.9.1.2/32" in c for c in got), \
            f"нет разрешения переехавшему на {iface}"
        assert any("DROP" in c for c in got), f"нет глухого DROP на {iface}"

    # DROP-и идут ПОСЛЕ всех ACCEPT-ов: DROP первого интерфейса, попав между,
    # накрыл бы разрешения второго — и порядок интерфейсов молча решал бы, кому
    # можно ходить по SSH.
    accepts = [n for n, c in enumerate(rules) if "ACCEPT" in c]
    drops = [n for n, c in enumerate(rules) if "DROP" in c]
    assert max(accepts) < min(drops)


def test_ssh_failsafe_is_placed_on_every_interface(monkeypatch):
    """Страж fail-closed ставится в conf КАЖДОГО интерфейса.

    Он закрывает промежуток между подъёмом интерфейса и реассертом бота — а
    промежуток этот у каждого интерфейса свой. С одним лишь дефолтным новый
    интерфейс поднимался бы с открытым SSH-к-хосту до ближайшего тика.
    """
    monkeypatch.setattr(cfg, "AWG_RUNTIME", "host")
    monkeypatch.setattr(cfg, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(cfg, "MIGRATION_INTERFACE", "awg1")
    written: dict = {}
    monkeypatch.setattr(awg, "read_file",
                        lambda p: "[Interface]\nAddress = 10.8.1.0/24\n\n[Peer]\nPublicKey = x\n")
    monkeypatch.setattr(awg, "write_file", lambda p, c: written.__setitem__(p, c))
    monkeypatch.setattr(awg, "_backup_conf", lambda iface=None: None)
    monkeypatch.setattr(awg, "_exec",
                        lambda args, **kw: subprocess.CompletedProcess(args, 0, b"", b""))

    assert awg.ensure_ssh_failsafe() is True
    assert len(written) == 2, f"страж поставлен не везде: {list(written)}"
    for path, body in written.items():
        iface = "awg1" if "awg1" in path else "awg0"
        assert f"-i {iface}" in body, f"в {path} правило чужого интерфейса"


def test_ssh_failsafe_survives_absent_migration_conf(monkeypatch):
    """Интерфейса переезда может ещё не быть — его conf не создан. Это не отказ:
    дефолтный обслуживается, отсутствующий пропускается. Молчание ДЕФОЛТНОГО,
    напротив, скрывать нельзя — там это настоящая поломка."""
    monkeypatch.setattr(cfg, "AWG_RUNTIME", "host")
    monkeypatch.setattr(cfg, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(cfg, "MIGRATION_INTERFACE", "awg1")

    def read(path):
        if "awg1" in path:
            raise awg.AwgError("нет такого файла")
        return "[Interface]\nAddress = 10.8.1.0/24\n\n[Peer]\nPublicKey = x\n"

    written: dict = {}
    monkeypatch.setattr(awg, "read_file", read)
    monkeypatch.setattr(awg, "write_file", lambda p, c: written.__setitem__(p, c))
    monkeypatch.setattr(awg, "_backup_conf", lambda iface=None: None)
    monkeypatch.setattr(awg, "_exec",
                        lambda args, **kw: subprocess.CompletedProcess(args, 0, b"", b""))

    assert awg.ensure_ssh_failsafe() is True
    assert len(written) == 1 and "awg0" in next(iter(written))


def test_ssh_reconcile_no_targets_is_noop(monkeypatch):
    """Пустой targets → ни одной команды (безопаснее не трогать, чем криво)."""
    calls = []
    monkeypatch.setattr(awg, "_exec",
                        lambda args, **kw: calls.append(args) or
                        subprocess.CompletedProcess(args, 0, b"", b""))
    awg.ssh_reconcile(["10.8.1.5"], [])
    assert calls == []


def test_ssh_reconcile_no_admin_ips_is_noop(monkeypatch):
    """Пустой admin_ips → тоже ни одной команды, и цена ошибки тут выше.

    Без охраны желаемое состояние вырождается в одни DROP-и: SSH-к-хосту из
    туннеля закрыт всем, включая того, кто пришёл бы это чинить. Пустым список
    бывает в переходных состояниях — админ ещё без устройств, БД читается в
    момент пересоздания, — то есть ровно тогда, когда доступ нужнее всего.
    Симметричная охрана на targets стояла с самого начала, эта — нет.
    """
    calls = []
    monkeypatch.setattr(awg, "_exec",
                        lambda args, **kw: calls.append(args) or
                        subprocess.CompletedProcess(args, 0, b"", b""))
    awg.ssh_reconcile([], ["10.8.1.0"])
    assert calls == [], "пустой вайтлист повесил бы DROP на всех"


def test_ssh_reconcile_skips_jump_if_present(monkeypatch):
    """Если джамп уже есть (-C код 0) — повторно не вставляем."""
    calls = []

    def fake_exec(args, **kw):
        calls.append(args)
        rc = 1 if args[:3] == ["iptables", "-C", "INPUT"] else 0
        return subprocess.CompletedProcess(args, rc, b"", b"")

    monkeypatch.setattr(awg, "_exec", fake_exec)
    awg.ssh_reconcile(["10.8.1.5"], ["172.29.172.1"])
    assert ["iptables", "-I", "FORWARD", "1", "-j", "AWGBOT_SSH"] not in calls


# ── точка врезки цепочки: INPUT на хосте, FORWARD в контейнере ───────────────

def _collect(monkeypatch, *, present=()):
    """Прогон ssh_reconcile с фейковым iptables. present — цепочки, в которых
    джамп якобы уже есть."""
    calls = []

    def fake_exec(args, **kw):
        calls.append(args)
        if args[:2] == ["iptables", "-C"]:
            rc = 0 if args[2] in present else 1
            return subprocess.CompletedProcess(args, rc, b"", b"")
        return subprocess.CompletedProcess(args, 1, b"", b"")

    monkeypatch.setattr(awg, "_exec", fake_exec)
    awg.ssh_reconcile(["10.8.1.5"], ["10.8.1.0"])
    return calls


def test_ssh_chain_hooks_into_input_on_the_host(monkeypatch):
    """На хосте цель фильтра — адрес самой машины, значит INPUT, а не FORWARD.

    Пакет из туннеля на локальный адрес netfilter отдаёт в INPUT; через FORWARD
    ходит только транзит. Пока врезка была захардкожена в FORWARD, на хосте
    цепочка собиралась правильной и не срабатывала никогда — счётчики по всем
    правилам оставались нулевыми, и выглядело это как исправная защита.
    """
    monkeypatch.setattr(cfg, "AWG_RUNTIME", "host")
    calls = _collect(monkeypatch)
    assert ["iptables", "-I", "INPUT", "1", "-j", "AWGBOT_SSH"] in calls
    assert ["iptables", "-I", "FORWARD", "1", "-j", "AWGBOT_SSH"] not in calls


def test_ssh_chain_hooks_into_forward_in_the_container(monkeypatch):
    """В контейнере цель (шлюз docker-сети) чужая — пакет транзитный, FORWARD."""
    monkeypatch.setattr(cfg, "AWG_RUNTIME", "docker")
    calls = _collect(monkeypatch)
    assert ["iptables", "-I", "FORWARD", "1", "-j", "AWGBOT_SSH"] in calls
    assert ["iptables", "-I", "INPUT", "1", "-j", "AWGBOT_SSH"] not in calls


def test_stale_jump_from_the_previous_mode_is_removed(monkeypatch):
    """Переезд на хост оставлял джамп в FORWARD — его надо снимать.

    Иначе рядом с рабочей врезкой висит вторая, ведущая в ту же цепочку из
    чужого потока. Она безвредна, но показывает исправно выглядящее правило с
    нулевыми счётчиками — ровно та картина, которая скрыла ошибку в прошлый раз.
    """
    monkeypatch.setattr(cfg, "AWG_RUNTIME", "host")
    calls = _collect(monkeypatch, present=("FORWARD",))
    assert ["iptables", "-D", "FORWARD", "-j", "AWGBOT_SSH"] in calls
    assert ["iptables", "-I", "INPUT", "1", "-j", "AWGBOT_SSH"] in calls


def test_failsafe_postup_hooks_where_the_bot_hooks(monkeypatch):
    """Страж в PostUp и фильтр бота обязаны врезаться в ОДНУ цепочку.

    Разойдись они — глухой DROP встанет в потоке, которого нет, и окно между
    подъёмом awg0 и реассертом бота останется открытым.
    """
    for mode, hook in (("host", "INPUT"), ("docker", "FORWARD")):
        monkeypatch.setattr(cfg, "AWG_RUNTIME", mode)
        line = awg._ssh_failsafe_postup()
        assert f"-I {hook} 1 -j AWGBOT_SSH" in line, mode
        other = "FORWARD" if hook == "INPUT" else "INPUT"
        assert f"-I {other} 1 -j AWGBOT_SSH" not in line, mode


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
            # джамп есть ровно там, где ему положено быть в этом режиме,
            # и НЕ висит в прежней точке врезки
            rc = 0 if args[2] == "FORWARD" else 1
            return subprocess.CompletedProcess(args, rc, b"", b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(awg, "_exec", fake_exec)
    awg.ssh_reconcile(["10.8.1.5"], ["172.29.172.1"])
    mutating = [c for c in calls if c[1] in ("-F", "-A", "-I", "-N", "-D")]
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
    monkeypatch.setattr(awgmod, "_backup_conf", lambda iface=None: None)
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
