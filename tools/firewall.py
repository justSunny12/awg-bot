"""
tools/firewall.py — `awg-bot firewall …`: файервол хоста в одной точке.

  status                     что включено, что живёт в ядре, есть ли расхождения
  setup                      мастер: вайтлист SSH, прочие порты, применение с
                             таймером отката, потом `confirm`
  confirm [--disable-ufw]    вход проверен: снять таймер отката (и ufw, если просили)
  apply [--no-rollback]      пересобрать таблицу из conf (после правки руками)
  allow <ip|cidr|имя> …      добавить в вайтлист SSH и применить
  deny  <ip|cidr|имя> …      убрать из вайтлиста и применить (с таймером отката)
  off                        снять таблицу, enabled: false (SSH откроется всем)
  rollback                   то же; его дёргает таймер отката

Таймер отката — страховка от самозапирания: если после `setup`/`apply`/`deny`
вход новым подключением не получился и `confirm` не прозвучал, через
ROLLBACK_SECONDS таблица снимается, а firewall.enabled сбрасывается, чтобы бот
не вернул её следующим тиком.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from awgbot.core import config, settings
from awgbot.infra import nftguard

ROOT = Path(__file__).resolve().parents[1]


def _admin_ips() -> list[str]:
    try:
        from awgbot.infra.db import Database
        if not Path(config.DB_PATH).exists():
            return []
        db = Database(config.DB_PATH)
        try:
            return db.admin_device_addresses(config.ADMIN_ID)
        finally:
            db.close()
    except Exception as e:                            # noqa: BLE001
        print(f"[!] адреса устройств админа не прочитаны ({e}) — set будет пуст до старта бота")
        return []


def _parse_entries(raw: str) -> list[str]:
    out: list[str] = []
    for tok in raw.replace(",", " ").split():
        kind, val = nftguard.classify(tok)           # ValueError → наружу
        if val not in out:
            out.append(val)
    return out


def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        v = ""
    return v or default


def _yes(prompt: str) -> bool:
    return _ask(f"{prompt} [y/N]").lower() == "y"


def _print_spec(spec: nftguard.GuardSpec) -> None:
    mode = "host (пер-пирный SSH из туннеля)" if spec.per_peer else "docker (SSH из туннеля по bridge-подсети)"
    print(f"  режим            : {mode}")
    print(f"  порт SSH         : {spec.ssh_port}")
    print(f"  вайтлист v4      : {', '.join(spec.ssh_allow4) or '—'}")
    print(f"  вайтлист v6      : {', '.join(spec.ssh_allow6) or '—'}")
    if spec.unresolved:
        print(f"  НЕ резолвятся    : {', '.join(spec.unresolved)}")
    print(f"  SSH открыт всем  : {'ДА' if spec.ssh_open else 'нет'}")
    print(f"  подсети туннеля  : {', '.join(spec.tunnel_nets4) or '—'}")
    print(f"  устройства админа: {', '.join(spec.tunnel_admin4) or '—'}")
    print(f"  порты awg (udp)  : {', '.join(map(str, spec.udp_ports)) or 'НЕ НАЙДЕНЫ'}")
    print(f"  прочие tcp/udp   : {spec.open_tcp or '—'} / {spec.open_udp or '—'}")


def _arm(seconds: int = nftguard.ROLLBACK_SECONDS) -> None:
    env = {k: v for k, v in os.environ.items()
           if k in ("AWG_BOT_CONF_DIR", "AWG_BOT_DATA_DIR", "AWG_BOT_ENV")}
    cmd = ["-p", f"WorkingDirectory={ROOT}", sys.executable, "-m", "tools.firewall", "rollback"]
    nftguard.arm_rollback(seconds, cmd, env)
    print(f"\n⏱ Таймер отката: {seconds} с. Проверь вход НОВЫМ подключением и выполни\n"
          f"   awg-bot firewall confirm\n"
          f"Иначе таблица снимется сама и firewall.enabled вернётся в false.")


def _apply(with_rollback: bool) -> None:
    spec = nftguard.build_spec(_admin_ips())
    text = nftguard.render(spec)
    for line in nftguard.ensure_persistence():
        print(f"  {line}")
    nftguard.apply_text(text)
    print(f"✓ таблица {nftguard.TABLE} применена, файл {nftguard.RULES_FILE}")
    if with_rollback:
        _arm()


# ── команды ──────────────────────────────────────────────────────────────────

def cmd_status(_args) -> int:
    st = nftguard.status(_admin_ips())
    print(f"firewall.enabled : {'да' if st['enabled'] else 'НЕТ (бот таблицу не ведёт)'}")
    print(f"таблица в ядре   : {'есть' if st['present'] else 'НЕТ'}")
    print(f"файл = желаемому : {'да' if st['file'] else 'НЕТ (apply пересоберёт)'}")
    if st["present"]:
        want = set(st["spec"].tunnel_admin4)
        live = st["live_admin"] or set()
        mark = "совпадает" if want == live else f"РАСХОЖДЕНИЕ (ядро: {', '.join(sorted(live)) or '—'})"
        print(f"set устройств    : {mark}")
    print(f"таймер отката    : {'АКТИВЕН — нужен confirm' if st['rollback'] else 'нет'}")
    print(f"ufw              : {'АКТИВЕН (второй владелец правил)' if st['ufw'] else 'выключен/нет'}")
    print("желаемое:")
    _print_spec(st["spec"])
    return 0


def cmd_setup(_args) -> int:
    print("═══ Файервол хоста: единственная точка — таблица awg_bot_guard ═══\n")
    cur = settings.get("app.firewall.ssh_allow", []) or []
    print("ВАШИ адреса для SSH (IP, CIDR или имя DynDNS; через запятую/пробел).")
    print("Из туннеля SSH открыт устройствам админа всегда — этот список про вход")
    print("СНАРУЖИ. Не сводите его к туннелю: упавший awg оставит без входа.")
    print(f"Сейчас: {', '.join(cur) or '— (SSH открыт всем)'}")
    raw = _ask("Вайтлист (Enter — оставить; '-' — очистить)")
    if raw == "-":
        allow: list[str] = []
    elif raw:
        try:
            allow = _parse_entries(raw)
        except ValueError as e:
            print(f"[ОШИБКА] {e}")
            return 1
    else:
        allow = list(cur)
    if not allow and not _yes("Вайтлист пуст: SSH останется ОТКРЫТ ДЛЯ ВСЕХ (только ключи). Так и оставить?"):
        return 1

    def ports(key: str) -> list[int]:
        curp = settings.get(key, []) or []
        raw = _ask(f"{key.split('.')[-1]} — прочие порты хоста (Enter — {curp or '—'}; '-' — очистить)")
        if raw == "-":
            return []
        if not raw:
            return [int(p) for p in curp]
        try:
            return [int(p) for p in raw.replace(",", " ").split()]
        except ValueError:
            print("[ОШИБКА] порты — числа"); raise
    try:
        open_tcp = ports("app.firewall.open_tcp")
        open_udp = ports("app.firewall.open_udp")
    except ValueError:
        return 1

    settings.set_value("app.firewall.ssh_allow", allow)
    settings.set_value("app.firewall.open_tcp", open_tcp)
    settings.set_value("app.firewall.open_udp", open_udp)

    spec = nftguard.build_spec(_admin_ips())
    print("\nБудет применено:")
    _print_spec(spec)
    print("\n" + nftguard.render(spec))
    if spec.unresolved:
        print(f"[!] не резолвятся: {', '.join(spec.unresolved)} — в таблицу не попадут")
    if not spec.udp_ports and spec.per_peer:
        print("[!] ListenPort не найден ни в одном conf — порт клиентов awg НЕ будет открыт")
        if not _yes("Продолжить?"):
            return 1
    if nftguard.ufw_active():
        print("[i] ufw активен: до `confirm --disable-ufw` он остаётся вторым слоем;")
        print("    пакет должен пройти оба — это временно и безопасно.")
    if not _yes("Применить с таймером отката?"):
        return 1
    settings.set_value("app.firewall.enabled", True)
    _apply(with_rollback=True)
    return 0


def cmd_confirm(args) -> int:
    was = nftguard.disarm_rollback()
    print("✓ таймер отката снят" if was else "таймера отката не было")
    if nftguard.ufw_active():
        if "--disable-ufw" in args or _yes("ufw активен — выключить (таблица awg_bot_guard уже держит всё)?"):
            rc = subprocess.run(["ufw", "disable"], capture_output=True)
            print("✓ ufw выключен" if rc.returncode == 0 else
                  f"[!] ufw disable: {rc.stderr.decode(errors='replace').strip()}")
        else:
            print("[i] ufw оставлен — два владельца правил; выключить позже: ufw disable")
    return cmd_status(args)


def cmd_apply(args) -> int:
    if not nftguard.enabled():
        print("firewall.enabled=false — сначала `awg-bot firewall setup`")
        return 1
    _apply(with_rollback="--no-rollback" not in args)
    return 0


def _edit_allow(add: list[str], remove: list[str]) -> list[str]:
    cur = list(settings.get("app.firewall.ssh_allow", []) or [])
    for v in add:
        if v not in cur:
            cur.append(v)
    cur = [v for v in cur if v not in remove]
    settings.set_value("app.firewall.ssh_allow", cur)
    return cur


def cmd_allow(args) -> int:
    try:
        entries = _parse_entries(" ".join(args))
    except ValueError as e:
        print(f"[ОШИБКА] {e}"); return 1
    if not entries:
        print("укажите адреса"); return 1
    cur = _edit_allow(entries, [])
    print(f"вайтлист: {', '.join(cur)}")
    if nftguard.enabled():
        _apply(with_rollback=False)          # добавление запереть не может
    else:
        print("firewall.enabled=false — записано в conf, применится после `setup`")
    return 0


def cmd_deny(args) -> int:
    try:
        entries = _parse_entries(" ".join(args))
    except ValueError as e:
        print(f"[ОШИБКА] {e}"); return 1
    cur = _edit_allow([], entries)
    print(f"вайтлист: {', '.join(cur) or '— (SSH открыт всем)'}")
    if nftguard.enabled():
        _apply(with_rollback=True)           # удаление может запереть — с откатом
    return 0


def cmd_off(_args) -> int:
    nftguard.disarm_rollback()
    for line in nftguard.remove():
        print(f"  {line}")
    settings.set_value("app.firewall.enabled", False)
    print("firewall.enabled: false — SSH открыт всем адресам (доступ не потерян).")
    print("Старые слои (ufw и т.п.) не возвращаются сами.")
    return 0


def cmd_rollback(_args) -> int:
    for line in nftguard.remove():
        print(f"  {line}")
    settings.set_value("app.firewall.enabled", False)
    print("ОТКАТ: таблица снята, firewall.enabled: false")
    return 0


COMMANDS = {
    "status": cmd_status, "setup": cmd_setup, "confirm": cmd_confirm,
    "apply": cmd_apply, "allow": cmd_allow, "deny": cmd_deny,
    "off": cmd_off, "rollback": cmd_rollback,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print(__doc__.strip())
        return 2
    settings.init(config.CONF_DIR)
    try:
        return COMMANDS[argv[0]](argv[1:])
    except nftguard.GuardError as e:
        print(f"[ОШИБКА] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
