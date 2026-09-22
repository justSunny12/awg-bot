"""
tools/gwssh.py — `awg-bot ssh …` на шлюзе: доступ по SSH из терминала, та же
механика, что в разделе «🛡 Доступ по SSH» агента (domain/gwssh).

  status                 порт (факт и владелец), туннель, снаружи, адреса, резолв
  port <N>               перевести sshd и фильтр на порт (владелец конфига — бот)
  allow <ip|cidr|имя> …  адреса для входа снаружи (только IPv4) и применить
  deny  <ip|cidr|имя> …  убрать из списка и применить
  on | off               фильтр снаружи: включить (нужна обвязка нового образца) / выключить

Доверенные адреса из туннеля (ADMIN_IPS_EXTRA) — по-прежнему `awg-bot firewall
allow/deny`. Порт задаёт чужой владелец (OMV) — `port` отказывает и говорит, где.
"""
from __future__ import annotations

import sys

from awgbot.core import config


def _svc():
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    db = Database(config.DB_PATH)
    db.init_schema()
    return GatewayServices(db)


def cmd_status(_args) -> int:
    st = _svc().ssh_screen()
    owner = {"omv": "OMV (Службы → SSH)", "generator": "другой процесс"}.get(st["owner"], "бот")
    port = "sshd не запущен" if st["sshd_down"] else str(st["port"])
    print(f"порт SSH          : {port}  владелец конфига: {owner}")
    if st.get("owner_port") and st["owner_port"] != st["port"]:
        print(f"                    в OMV задан {st['owner_port']} — нажми «Применить» в OMV")
    if st.get("env_port") is not None and st["env_port"] != st["port"]:
        print(f"                    в firewall.env {st['env_port']} — агент поправит следующим тиком")
    extra = [p for p in st["ports"] if p != st["port"]]
    if extra:
        print(f"                    sshd слушает ещё: {', '.join(map(str, extra))}")
    print(f"из туннеля        : устройства админа ({len(st['admin_ips'])}) и сервер по линку — всегда")
    lan = ", ".join(st.get("lan") or [])
    print(f"из локальной сети : открыт всегда{' (' + lan + ')' if lan else ''}")
    if not st["new_plumbing"]:
        print("снаружи           : обвязка старого образца — фильтр появится после перевыпуска конфигурации шлюза")
    else:
        print(f"снаружи           : фильтр {'включён' if st['filter'] else 'выключен'}")
    print(f"адреса снаружи    : {', '.join(st['allow']) or '—'}")
    if st["resolved"]:
        print(f"в наборе          : {', '.join(st['resolved'])}")
    if st["unresolved"]:
        held = st.get("held") or []
        print(f"не резолвятся     : {', '.join(st['unresolved'])}"
              + (f" — держу прошлый адрес: {', '.join(held)}" if held else " — прошлого адреса нет"))
    print(f"сервер снаружи    : {st['server'] or '— (адрес не определён)'}")
    for name, port_ in sorted(st["table_ports"].items()):
        print(f"таблица, {name:<9}: порт {port_}")
    if st["omv_rules"]:
        print(f"правила OMV       : {st['omv_rules']} (Сеть → Файервол) — действуют рядом")
    if st["ufw"]:
        print("ufw               : активен — второй владелец правил")
    return 0


def cmd_port(args) -> int:
    from awgbot.domain.gwssh import SshOwnerRefusal
    from awgbot.domain.services import ServiceError
    if not args or not args[0].isdigit():
        print("укажи порт: awg-bot ssh port <1–65535>"); return 2
    try:
        old = _svc().ssh_port_change(int(args[0]))
    except SshOwnerRefusal as e:
        where = e.owner.where or ("файл: " + ", ".join(e.owner.files))
        print(f"[ОТКАЗ] sshd_config принадлежит {'OMV' if e.owner.kind == 'omv' else 'другому процессу'}: "
              f"порт меняется там — {where}. Иначе настройка проживёт до первой генерации файла; "
              "бот увидит новый порт сам и переведёт на него фильтр.")
        return 1
    except ServiceError as e:
        print(f"[ОШИБКА] {e}"); return 1
    print(f"✓ порт SSH: {old} → {args[0]}; проверь вход новым подключением, поправь проброс на роутере")
    return 0


def cmd_allow(args) -> int:
    from awgbot.domain.services import ServiceError
    try:
        cur = _svc().ssh_allow_add(" ".join(args))
    except ServiceError as e:
        print(f"[ОШИБКА] {e}"); return 1
    print(f"адреса снаружи: {', '.join(cur)}")
    return 0


def cmd_deny(args) -> int:
    svc = _svc()
    cur = svc.ssh_screen()["allow"]
    gone = [a for a in args if a in cur]
    if not gone:
        print(f"нечего убирать; сейчас: {', '.join(cur) or '—'}"); return 1
    for a in gone:
        cur = svc.ssh_allow_remove(a)
    print(f"адреса снаружи: {', '.join(cur) or '—'}")
    return 0


def cmd_on(_args) -> int:
    from awgbot.domain.services import ServiceError
    try:
        _svc().ssh_filter_on()
    except ServiceError as e:
        print(f"[ОШИБКА] {e}"); return 1
    print("✓ фильтр снаружи включён: локальная сеть, сервер и адреса из списка; проверь вход новым подключением снаружи")
    return 0


def cmd_off(_args) -> int:
    from awgbot.domain.services import ServiceError
    try:
        _svc().ssh_filter_off()
    except ServiceError as e:
        print(f"[ОШИБКА] {e}"); return 1
    print("✓ фильтр снаружи снят: SSH открыт всем")
    return 0


COMMANDS = {"status": cmd_status, "port": cmd_port, "allow": cmd_allow, "deny": cmd_deny,
            "on": cmd_on, "off": cmd_off}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if config.ROLE != "gateway":
        print("awg-bot ssh — команда агента шлюза; на сервере порт и адреса — в разделе "
              "«Доступ по SSH» бота или awg-bot firewall")
        return 2
    if not argv or argv[0] not in COMMANDS:
        print(__doc__.strip())
        return 2
    try:
        return COMMANDS[argv[0]](argv[1:])
    except Exception as e:                                # noqa: BLE001
        print(f"[ОШИБКА] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
