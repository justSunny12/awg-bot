"""
gwssh.py — доступ по SSH на шлюзе (раздел «🛡 Доступ по SSH» агента,
`awg-bot ssh …`; концепт «доступ по SSH на шлюзе»).

Три вещи, которых нет у основного бота:
  • порт — ФАКТ, а не настройка: sshd_config на малине может принадлежать
    OMV, и порт задаёт он. Агент читает слушающие сокеты sshd каждый тик и
    держит таблицу awg_gw_guard на этом порту (SSH_PORT в firewall.env →
    реассерт), уведомляя один раз на значение;
  • чужой владелец sshd_config (OMV, иной генератор) — смена порта из чата
    отказана с указанием, где менять: иначе настройка проживёт до первого
    «Применить» в OMV, а проброс на роутере человек уже перевёл;
  • фильтр снаружи (цепочка ssh_in) — только для порта SSH и только для
    не-туннельных источников: локальная сеть и сервер открыты всегда, поэтому
    запереться нельзя и таймер отката не нужен. Имена DynDNS резолвит агент
    и правит наборы ssh_allow4 / server4 напрямую (set_sync).
"""
from __future__ import annotations

import logging
import time

from awgbot.core import config
from awgbot.domain.services import Notification, ServiceError

log = logging.getLogger("awgbot.gateway")


class SshOwnerRefusal(ServiceError):
    """Смена порта отказана: конфигом sshd владеет не бот. Экран рисует
    отказ по полям, а не по тексту исключения."""

    def __init__(self, owner, listening: int | None):
        super().__init__("sshd_config принадлежит " + (owner.kind or "?"))
        self.owner = owner
        self.listening = listening


def _no32(cidr: str) -> str:
    """Одиночный адрес — без маски: resolve_allow отдаёт /32, как CIDR."""
    return cidr[:-3] if cidr.endswith("/32") else cidr


