"""
gwssh.py — доступ по SSH на шлюзе (раздел «🛡 Доступ по SSH» агента,
`awg-bot ssh …`; концепт «доступ по SSH на шлюзе»).

Три вещи, которых нет у основного бота:
  • порт — ФАКТ, а не настройка: sshd_config на шлюзе может принадлежать
    OMV, и порт задаёт он. Агент читает слушающие сокеты sshd каждый тик и
    держит таблицу awg_gw_guard на этом порту (SSH_PORT в firewall.env →
    реассерт), уведомляя один раз на значение;
  • чужой владелец sshd_config (OMV, иной генератор) — смена порта из чата
    отказана с указанием, где менять: иначе настройка проживёт до первого
    «Применить» в OMV, а проброс на роутере человек уже перевёл;
  • фильтр снаружи (цепочка ssh_in) — только для порта SSH и только для
    не-туннельных источников: локальная сеть и сервер открыты всегда, поэтому
    запереться нельзя и таймер отката не нужен. Имена DynDNS резолвит агент
    и правит наборы ssh_allow4 / server4 / lan4 напрямую (set_sync).

Тик и действия из чата/CLI идут в разных потоках — всё, что пишет файл или
таблицу, под одним замком; факт порта снимается один раз на тик и
передаётся проверкам, экрану и реконсайлу.
"""
from __future__ import annotations

import logging
import threading
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


def _merge_nets(entries: list[str]) -> list[str]:
    """Схлопнуть пересекающиеся адреса/подсети списка, имена оставить как
    есть; порядок — как ввели, поглощённые записи уходят."""
    from awgbot.infra import gwguard
    literal = [e for e in entries if e[0].isdigit()]
    kept = set(gwguard.collapse(literal))
    out: list[str] = []
    for e in entries:
        if not e[0].isdigit():
            out.append(e)
        elif e in kept and e not in out:
            out.append(e)
    # схлопывание могло породить новую подсеть (две смежные /25 → /24)
    for k in gwguard.collapse(literal):
        if k not in out:
            out.append(k)
    return out


