"""
tools/firewall_gw.py — `awg-bot firewall …` на шлюзе (роль gateway): таблица
awg_gw_guard под скриптом обвязки. Вход — tools/firewall.main по роли.
"""
from __future__ import annotations

import subprocess

from awgbot.core import config
from tools.firewall import _parse_entries


# ── роль gateway: таблица awg_gw_guard под скриптом обвязки ──────────────────
# Здесь бот таблицу не пишет: её ставит routing-gw-setup.sh из бандла (юнит
# awg-link-gw). Локально можно только добавить/убрать адреса SSH через туннель
# (SSH_ALLOW_EXTRA) и перевыставить таблицу.

GW_DOC = """awg-bot firewall (шлюз) — таблица awg_gw_guard ставится бандлом с ВПС.

  status                 таблица, устройства админа (полный доступ с туннеля), политика FORWARD
  allow <ip|cidr> …      доверенные адреса сверх бандла (доступ к шлюзу и локальной сети) и применить
  deny  <ip|cidr> …      убрать из локальных добавок и применить
  apply                  перевыставить таблицу (systemctl restart awg-link-gw)

Порт SSH, адреса снаружи и фильтр снаружи — `awg-bot ssh …`.
"""


def gw_status(_args) -> int:
    from awgbot.infra import gwguard
    info = gwguard.table_info()
    print(f"таблица {gwguard.TABLE}: {'есть' if info else 'НЕТ — обвязка снята или не развёрнута: «🚑 Восстановить» в чате бота шлюза'}")
    if info:
        sets = info["sets"]
        print(f"  подсети туннеля  : {', '.join(sorted(sets.get('tunnel_nets4', ()))) or '—'}")
        print(f"  доступ с туннеля : ВПС по линку (SSH) + полный: {', '.join(sorted(sets.get('admin4', ()))) or '—'}")
        print(f"  диапазоны Telegram: {len(sets.get('tg_nets4', ()))}")
        missing = [c for c in gwguard.CHAINS if c not in info['chains']]
        print(f"  цепочки          : {'все' if not missing else 'нет ' + ', '.join(missing)}")
    print(f"из бандла (ADMIN_IPS): {', '.join(gwguard.unit_admin_ips()) or '—'}")
    print(f"локально (ADMIN_IPS_EXTRA): {', '.join(gwguard.read_extra()) or '—'}")
    pol = gwguard.iptables_forward_policy()
    note = ""
    if pol not in (None, "accept"):
        # наши ACCEPT для линка в той же цепочке открывают транзит — как в
        # проверке монитора агента
        acc = gwguard.forward_accepts()
        note = ("  (транзит линка открыт нашими ACCEPT)"
                if {f"i:{config.GW_LINK_IF}", f"o:{config.GW_LINK_IF}"} <= acc
                else "  ← drop перекроет транзит клиентов")
    print(f"политика ip filter FORWARD: {pol or 'цепочки нет'}{note}")
    rc = subprocess.run(["systemctl", "is-enabled", config.GW_UNIT], capture_output=True).returncode
    print(f"юнит {config.GW_UNIT}: {'включён' if rc == 0 else 'НЕ включён'}")
    env = gwguard.read_env()
    print(f"SSH снаружи: порт {env.get('SSH_PORT') or '22 (факт ещё не снят)'}, фильтр "
          f"{'включён' if env.get('SSH_FILTER') == '1' else 'выключен'} — подробнее: awg-bot ssh status")
    return 0


def _gw_edit(add: list[str], remove: list[str]) -> int:
    from awgbot.infra import gwguard
    cur = gwguard.read_extra()
    for v in add:
        if v not in cur:
            cur.append(v)
    cur = [v for v in cur if v not in remove]
    # admin4 — flags interval: пересечение nft отвергнет, а после ребута — всю
    # таблицу. Схлопываем: с устройствами админа из бандла (их менять нельзя —
    # покрывающую подсеть не добавляем) и между собой.
    bundle = gwguard.unit_admin_ips()
    clash = [v for v in cur if gwguard.overlaps(v, bundle)]
    if clash:
        print(f"[ОШИБКА] {', '.join(clash)} пересекается с устройствами админа из бандла "
              f"({', '.join(bundle)}) — их не объединить, они приезжают с сервера")
        return 1
    merged = gwguard.collapse(cur)
    gone = [v for v in cur if v not in merged]
    cur = merged
    if gone:
        print(f"объединено с покрывающей подсетью: {', '.join(gone)}")
    try:
        gwguard.write_extra(cur)
    except ValueError as e:
        print(f"[ОШИБКА] {e}"); return 1
    print(f"локальные добавки: {', '.join(cur) or '—'}")
    ok, err = gwguard.reassert()
    print("✓ таблица перевыставлена" if ok else f"[ОШИБКА] реассерт: {err}")
    return 0 if ok else 1


def gw_allow(args) -> int:
    try:
        entries = _parse_entries(" ".join(args))
    except ValueError as e:
        print(f"[ОШИБКА] {e}"); return 1
    if not entries or any(not e[0].isdigit() for e in entries):
        print("укажите IP или CIDR (имена на шлюзе не резолвятся)"); return 1
    return _gw_edit(entries, [])


def gw_deny(args) -> int:
    try:
        entries = _parse_entries(" ".join(args))
    except ValueError as e:
        print(f"[ОШИБКА] {e}"); return 1
    return _gw_edit([], entries)


def gw_apply(_args) -> int:
    from awgbot.infra import gwguard
    ok, err = gwguard.reassert()
    print("✓ таблица перевыставлена" if ok else f"[ОШИБКА] реассерт: {err}")
    return 0 if ok else 1


GW_COMMANDS = {"status": gw_status, "allow": gw_allow, "deny": gw_deny, "apply": gw_apply}
