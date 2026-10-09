"""link.py — линк, обвязка, ядро и версии — что агент видит на хосте."""

from __future__ import annotations

import glob
import os
import re
import time
from awgbot.core import config
from awgbot.util import timeutil
from awgbot.domain.gateway import base
from awgbot.domain.gateway.base import GwCheck, log


class LinkMixin:
    """Линк, обвязка, ядро и версии — что агент видит на хосте."""
    # ── линк ─────────────────────────────────────────────────────────────────

    def link_status(self) -> tuple[bool, float | None, int, int]:
        """(интерфейс поднят, возраст хендшейка в сек | None, rx, tx) — одним
        `awg show <if> dump`: хендшейк и счётчики в нём же, два вызова были лишними."""
        up = base._run(["ip", "link", "show", config.GW_LINK_IF]).returncode == 0
        if not up:
            return False, None, 0, 0
        age = None
        rx = tx = 0
        try:
            from awgbot.infra.awg import parse_dump
            peers = parse_dump(base._out(base._run(["awg", "show", config.GW_LINK_IF, "dump"])))
            ts = max((int(p["last_handshake"] or 0) for p in peers), default=0)
            if ts:
                age = max(0.0, timeutil.now().timestamp() - ts)
            rx = sum(int(p.get("rx") or 0) for p in peers)
            tx = sum(int(p.get("tx") or 0) for p in peers)
        except Exception as e:                          # noqa: BLE001
            log.warning("gateway: awg show dump: %s", e)
        return True, age, rx, tx


    # ── обвязка ──────────────────────────────────────────────────────────────

    def plumbing_checks(self) -> list[GwCheck]:
        checks: list[GwCheck] = []
        try:
            fwd = base.pathlib_read("/proc/sys/net/ipv4/ip_forward").strip() == "1"
            checks.append(GwCheck("ip_forward", fwd,
                                  "" if fwd else "выключен — транзита клиентов нет"))
        except Exception:                                # noqa: BLE001
            checks.append(GwCheck("ip_forward", None, "не прочитался"))

        # Вся обвязка — одна nft-таблица (gwguard): MASQUERADE, изоляция,
        # метки Telegram, защита машины от туннеля. Одним `nft -j list table`.
        from awgbot.infra import gwguard
        try:
            info = gwguard.table_info()
        except gwguard.GwGuardError as e:
            info = None
            checks.append(GwCheck("таблица awg_gw_guard", None, str(e)))
        else:
            if info is None:
                checks.append(GwCheck(
                    "таблица awg_gw_guard", False,
                    "нет — обвязка старого образца или снята; перевыпусти "
                    "конфигурацию шлюза с сервера AWG"))
        self._guard_info = info
        if info is not None:
            nets = info["sets"].get("tunnel_nets4", set())
            client_subnet = gwguard.client_subnet()   # conf агента или юнит обвязки
            if client_subnet:
                ok = client_subnet in nets
                checks.append(GwCheck(
                    "MASQUERADE/изоляция", ok,
                    "" if ok else f"{client_subnet} нет в tunnel_nets4 — конфигурация шлюза "
                    "выпущена под другую подсеть, перевыпусти её с сервера AWG"))
            else:
                checks.append(GwCheck("MASQUERADE/изоляция", None,
                                      "подсеть клиентов неизвестна (нет конфигурации с сервера AWG) — "
                                      "проверка выключена"))
            missing_chains = [c for c in gwguard.CHAINS if c not in info["chains"]]
            checks.append(GwCheck("цепочки таблицы", not missing_chains,
                                  "" if not missing_chains else
                                  "нет: " + ", ".join(missing_chains)))
            pol = gwguard.iptables_forward_policy()
            ok_fwd = pol in (None, "accept")
            if not ok_fwd:
                # docker ставит DROP; раздел 3a скрипта открывает транзит своим
                # интерфейсам в той же цепочке. Есть наши ACCEPT для линка —
                # транзит идёт, и красная проверка была бы ложной тревогой.
                acc = gwguard.forward_accepts()
                ok_fwd = {f"i:{config.GW_LINK_IF}", f"o:{config.GW_LINK_IF}"} <= acc
            checks.append(GwCheck("политика FORWARD", ok_fwd,
                                  "" if ok_fwd else
                                  f"ip filter FORWARD: {pol} — drop чужой таблицы "
                                  "перекрывает транзит клиентов"))
        # Включённость юнита меняется только руками — статический ярус (45
        # мин, кнопки «Статус»/«Монитор здоровья» сбрасывают), а не exec на тик.
        enabled = self._unit_enabled()
        checks.append(GwCheck("юнит реассерта", enabled,
                              "" if enabled else
                              f"{config.GW_UNIT} не включён — ребут не восстановит обвязку"))
        # Политика «Telegram → аплинк»: без неё метка стоит, а пакеты агента
        # уходят домашнему провайдеру — Telegram недоступен, агент молчит.
        # Аплинк (автодетект — три exec) и состояние политики запоминаем на
        # тик: heal возьмёт их отсюда, а не снимет заново.
        uplink = gwguard.uplink_interface()
        pol = gwguard.uplink_policy(uplink) if uplink else None
        self._uplink_state = (uplink, pol)
        if uplink:
            ok = pol["rule"] and pol["route"]
            lack = [n for n, v in (("правило по метке", pol["rule"]),
                                   (f"маршрут в {uplink}", pol["route"])) if not v]
            checks.append(GwCheck("политика аплинка", ok,
                                  "" if ok else "нет: " + ", ".join(lack) + " — агент перевыставит"))
            # Маскарад в аплинк: без него локальный пакет уходит в туннель с
            # адресом домашней сети, и ВПС его отбрасывает — Telegram через ВПС
            # не проходит. На прежнем шлюзе это делало чужое правило домашней
            # схемы, чистая установка без него нема.
            info = self.__dict__.get("_guard_info")
            if info is not None:
                masq = uplink in info.get("masq_ifaces", set())
                checks.append(GwCheck("маскарад в аплинк", masq,
                                      "" if masq else f"нет masquerade в {uplink}: пакеты агента "
                                      "уходят в туннель с локальным адресом — перевыпусти "
                                      "конфигурацию шлюза с сервера AWG"))
        else:
            checks.append(GwCheck("политика аплинка", None, "аплинк не найден"))
        # Маскарад клиентов привязан к интерфейсу маршрута по умолчанию на
        # момент применения. Переехал маршрут (OMV собрал bridge или bond) —
        # правило стоит на прежнем интерфейсе, РФ-доступ у всех молчит, а зонд
        # с адреса устройства проходит. Ловим по факту и перевыставляем обвязку
        # (скрипт берёт интерфейс заново).
        info = self.__dict__.get("_guard_info")
        wan = gwguard.default_route_dev()
        if info is not None and wan and wan not in (uplink, config.GW_LINK_IF):
            masq_ifaces = info.get("masq_ifaces", set())
            ok = not masq_ifaces or wan in masq_ifaces
            self.__dict__["_masq_stale"] = not ok
            checks.append(GwCheck("маскарад клиентов", ok,
                                  "" if ok else f"маршрут по умолчанию через {wan}, а masquerade "
                                  "клиентов стоит в другом интерфейсе — агент перевыставит обвязку"))
        return checks

    def _unit_enabled(self) -> bool:
        c = self.__dict__.get("_unit_enabled_cache")
        if c and time.monotonic() - c[0] < self._STATIC_TTL:
            return c[1]
        enabled = base._run(["systemctl", "is-enabled", config.GW_UNIT]).returncode == 0
        self.__dict__["_unit_enabled_cache"] = (time.monotonic(), enabled)
        return enabled

    def uplink_policy_heal(self) -> list[str]:
        """Перевыставить правило/маршрут аплинка, если пропали (см. gwguard).
        Каждый тик: аплинк и состояние политики — те, что только что снял
        plumbing_checks (дубль автодетекта и двух `ip -j` на тик снят); вне
        тика — свои пробы. Idempotent-команды ip только при пропаже."""
        from awgbot.infra import gwguard
        state = self.__dict__.pop("_uplink_state", None)
        if state is None:
            uplink = gwguard.uplink_interface()
            pol = gwguard.uplink_policy(uplink) if uplink else None
        else:
            uplink, pol = state
        if not uplink:
            return []
        fixed = gwguard.uplink_policy_ensure(uplink, pol)
        if fixed:
            log.warning("gateway: политика аплинка перевыставлена: %s", ", ".join(fixed))
        return fixed


    # ── ядро/версии ──────────────────────────────────────────────────────────

    _KVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(.*)$")

    def kernel_coverage(self, modules_root: str = "/lib/modules",
                        running: str | None = None) -> tuple[list[str], int]:
        """Ядра без модуля amneziawg среди ТЕХ, в которые эта машина может
        загрузиться: тот же вариант, что запущенное (суффикс после версии —
        Raspberry Pi OS кладёт в один пакет ядра всех плат: -v8, -2712, -v7l,
        и чужие здесь не загрузятся никогда), и не старше запущенного (назад
        не откатываемся). Смысл проверки — НОВОЕ ядро из apt до ребута: dkms
        молча пропускает ядро без headers, и ребут в него оставил бы шлюз без
        awg. Возвращает (без модуля, сколько ядер рассмотрено)."""
        running = running or os.uname().release
        m = self._KVER_RE.match(running)
        run_ver = tuple(int(x) for x in m.groups()[:3]) if m else None
        run_flavor = m.group(4) if m else ""
        missing: list[str] = []
        considered = 0
        kernels = sorted(
            d for d in glob.glob(os.path.join(modules_root, "*")) if os.path.isdir(d))
        for kdir in kernels:
            name = os.path.basename(kdir)
            km = self._KVER_RE.match(name)
            if run_ver is not None:
                if not km or km.group(4) != run_flavor:
                    continue                     # ядро другой платы/варианта
                if tuple(int(x) for x in km.groups()[:3]) < run_ver:
                    continue                     # старее запущенного — назад не идём
            considered += 1
            # БЕЗ рекурсии по дереву модулей: `**` обходил тысячи файлов на
            # каждое ядро, и панель на Pi рисовалась секунды. Модуль лежит в
            # известных местах: DKMS — updates/dkms, пакетная сборка — kernel/net.
            found = (glob.glob(os.path.join(kdir, "updates", "dkms", "amneziawg.ko*"))
                     or glob.glob(os.path.join(kdir, "kernel", "net", "amneziawg.ko*"))
                     or glob.glob(os.path.join(kdir, "extra", "amneziawg.ko*")))
            if not found:
                missing.append(name)
        return missing, considered

    def versions(self) -> tuple[str, str]:
        """(version, srcversion) модуля. version у сборок AWG ВРЁТ (тег 0828 нёс
        строку 0812) — различать сборки можно только по srcversion."""
        out = base._out(base._run(["modinfo", "amneziawg"]))
        ver = src = ""
        for line in out.splitlines():
            if line.startswith("version:"):
                ver = line.split(":", 1)[1].strip()
            elif line.startswith("srcversion:"):
                src = line.split(":", 1)[1].strip()
        return ver, src