class GwSshMixin:
    _SSH_PORT_SEEN_KEY = "gw_ssh_port_seen"
    # Владелец конфига, правила OMV и ufw меняются руками и редко — не exec на
    # каждый тик (omv-confdbadm нескорый). Смена порта владельца читает живьём.
    _SSH_STATIC_TTL = 10 * 60

    def _ssh_static(self) -> tuple:
        from awgbot.infra import nftguard, sshd
        c = self.__dict__.get("_ssh_static_cache")
        if c and time.monotonic() - c[0] < self._SSH_STATIC_TTL:
            return c[1], c[2], c[3]
        owner = sshd.owner()
        rules = sshd.omv_firewall_rules() if owner.kind == "omv" else 0
        ufw = nftguard.ufw_active()
        self.__dict__["_ssh_static_cache"] = (time.monotonic(), owner, rules, ufw)
        return owner, rules, ufw

    def invalidate_ssh_static(self) -> None:
        self.__dict__.pop("_ssh_static_cache", None)

    # ── чтение ───────────────────────────────────────────────────────────────

    @staticmethod
    def _ssh_allow_split(env: dict) -> list[str]:
        return [t for t in env.get("SSH_ALLOW", "").split() if t]

    def ssh_port_fact(self) -> tuple[int | None, list[int]]:
        """(порт, на котором слушает sshd | None — не запущен, все его порты)."""
        from awgbot.infra import sshd
        try:
            ports = sshd.listening_ports()
        except sshd.SshdError as e:
            log.warning("gateway: порт sshd не прочитан: %s", e)
            return None, []
        return (ports[0] if ports else None), ports

    def ssh_screen(self, info: dict | None = None) -> dict:
        """Всё для раздела и панели одним снимком. info — уже снятый
        table_info (тик отдаёт свой, чтобы не ходить в nft дважды)."""
        from awgbot.infra import gwguard, nftguard, sshd
        env = gwguard.read_env()
        port, ports = self.ssh_port_fact()
        env_port = int(env["SSH_PORT"]) if env.get("SSH_PORT", "").isdigit() else None
        try:
            conf_ports = sshd.effective_ports()
        except sshd.SshdError:
            conf_ports = []
        owner, omv_rules, ufw = self._ssh_static()
        allow = self._ssh_allow_split(env)
        v4, _v6, bad = nftguard.resolve_allow(allow)
        v4 = [_no32(x) for x in v4]
        if info is None:
            try:
                info = gwguard.table_info()
            except gwguard.GwGuardError:
                info = None
        chains = (info or {}).get("chains", set())
        server = gwguard.server_host()
        return {
            "port": port if port is not None else (env_port or 22),
            "sshd_down": port is None,
            "ports": ports,                       # все порты sshd; >1 — предупреждение
            "env_port": env_port,
            "conf_ports": conf_ports,             # sshd -T: «в конфиге 2222, слушает 22»
            "owner": owner.kind, "owner_where": owner.where, "owner_port": owner.port,
            "owner_detail": owner.detail, "owner_files": list(owner.files),
            "filter": env.get("SSH_FILTER") == "1",
            "allow": allow,
            "resolved": v4,
            "unresolved": bad,
            "admin_ips": gwguard.unit_admin_ips() + gwguard.read_extra(),
            "server": server,
            "new_plumbing": bool(info) and "ssh_in" in chains,
            "table_ports": dict((info or {}).get("ssh_ports", {})),
            "ufw": ufw,
            "omv_rules": omv_rules,
        }

    # ── порт ─────────────────────────────────────────────────────────────────

    def ssh_port_busy(self, port: int) -> str:
        from awgbot.infra import sshd
        if not sshd.valid_port(port):
            raise ServiceError("порт — число от 1 до 65535")
        try:
            return sshd.port_busy(int(port))
        except sshd.SshdError as e:
            raise ServiceError(str(e))

    def ssh_port_change(self, port: int) -> int:
        """Перевести таблицу и sshd на порт; владелец конфига — бот. Порядок как
        на ВПС: сначала таблица (реассерт), потом sshd — к рестарту порт уже
        открыт из туннеля и локальной сети. Возвращает прежний порт."""
        from awgbot.infra import gwguard, sshd
        port = int(port)
        if not sshd.valid_port(port):
            raise ServiceError("порт — число от 1 до 65535")
        old, _ = self.ssh_port_fact()
        owner = sshd.owner()                              # живьём, не из кэша
        self.invalidate_ssh_static()
        if owner:
            raise SshOwnerRefusal(owner, old)
        if old is None:
            raise ServiceError("sshd не запущен — менять порт нечему")
        if port == old:
            raise ServiceError(f"порт {port} уже выбран текущий")
        busy = self.ssh_port_busy(port)
        if busy:
            raise ServiceError(f"порт {port} уже занят процессом {busy}")
        gwguard.write_env(SSH_PORT=str(port))
        ok, err = gwguard.reassert()
        if not ok:
            gwguard.write_env(SSH_PORT=str(old))
            raise ServiceError(f"таблица не перевыставлена: {err}")
        try:
            for line in sshd.set_port(port):
                log.info("sshd: %s", line)
        except sshd.SshdError as e:
            gwguard.write_env(SSH_PORT=str(old))
            gwguard.reassert()
            raise ServiceError(str(e))
        self.db.set_state(self._SSH_PORT_SEEN_KEY, str(port))
        return old

    # ── адреса снаружи и фильтр ──────────────────────────────────────────────

    def ssh_allow_add(self, raw: str) -> list[str]:
        """Добавить адреса снаружи: IP, подсеть или имя, через пробел/запятую.
        Только IPv4 — таблица v4, за роутером квартиры v6-проброса нет."""
        from awgbot.infra import gwguard, nftguard
        entries: list[str] = []
        for tok in str(raw).replace(",", " ").split():
            try:
                kind, norm = nftguard.classify(tok)
            except ValueError as e:
                raise ServiceError(f"«{tok}» не адрес, не подсеть и не имя: {e}")
            if kind == "6":
                raise ServiceError(f"{tok} — IPv6, на шлюзе фильтр только по IPv4")
            if norm.endswith("/32"):
                norm = norm[:-3]                          # одиночный адрес — без маски
            if norm not in entries:
                entries.append(norm)
        if not entries:
            raise ServiceError("пусто: жду адрес, подсеть или имя")
        cur = self._ssh_allow_split(gwguard.read_env())
        cur += [e for e in entries if e not in cur]
        gwguard.write_env(SSH_ALLOW=" ".join(cur))
        self._ssh_sync_sets()
        return cur

    def ssh_allow_remove(self, entry: str) -> list[str]:
        from awgbot.infra import gwguard
        cur = [v for v in self._ssh_allow_split(gwguard.read_env()) if v != entry]
        gwguard.write_env(SSH_ALLOW=" ".join(cur))
        self._ssh_sync_sets()
        return cur

    def ssh_filter_on(self) -> None:
        """Переход на ssh_in рендерит скрипт — включение через реассерт. На
        обвязке старого образца включать нечего: цепочки нет."""
        from awgbot.infra import gwguard
        try:
            info = gwguard.table_info()
        except gwguard.GwGuardError as e:
            raise ServiceError(str(e))
        if not info or "ssh_in" not in info["chains"]:
            raise ServiceError("обвязка шлюза старого образца: перевыпусти конфигурацию "
                               "шлюза с сервера и примени её здесь")
        gwguard.write_env(SSH_FILTER="1")
        ok, err = gwguard.reassert()
        if not ok:
            gwguard.write_env(SSH_FILTER="0")
            raise ServiceError(f"таблица не перевыставлена: {err}")
        self._ssh_sync_sets()

    def ssh_filter_off(self) -> None:
        from awgbot.infra import gwguard
        gwguard.write_env(SSH_FILTER="0")
        ok, err = gwguard.reassert()
        if not ok:
            raise ServiceError(f"таблица не перевыставлена: {err}")

    def _ssh_sync_sets(self, info: dict | None = None) -> dict:
        """Наборы с динамикой: ssh_allow4 (IP + резолв имён), server4 (хост
        сервера). Резолв меняется — запомнить в firewall.env для следующей
        загрузки (скрипт кладёт SSH_ALLOW_RESOLVED в набор до первого тика).
        Возвращает {'allow': bool, 'server': bool, 'unresolved': [...]}."""
        from awgbot.infra import gwguard, nftguard
        env = gwguard.read_env()
        allow = self._ssh_allow_split(env)
        v4 = [_no32(x) for x in nftguard.resolve_allow(allow)[0]]
        bad = nftguard.resolve_allow(allow)[2]
        names = [a for a in allow if not a[0].isdigit()]
        resolved = sorted({_no32(x) for x in nftguard.resolve_allow(names)[0]}) if names else []
        out = {"allow": False, "server": False, "unresolved": bad}
        if info is None:
            try:
                info = gwguard.table_info()
            except gwguard.GwGuardError:
                info = None
        if " ".join(resolved) != env.get("SSH_ALLOW_RESOLVED", ""):
            gwguard.write_env(SSH_ALLOW_RESOLVED=" ".join(resolved))
        try:
            out["allow"] = gwguard.set_sync("ssh_allow4", set(v4), info)
            host = gwguard.server_host()
            server_ips: set[str] = set()
            if host:
                try:
                    kind, norm = nftguard.classify(host)
                except ValueError:
                    kind, norm = "", ""
                if kind == "4":
                    server_ips = {_no32(norm)}
                elif kind == "host":
                    server_ips = {_no32(x) for x in nftguard.resolve_allow([norm])[0]}
            out["server"] = gwguard.set_sync("server4", server_ips, info)
        except gwguard.GwGuardError as e:
            log.warning("gateway: наборы SSH не приведены: %s", e)
        return out

    # ── тик ──────────────────────────────────────────────────────────────────

    def ssh_reconcile(self, info: dict | None = None) -> list[Notification]:
        """Порт как факт: sshd слушает не то, что в firewall.env → записать,
        перевыставить таблицу сразу (одно событие, не стрик), уведомить один
        раз на значение. Затем — наборы с динамикой."""
        from awgbot.infra import gwguard, sshd
        notes: list[Notification] = []
        env = gwguard.read_env()
        port, _ports = self.ssh_port_fact()
        if port is not None:
            cur = env.get("SSH_PORT", "")
            if str(port) != cur:
                try:
                    gwguard.write_env(SSH_PORT=str(port))
                except (OSError, ValueError) as e:
                    log.warning("gateway: SSH_PORT не записан: %s", e)
                else:
                    # Пустой файл и порт 22 — первый тик после обновления на
                    # машине «как сегодня»: таблица и так на 22, реассерт лишний.
                    if cur or port != 22:
                        ok, err = gwguard.reassert()
                        if not ok:
                            log.warning("gateway: реассерт после смены порта sshd: %s", err)
                        info = None                     # таблица переставлена — снимок устарел
                    seen = self.db.get_state(self._SSH_PORT_SEEN_KEY) or ""
                    if cur and seen != str(port):
                        self.invalidate_ssh_static()      # порт сменил, возможно, новый владелец
                        owner = sshd.owner()
                        who = (f" (задал {'OMV' if owner.kind == 'omv' else 'другая программа'})"
                               if owner else " — sshd_config правили мимо бота")
                        notes.append(Notification(
                            config.ADMIN_ID,
                            f"🛡 Порт SSH на шлюзе изменился: {cur} → {port}{who}. Фильтр "
                            "переведён на новый порт: из туннеля, из локальной сети и снаружи. "
                            "Если ходишь снаружи — поправь проброс порта на роутере.",
                            critical=False))
                    self.db.set_state(self._SSH_PORT_SEEN_KEY, str(port))
        try:
            self._ssh_sync_sets(info)
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway: ssh sets: %s", e)
        return notes

    def ssh_checks(self, info: dict | None) -> list:
        """Проверки монитора: таблица держит порт sshd; фильтр снаружи (если
        включён) действительно стоит. ok=None — sshd не запущен."""
        from awgbot.domain.gateway import GwCheck
        from awgbot.infra import gwguard
        checks = []
        port, _ = self.ssh_port_fact()
        table_ports = (info or {}).get("ssh_ports", {})
        held = table_ports.get("tunnel_in")
        if port is None:
            checks.append(GwCheck("порт SSH", None, "sshd не запущен — доступ только из консоли"))
        elif info is None:
            pass                                           # таблицы нет — своя проверка выше
        else:
            ok = held == port
            checks.append(GwCheck("порт SSH", ok, "" if ok else
                                  f"sshd слушает {port}, таблица держит {held or '?'}: "
                                  "реассерт не прошёл"))
        if gwguard.read_env().get("SSH_FILTER") == "1" and info is not None:
            if "ssh_in" not in info["chains"]:
                checks.append(GwCheck("фильтр SSH снаружи", False,
                                      "включён, а цепочки ssh_in в таблице нет: перевыпусти "
                                      "конфигурацию шлюза с сервера и примени её здесь"))
            else:
                ok = table_ports.get("input") == (port if port is not None else held)
                checks.append(GwCheck("фильтр SSH снаружи", ok, "" if ok else
                                      "включён, а перехода на цепочку ssh_in в таблице нет"))
        return checks