class GwSshMixin:
    _SSH_PORT_SEEN_KEY = "gw_ssh_port_seen"
    # Владелец конфига, правила OMV и ufw меняются руками и редко — не exec на
    # каждый тик. Смена порта владельца читает живьём.
    _SSH_STATIC_TTL = 10 * 60
    _SSH_REASSERT_RETRY = 10 * 60          # повтор реассерта, пока таблица не на порту sshd
    _ssh_lock = threading.Lock()
    _ssh_last_reassert = 0.0

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

    def ssh_port_fact(self, env: dict | None = None) -> tuple[int | None, list[int]]:
        """(порт, на котором слушает sshd | None — не запущен, все его порты).
        sshd на нескольких портах — держим тот, что уже в firewall.env, иначе
        меньший: порядок `ss` не контракт, прыгать между тиками нельзя."""
        from awgbot.infra import gwguard, sshd
        try:
            ports = sshd.listening_ports()
        except sshd.SshdError as e:
            log.warning("gateway: порт sshd не прочитан: %s", e)
            return None, []
        if not ports:
            return None, []
        cur = (env if env is not None else gwguard.read_env()).get("SSH_PORT", "")
        if cur.isdigit() and int(cur) in ports:
            return int(cur), ports
        return min(ports), ports

    def _ssh_resolve(self, allow: list[str], env: dict) -> tuple[list[str], list[str], list[str]]:
        """(адреса для набора: IP/CIDR как есть + резолв имён + удержанные,
        нерезолвящиеся имена, удержанные прошлые адреса). DNS молчит — держим
        прошлый адрес: из кэша резолвера, а после рестарта агента — из
        SSH_ALLOW_RESOLVED файла (fail-closed, не пусто)."""
        from awgbot.infra import gwguard, nftguard
        v4, _v6, bad = nftguard.resolve_allow(allow)
        v4 = [_no32(x) for x in v4]
        literal = [a for a in allow if a[0].isdigit()]
        held: list[str] = []
        if bad:
            prev = [t for t in env.get("SSH_ALLOW_RESOLVED", "").split() if t]
            held = [p for p in prev if p not in v4 and not gwguard.overlaps(p, literal)]
        return v4 + held, bad, held

    def ssh_screen(self, info: dict | None = None, fact: tuple | None = None,
                   conf: bool = True) -> dict:
        """Всё для раздела и панели одним снимком. info — уже снятый
        table_info, fact — уже снятый ssh_port_fact (тик отдаёт свои, чтобы
        не ходить в nft и ss дважды); conf — звать ли `sshd -T` (строка «в
        конфиге 2222, слушает 22» нужна только разделу, не панели)."""
        from awgbot.infra import gwguard, sshd
        env = gwguard.read_env()
        port, ports = fact if fact is not None else self.ssh_port_fact(env)
        env_port = int(env["SSH_PORT"]) if env.get("SSH_PORT", "").isdigit() else None
        conf_ports: list[int] = []
        if conf:
            try:
                conf_ports = sshd.effective_ports()
            except sshd.SshdError:
                conf_ports = []
        owner, omv_rules, ufw = self._ssh_static()
        allow = self._ssh_allow_split(env)
        v4, bad, held = self._ssh_resolve(allow, env)
        if info is None:
            try:
                info = gwguard.table_info()
            except gwguard.GwGuardError:
                info = None
        chains = (info or {}).get("chains", set())
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
            "held": held,                         # прошлые адреса нерезолвящихся имён
            "admin_ips": gwguard.unit_admin_ips() + gwguard.read_extra(),
            "server": gwguard.server_host(),
            "lan": sorted((info or {}).get("sets", {}).get("lan4", set())),
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
        with self._ssh_lock:
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
        Только IPv4 — таблица v4, за роутером квартиры v6-проброса нет.
        Пересечения (адрес внутри подсети, подсеть поверх адресов) — nft не
        примет набор, и после ребута обвязки не будет — схлопываются: в списке
        остаётся покрывающая подсеть, вызывающий видит, что объединено."""
        from awgbot.infra import gwguard, nftguard
        entries: list[str] = []
        for tok in str(raw).replace(",", " ").split():
            try:
                kind, norm = nftguard.classify(tok)
            except ValueError:
                raise ServiceError(f"«{tok}» не адрес, не подсеть и не имя")
            if kind == "6":
                raise ServiceError(f"{tok} — IPv6, на шлюзе фильтр только по IPv4")
            norm = _no32(norm)
            if norm not in entries:
                entries.append(norm)
        if not entries:
            raise ServiceError("пусто: жду адрес, подсеть или имя")
        with self._ssh_lock:
            cur = self._ssh_allow_split(gwguard.read_env())
            cur += [e for e in entries if e not in cur]
            cur = _merge_nets(cur)
            gwguard.write_env(SSH_ALLOW=" ".join(cur))
            self._ssh_sync_sets()
        return cur

    def ssh_allow_remove(self, entry: str) -> list[str]:
        from awgbot.infra import gwguard
        with self._ssh_lock:
            cur = [v for v in self._ssh_allow_split(gwguard.read_env()) if v != entry]
            gwguard.write_env(SSH_ALLOW=" ".join(cur))
            self._ssh_sync_sets()
        return cur

    def ssh_filter_on(self) -> None:
        """Переход на ssh_in рендерит скрипт — включение через реассерт. На
        обвязке старого образца включать нечего: цепочки нет."""
        from awgbot.infra import gwguard
        with self._ssh_lock:
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
        with self._ssh_lock:
            gwguard.write_env(SSH_FILTER="0")
            ok, err = gwguard.reassert()
            if not ok:
                raise ServiceError(f"таблица не перевыставлена: {err}")

    def _ssh_sync_sets(self, info: dict | None = None) -> dict:
        """Наборы с динамикой: ssh_allow4 (IP + резолв имён), server4 (хост
        сервера), lan4 (подсети интерфейса квартиры). Резолв имён меняется —
        запомнить в firewall.env для следующей загрузки (скрипт кладёт
        SSH_ALLOW_RESOLVED в набор до первого тика); DNS молчит — файл не
        трогать, в наборе — прошлый адрес. Возвращает {'allow', 'server',
        'lan': bool, 'unresolved': [...]}."""
        from awgbot.infra import gwguard, nftguard
        env = gwguard.read_env()
        allow = self._ssh_allow_split(env)
        v4, bad, _held = self._ssh_resolve(allow, env)
        literal = [a for a in allow if a[0].isdigit()]
        out = {"allow": False, "server": False, "lan": False, "unresolved": bad}
        if info is None:
            try:
                info = gwguard.table_info()
            except gwguard.GwGuardError:
                info = None
        if not bad:
            # только когда все имена ответили: иначе затёрли бы прошлый адрес
            resolved = [x for x in v4 if x not in literal and not gwguard.overlaps(x, literal)]
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
            lan = gwguard.lan_nets()
            if lan:                                   # не определить — набор скрипта не трогаем
                out["lan"] = gwguard.set_sync("lan4", set(lan), info)
        except gwguard.GwGuardError as e:
            log.warning("gateway: наборы SSH не приведены: %s", e)
        return out

    # ── тик ──────────────────────────────────────────────────────────────────

    def ssh_reconcile(self, info: dict | None = None, fact: tuple | None = None) -> list[Notification]:
        """Порт как факт: sshd слушает не то, что в firewall.env → записать,
        перевыставить таблицу сразу (одно событие, не стрик), уведомить один
        раз на значение. Таблица всё ещё не на порту sshd (реассерт не
        прошёл) — повторять не чаще раза в 10 минут. Затем — наборы с
        динамикой. После --rollback (юнита нет) — ничего не пишем."""
        from awgbot.infra import gwguard, sshd
        notes: list[Notification] = []
        if not gwguard.plumbing_installed():
            return notes
        with self._ssh_lock:
            env = gwguard.read_env()
            port, _ports = fact if fact is not None else self.ssh_port_fact(env)
            if port is not None:
                cur = env.get("SSH_PORT", "")
                if str(port) != cur:
                    try:
                        gwguard.write_env(SSH_PORT=str(port))
                    except (OSError, ValueError) as e:
                        log.warning("gateway: SSH_PORT не записан: %s", e)
                    else:
                        ok = True
                        # Пустой файл и порт 22 — первый тик после обновления на
                        # машине «как сегодня»: таблица и так на 22, реассерт лишний.
                        if cur or port != 22:
                            ok, err = gwguard.reassert()
                            self._ssh_last_reassert = time.monotonic()
                            if not ok:
                                log.warning("gateway: реассерт после смены порта sshd: %s", err)
                            info = None                 # таблица переставлена — снимок устарел
                        seen = self.db.get_state(self._SSH_PORT_SEEN_KEY) or ""
                        if cur and seen != str(port):
                            self.invalidate_ssh_static()  # порт сменил, возможно, новый владелец
                            owner = sshd.owner()
                            who = (f" (задал {'OMV' if owner.kind == 'omv' else 'другой процесс'})"
                                   if owner else " — sshd_config правили мимо бота")
                            tail = ("Фильтр переведён на новый порт: из туннеля, из локальной сети "
                                    "и снаружи." if ok else
                                    "Таблицу перевыставить не удалось — повторю через несколько минут; "
                                    "пока сервер по линку стучится в старый порт.")
                            notes.append(Notification(
                                config.ADMIN_ID,
                                f"🛡 Порт SSH на шлюзе изменился: {cur} → {port}{who}. {tail}",
                                critical=False))
                        self.db.set_state(self._SSH_PORT_SEEN_KEY, str(port))
                else:
                    held = (info or {}).get("ssh_ports", {}).get("tunnel_in") if info else None
                    if info and held is not None and held != port \
                            and time.monotonic() - self._ssh_last_reassert >= self._SSH_REASSERT_RETRY:
                        # файл верный, таблица нет — прошлый реассерт не прошёл
                        ok, err = gwguard.reassert()
                        self._ssh_last_reassert = time.monotonic()
                        log.warning("gateway: таблица держит порт %s, sshd на %s — реассерт %s",
                                    held, port, "прошёл" if ok else f"не прошёл: {err}")
                        info = None
            try:
                self._ssh_sync_sets(info)
            except Exception as e:                            # noqa: BLE001
                log.warning("gateway: ssh sets: %s", e)
        return notes

    def ssh_checks(self, info: dict | None, fact: tuple | None = None) -> list:
        """Проверки монитора: таблица держит порт sshd; фильтр снаружи (если
        включён) действительно стоит. ok=None — sshd не запущен."""
        from awgbot.domain.gateway import GwCheck
        from awgbot.infra import gwguard
        checks = []
        port, _ = fact if fact is not None else self.ssh_port_fact()
        table_ports = (info or {}).get("ssh_ports", {})
        held = table_ports.get("tunnel_in")
        if port is None:
            checks.append(GwCheck("порт SSH", None, "sshd не запущен"))
        elif info is None:
            pass                                           # таблицы нет — своя проверка выше
        else:
            ok = held == port
            checks.append(GwCheck("порт SSH", ok, "" if ok else
                                  f"sshd слушает {port}, таблица держит {held or '?'}: "
                                  "реассерт не прошёл (🔧 Мастер восстановления)"))
        if gwguard.read_env().get("SSH_FILTER") == "1" and info is not None:
            if "ssh_in" not in info["chains"]:
                checks.append(GwCheck("фильтр SSH снаружи", False,
                                      "включён, а цепочки ssh_in в таблице нет: перевыпусти "
                                      "конфигурацию шлюза с сервера и примени её здесь"))
            else:
                ok = table_ports.get("input") == (port if port is not None else held)
                checks.append(GwCheck("фильтр SSH снаружи", ok, "" if ok else
                                      "включён, а перехода на цепочку ssh_in в таблице нет "
                                      "(🔧 Мастер восстановления)"))
        return checks
