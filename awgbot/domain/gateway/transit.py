"""transit.py — VPN-транзит: обвязка локальной сети, свои домены, фиды по каналу."""

from __future__ import annotations

import html
import json
import os
import time
from awgbot.core import config
from awgbot.util import timeutil
from awgbot.domain.services import Notification
from awgbot.domain import gwownlists
from awgbot.domain.gateway import base
from awgbot.domain.gateway.base import GwCheck, GwStatus, log


class TransitMixin:
    """VPN-транзит: обвязка локальной сети, свои домены, фиды по каналу."""
    # ── локальная сеть без VPN ──────────────
    _LAN_LAST_KEY = "gw_lan_counters"       # {"lan": pkts, "dns": pkts, "lan_at": iso, "dns_at": iso}
    _LAN_QUIET_SECONDS = 24 * 3600          # столько без пакетов из LAN — «роутер не заворачивает»
    _LAN_FAILS_KEY = "gw_lan_lists_fails"

    BUSY_APPLYING = ("обвязка сейчас применяется (конфигурация или настройки с сервера AWG) — "
                     "повтори через минуту")
    BUSY_ACTIVATING = "юнит обвязки ещё стартует — повтори через минуту"
    STILL_APPLYING = "обвязка всё ещё применяет настройки — итог в 🩺 Здоровье через несколько минут"

    def reassert_guarded(self, why: str, *, timeout: int | None = None) -> tuple[bool, str]:
        """Единственный путь к рестарту юнита обвязки — для тика, кнопки
        «Восстановить», путей SSH и настроек с сервера.

        Под замком применения без ожидания: идёт применение (файл конфигурации
        из чата, настройки из канала) — включение режима без VPN минуты стоит
        на apt, и рестарт юнита посреди него убил бы dpkg. Юнит уже в
        activating — то же самое, только запущено извне. Отказ — словами:
        вызывающий сам решает, откатывать ли своё. Отметка времени общая с
        троттлингом тика."""
        from awgbot.infra import gwguard
        if not base._APPLY_LOCK.acquire(blocking=False):
            return False, self.BUSY_APPLYING
        try:
            if gwguard.unit_state().get("ActiveState") == "activating":
                return False, self.BUSY_ACTIVATING
            self._last_reassert = time.monotonic()
            ok, err = gwguard.reassert() if timeout is None else gwguard.reassert(timeout=timeout)
        finally:
            base._APPLY_LOCK.release()
        if not ok:
            log.warning("gateway: реассерт обвязки не удался (%s): %s", why, err)
        return ok, err

    def _reassert_throttled(self, why: str) -> bool:
        """Рестарт юнита обвязки не чаще раза в 10 минут (общий троттлинг с
        tg_mark_ensure): скрипт идемпотентен, вернёт и guard, и awg_home."""
        if time.monotonic() - self._last_reassert < self._REASSERT_MIN_INTERVAL:
            return False
        ok, _err = self.reassert_guarded(why)
        if ok:
            log.warning("gateway: обвязка перевыставлена: %s", why)
        return ok

    def _upstream_verdict(self) -> bool | None:
        """Отвечает ли апстрим — по статистике dnsmasq, без запроса наружу.

        За тик росли отказы и не росли успешные отправки — апстрим молчит.
        Запросов не было вовсе (квартира спит) — держим прошлый вердикт: ждать
        ответа некому, а спрашивать ради проверки значит завести маячок."""
        from awgbot.infra import gwguard
        stats = gwguard.upstream_stats()
        if not stats:
            return None
        sent = sum(v[0] for v in stats.values())
        failed = sum(v[1] for v in stats.values())
        prev = self.__dict__.get("_upstream_prev")
        self.__dict__["_upstream_prev"] = (sent, failed)
        if prev is None or sent < prev[0] or failed < prev[1]:
            # первый взгляд (или dnsmasq перезапустился) — по накопленному с
            # его старта: ни одного ответа на все отправленные — апстрим молчит
            if sent == 0:
                return self.__dict__.setdefault("_upstream_ok", True)
            ok = failed < sent
            self.__dict__["_upstream_ok"] = ok
            return ok
        d_sent, d_failed = sent - prev[0], failed - prev[1]
        if d_sent == 0 and d_failed == 0:
            return self.__dict__.get("_upstream_ok", True)
        ok = d_failed < d_sent or d_failed == 0
        self.__dict__["_upstream_ok"] = ok
        return ok

    def lan_status(self) -> tuple[dict, list[GwCheck]]:
        """(блок для панели, проверки) — только при LAN_MODE=1 в юните; иначе
        ({}, []). Счётчики заворота и DNS — по разности с прошлым тиком:
        растут — роутер шлёт; не растут дольше _LAN_QUIET_SECONDS — нет."""
        from awgbot.infra import gwguard
        if not gwguard.lan_mode():
            return {}, []
        st = gwguard.script_status()
        iface, addr = st.get("LAN_IF", ""), st.get("LAN_ADDR", "")
        resolver = gwguard.unit_env("RESOLVER")
        nets_all = gwguard.unit_env("HOME_SUBNETS").split()
        info: dict = {"iface": iface, "addr": addr, "resolver": resolver or "1.1.1.1 (запасной)",
                      "subnet": nets_all[0] if nets_all else "",
                      "uplink": st.get("UPLINK_IF", "")}
        checks: list[GwCheck] = []
        # скрипта списков нет — фиды применить нечем, ни свои, ни из канала
        if not os.path.exists(gwguard.LAN_LISTS_SCRIPT):
            checks.append(GwCheck("скрипт списков", False,
                                  "скрипт обновления списков РФ-доступа не найден — "
                                  "перевыпусти конфигурацию шлюза с сервера AWG"))
        # скрипт обвязки не смог применить раздел — причина в статусе, а не «перевыпусти»
        err = st.get("LAN_ERROR", "")
        if err:
            checks.append(GwCheck("применение локальной сети", False, err))
        # интерфейс переехал (OMV собрал bridge/bond — адрес теперь на br0): правила
        # остались на старом имени, весь LAN идёт мимо маркировки. Перевыставить.
        nets = gwguard.unit_env("HOME_SUBNETS").split()
        if nets and iface:
            live = gwguard.iface_for_subnet(nets[0])
            if live and live[0] != iface:
                self._reassert_throttled(f"локальная сеть переехала {iface} → {live[0]}")
                checks.append(GwCheck("применение локальной сети", False,
                                      f"адрес подсети теперь на {live[0]}, правила стоят на {iface} — перевыставляю"))
        # резолвер
        active = gwguard.dnsmasq_active()
        checks.append(GwCheck("резолвер", active, "" if active else
                              ("dnsmasq не запущен: journalctl -u dnsmasq -e" if active is False
                               else "systemctl не ответил")))
        up = self._upstream_verdict() if active else None
        checks.append(GwCheck("апстрим через аплинк", up,
                              "" if up else
                              (f"{info['resolver']} не отвечает через аплинк: аплинк или резолвер сервера"
                               if up is False else ("резолвер не запущен — не проверяли" if not active
                                                    else "статистика dnsmasq не прочиталась"))))
        # таблица и наборы
        try:
            home = gwguard.home_table_info()
        except gwguard.GwGuardError:
            home = None
        table_ok = home is not None and "prerouting" in home["chains"]
        if not table_ok and not err:
            # таблицу снёс кто-то посторонний (nftables.service с flush ruleset,
            # ручной nft) — вернёт юнит; перевыпуск тут ни при чём
            fixed = self._reassert_throttled("таблицы awg_home нет")
            checks.append(GwCheck("таблица локальной сети", False,
                                  "таблицы awg_home нет — перевыставляю обвязку" if fixed
                                  else "таблицы awg_home нет — перевыставлю обвязку в ближайший такт"))
        else:
            checks.append(GwCheck("таблица локальной сети", table_ok, "" if table_ok else err))
        ls = gwguard.lists_status()
        info["domains"] = int(ls.get("domains") or 0)
        info["nets"] = int(ls.get("nets") or 0)
        info["updated_at"] = ls.get("updated_at", "")
        info["own_vpn"], info["own_ru"] = gwguard.lan_own_lists()
        lists_ok = table_ok and (info["nets"] > 0 or info["domains"] > 0)
        checks.append(GwCheck("списки", lists_ok, "" if lists_ok else
                              "списки не загружены: обновление не прошло"))
        # заворот и DNS с роутера — по росту счётчиков
        if home is not None:
            last = self.db.get_state_json(self._LAN_LAST_KEY, {})
            now = timeutil.now()
            cur = {"lan": home["lan_pkts"], "dns": home["dns_pkts"]}
            for key, name, why in (("lan", "трафик с роутера",
                                     "роутер не маршрутизирует трафик на шлюз (❓ Роутер)"),
                                    ("dns", "DNS с роутера",
                                     "DHCP роутера раздаёт не адрес шлюза")):
                prev = int(last.get(key, -1))
                at = last.get(f"{key}_at") or ""
                if prev < 0 or cur[key] > prev or cur[key] < prev:      # первый тик / рост / сброс
                    at = timeutil.to_iso(now)
                    ok: bool | None = True if prev >= 0 else None
                    detail = "" if prev >= 0 else "первый замер"
                else:
                    try:
                        quiet = (now - timeutil.parse_iso(at)).total_seconds() if at else 0.0
                    except ValueError:
                        quiet = 0.0
                    ok = quiet < self._LAN_QUIET_SECONDS
                    detail = "" if ok else (f"пакетов из локальной сети нет с "
                                            f"{timeutil.fmt_dt_ui(timeutil.parse_iso(at)) if at else '?'}: {why}")
                info[f"{key}_pkts"] = cur[key]
                last[key] = cur[key]
                last[f"{key}_at"] = at
                checks.append(GwCheck(name, ok, detail))
            if json.dumps(last) != (self.db.get_state(self._LAN_LAST_KEY) or ""):
                self.db.set_state(self._LAN_LAST_KEY, json.dumps(last))   # запись только при изменении
        for c in checks:
            c.group = "lan"
        return info, checks

    def lan_domains(self, cmd: str, domains: list[str]) -> tuple[bool, str]:
        """Свои списки из чата: add | ru | del. Разбор и денилист — в скрипте.
        Счётчики в снимке панели — сразу, не ждать тика монитора."""
        from awgbot.infra import gwguard
        ok, out = gwguard.run_lan_domain(cmd, domains)
        if ok:
            self.lan_own_counts_refresh()
        return ok, out

    def lan_own_counts_refresh(self) -> None:
        """Обновить в снимке последнего тика счётчики своих списков: панель
        рисуется из снимка, и после правки списка показывала бы прежние числа
        до следующего тика монитора."""
        from awgbot.infra import gwguard
        raw = self.db.get_state(self._SNAPSHOT_KEY)
        if not raw:
            return
        try:
            st = GwStatus.from_json(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            return
        lan = getattr(st, "lan", None)
        if not lan:
            return
        lan["own_vpn"], lan["own_ru"] = gwguard.lan_own_lists()
        self.db.set_state(self._SNAPSHOT_KEY, st.to_json())

    def lan_own_lists(self) -> list[tuple[str, str]]:
        """[(vpn|ru, домен)] из скрипта списков."""
        from awgbot.infra import gwguard
        ok, out = gwguard.run_lan_domain("list", [])
        return [(k, d) for d, k in gwownlists.parse_list(out).items()] if ok else []

    def lan_lists_now(self) -> tuple[bool, str]:
        """Обновить списки (кнопка и задача): (ok, хвост вывода); счётчик
        провалов подряд ведётся здесь, чтобы ручной успех его сбрасывал."""
        from awgbot.infra import gwguard
        ok, tail = gwguard.run_lan_lists()
        fails = 0 if ok else int(self.db.get_state(self._LAN_FAILS_KEY) or 0) + 1
        self.db.set_state(self._LAN_FAILS_KEY, str(fails))
        return ok, tail

    def lan_router_params(self) -> tuple[str, str, list[str]]:
        """(подсеть, адрес шлюза, локальные подсети других шлюзов) для рецепта
        роутера — их знает только малина: подсети из юнита обвязки, адрес из
        статуса скрипта."""
        from awgbot.infra import gwguard
        nets = gwguard.unit_env("HOME_SUBNETS").split()
        peers = gwguard.unit_env("PEER_HOME_NETS").split()
        return (nets[0] if nets else ""), gwguard.script_status().get("LAN_ADDR", ""), peers


    # ── фиды локальной сети по каналу ────────
    _LAN_CHANNEL_HASH_KEY = "gwlink_lists_hash"
    _LAN_CHANNEL_AT_KEY = "gwlink_lists_at"
    # Своё скачивание молчит, пока канал привозит фиды: сервер обновляет их раз
    # в шесть часов, запас вдвое — чтобы одна пропущенная доставка не вернула
    # адрес квартиры на GitHub раньше, чем канал успеет исправиться.
    _LAN_CHANNEL_FRESH_S = 12 * 3600
    _LAN_FEED_MAX = 8 * 1024 * 1024

    def lan_feeds_applied_hash(self) -> str:
        """Отпечаток фидов, применённых из канала; пусто — не применяли."""
        return (self.db.get_state(self._LAN_CHANNEL_HASH_KEY) or "").strip()

    def lan_feeds_from_channel_fresh(self) -> bool:
        """Фиды возит канал — своя задача никуда не ходит.

        Пока сессия жива и фиды из канала однажды пришли — да: сервер обновляет
        их сам и везёт, когда они меняются; неизменные фиды — это не повод
        лезть на GitHub с адреса квартиры. Канал оборван — запас 12 часов от
        последнего контакта, дальше агент снова качает сам."""
        # Живая сессия — живой сервер: полуоткрытую после жёсткого ребута ВПС
        # keepalive ядра на сокете добивает за минуты, и online гаснет. Слово
        # сервера тут не мерило: в здоровой сессии он молчит (роль шлёт только
        # при смене), и «тише 12 часов» заставляло агента качать фиды самому с
        # адреса квартиры и затирать фиды канала.
        if self.channel.online and self.lan_feeds_applied_hash():
            return True
        raw = self.db.get_state(self._LAN_CHANNEL_AT_KEY) or ""
        return raw.isdigit() and time.time() - int(raw) < self._LAN_CHANNEL_FRESH_S

    def lan_feeds_touch(self, digest: str = "") -> None:
        """Отметка «канал подтвердил фиды»: сервер назвал тот же отпечаток или
        сессия только что закрылась — отсюда отсчитывается запас."""
        if digest and digest != self.lan_feeds_applied_hash():
            return
        if self.lan_feeds_applied_hash():
            self.db.set_state(self._LAN_CHANNEL_AT_KEY, str(int(time.time())))

    def apply_lan_feeds(self, digest: str, packed_b64: str) -> dict:
        """Применить фиды, привезённые каналом: {ok, error}.

        Принятое — недоверенные данные с чужой машины, поэтому: потолок размера
        до и после распаковки (сжатая бомба не должна съесть память малины),
        сверка отпечатка с присланным, запись во временный каталог, и дальше те
        же проверки, что и для скачанного, — формат, длина, dnsmasq --test с
        откатом. Сами фиды — только данные: скрипт кладёт их в конфиги dnsmasq и
        в набор nft, ничего из них не исполняется.
        """
        import base64
        import zlib
        from awgbot.infra import gwguard
        if not gwguard.lan_mode():
            return {"ok": False, "error": "VPN-транзит на шлюзе выключен"}
        if not os.path.exists(gwguard.LAN_LISTS_SCRIPT):
            return {"ok": False, "error": "скрипта списков нет — примени конфигурацию шлюза"}
        if len(packed_b64 or "") > self._LAN_FEED_MAX:
            return {"ok": False, "error": "фиды больше предела"}
        try:
            packed = base64.b64decode(packed_b64 or "", validate=True)
            d = zlib.decompressobj()
            raw = d.decompress(packed, self._LAN_FEED_MAX)
            if d.unconsumed_tail:
                return {"ok": False, "error": "фиды после распаковки больше предела"}
            data = json.loads(raw.decode())
            domains, nets = str(data["domains"]), str(data["nets"])
        except (ValueError, KeyError, TypeError, zlib.error, UnicodeDecodeError) as e:
            return {"ok": False, "error": f"фиды повреждены: {e}"}
        from awgbot.util import gwlink
        got = gwlink.feeds_hash(domains, nets)
        if got != digest:
            return {"ok": False, "error": "отпечаток фидов не сошёлся"}
        if digest == self.lan_feeds_applied_hash():
            self.db.set_state(self._LAN_CHANNEL_AT_KEY, str(int(time.time())))
            return {"ok": True, "error": ""}
        os.makedirs(gwguard.LAN_FEED_DIR, mode=0o700, exist_ok=True)
        for name, text in (("domains.lst", domains), ("nets.lst", nets)):
            path = os.path.join(gwguard.LAN_FEED_DIR, name)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(path + ".tmp", path)
        ok, tail = gwguard.run_lan_lists(from_dir=gwguard.LAN_FEED_DIR)
        if not ok:
            return {"ok": False, "error": tail or "скрипт списков отказал"}
        with self.db.transaction():
            self.db.set_state(self._LAN_CHANNEL_HASH_KEY, digest)
            self.db.set_state(self._LAN_CHANNEL_AT_KEY, str(int(time.time())))
            self.db.set_state(self._LAN_FAILS_KEY, "0")
        return {"ok": True, "error": ""}

    def lan_lists_needed(self) -> bool:
        """Режим без VPN включён, а списков ещё нет: сразу после применения
        бандла, включившего режим, ждать планового обновления (до шести часов)
        значило бы оставить квартиру с пустыми наборами — «заблокированное»
        шло бы напрямую."""
        from awgbot.infra import gwguard
        if not gwguard.lan_mode():
            return False
        ls = gwguard.lists_status()
        return not (int(ls.get("domains") or 0) or int(ls.get("nets") or 0))

    def lan_lists_update(self) -> list[Notification]:
        """Задача планировщика: обновить списки; два провала подряд — замечание.
        Пока фиды привозит канал — не ходит никуда: адрес квартиры за фидами на
        GitHub и в Google не ходит, это и есть смысл этапа 3. Канал замолчал —
        задача сама вернётся к скачиванию."""
        from awgbot.infra import gwguard
        if not gwguard.lan_mode():
            return []
        if self.lan_feeds_from_channel_fresh():
            log.info("gateway: фиды локальной сети привозит канал — сам не качаю")
            return []
        ok, tail = self.lan_lists_now()
        fails = int(self.db.get_state(self._LAN_FAILS_KEY) or 0)
        if ok:
            log.info("gateway: списки локальной сети обновлены")
            return []
        log.warning("gateway: списки локальной сети не обновились: %s", tail)
        if fails == 2:
            return [Notification(config.ADMIN_ID,
                                 "⚠️ Списки локальной сети не обновились дважды подряд: "
                                 + (html.escape(tail, quote=False) or "без подробностей")
                                 + "\nФиды — через аплинк; проверь "
                                 "🩺 Здоровье.", critical=False)]
        return []
