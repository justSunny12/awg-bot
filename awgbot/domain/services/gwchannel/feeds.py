"""feeds.py — фиды локальной сети: скачивание на ВПС, отпечаток, ответы шлюзов."""
from __future__ import annotations

import json

from awgbot.core import config
from awgbot.util import timeutil

from .common import log


class FeedsMixin:
    # ── списки локальной сети по каналу (этап 3) ─────────────────────────────
    # Те же источники, что у скрипта списков на шлюзе (awg-lan-lists.sh): ВПС и
    # так ходит к этому репозиторию за своими списками, а локальный адрес с этим
    # перестаёт ходить на GitHub и в Google каждые шесть часов.
    _LAN_ITDOG = "https://raw.githubusercontent.com/itdoginfo/allow-domains/main"
    _LAN_DOMAINS_URL = _LAN_ITDOG + "/Russia/inside-dnsmasq-ipset.lst"
    _LAN_SERVICES = ("telegram", "meta", "twitter", "cloudflare", "discord")
    _LAN_GOOG_URL = "https://www.gstatic.com/ipranges/goog.json"
    _LAN_FEEDS_AT_KEY = "gwlink_lan_feeds_at"
    # момент последней удачной сверки с источниками — и когда фиды не изменились:
    # по нему канал заново подтверждает шлюзам «у тебя актуальные» (deliver_lists)
    _LAN_FEEDS_CHECKED_KEY = "gwlink_lan_feeds_checked"
    _GWLINK_LISTS_KEY = "gwlink_lists"

    def _lan_feeds_path(self):
        return config.DATA_DIR / "lan-feeds.json"

    def gwlink_lan_feeds(self) -> dict:
        """{'hash', 'domains', 'nets', 'fetched_at'} или пусто — фидов ещё нет."""
        try:
            data = json.loads(self._lan_feeds_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) and data.get("hash") else {}

    def gwlink_lan_feeds_digest(self) -> str:
        """Отпечаток текущих фидов без разбора всего файла на каждом такте:
        кеш по времени изменения, inode и размеру (одного mtime мало — на Linux
        две записи подряд его не различают), файл меняется раз в шесть часов."""
        path = self._lan_feeds_path()
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_ino, st.st_size)
        except OSError:
            return ""
        cached = self.__dict__.get("_lan_feeds_digest")
        if cached and cached[0] == key:
            return cached[1]
        digest = self.gwlink_lan_feeds().get("hash", "")
        self.__dict__["_lan_feeds_digest"] = (key, digest)
        return digest

    def gwlink_lan_feeds_update(self, force: bool = False) -> bool:
        """Скачать фиды локальной сети для шлюзов. True — фиды сменились.

        Только если хотя бы у одного слота включено «за шлюзом — без VPN»: без
        него фиды никому не нужны, и ходить за ними — лишняя сеть. Период — тот
        же, что у своих списков ВПС. Негодное (короткий фид доменов, пустые
        подсети) не заменяет прежнее: застывший список лучше пустого, а
        проверки формата на шлюзе повторятся всё равно.
        """
        import re
        import time as _time
        from awgbot.core import settings
        from awgbot.infra import routing
        if not any(g.lan_mode for g in self.db.gateways()):
            return False
        import random
        now = int(_time.time())
        every = int(settings.get("app.routing.lists_refresh_hours", 6)) * 3600
        # В state — момент СЛЕДУЮЩЕГО похода, с джиттером ±40 %: «прошло шесть
        # часов» внутри тика давало метроном с разбросом в минуты, а это семь
        # одних и тех же адресов по часам.
        # Гейт по моменту — и при 404 или коротком фиде тоже (иначе поход к
        # GitHub шёл бы каждый тик, 429 не за горами): неудача ставит повтор
        # через полчаса. Но когда фидов нет вовсе (восстановление на новом ВПС:
        # файл в копию не входит, а момент в базе — в будущем), первый поход
        # идёт сразу — иначе агенты сидели бы без фидов до восьми часов.
        nxt = self.db.get_state(self._LAN_FEEDS_AT_KEY) or ""
        if not force and nxt.isdigit() and now < int(nxt) and self.gwlink_lan_feeds_digest():
            return False
        self.db.set_state(self._LAN_FEEDS_AT_KEY, str(now + int(every * random.uniform(0.6, 1.4))))

        def retry_soon() -> None:
            self.db.set_state(self._LAN_FEEDS_AT_KEY, str(now + int(1800 * random.uniform(0.6, 1.4))))
        body, err, _code = routing.fetch(self._LAN_DOMAINS_URL, timeout=10)
        if body is None or len(re.findall(r"(?m)^ipset=/", body)) < 10:
            log.warning("канал линка: фид доменов локальной сети не получен (%s)", err or "короткий")
            retry_soon()
            return False
        nets: list[str] = []
        missed: list[str] = []
        for svc in self._LAN_SERVICES:
            text, err, _code = routing.fetch(f"{self._LAN_ITDOG}/Subnets/IPv4/{svc}.lst", timeout=10)
            if text is None:
                log.info("канал линка: подсети %s не получены (%s)", svc, err)
                missed.append(svc)
                continue
            nets += re.findall(r"(?m)^\d+\.\d+\.\d+\.\d+/\d+$", text)
        goog, err, _code = routing.fetch(self._LAN_GOOG_URL, timeout=10)
        if goog is not None:
            nets += re.findall(r'"ipv4Prefix"\s*:\s*"(\d+\.\d+\.\d+\.\d+/\d+)"', goog)
        else:
            missed.append("goog")
        if not nets:
            log.warning("канал линка: подсети локальной сети не получены ни из одного источника")
            retry_soon()
            return False
        if missed and self.gwlink_lan_feeds_digest():
            # Часть источников не ответила: такой сбор — не фид. Опубликуй его —
            # и подсети сервиса, чей список не пришёл, ушли бы со шлюзов до
            # следующего похода (часы). Прежний фид остаётся, повтор — скоро.
            log.warning("канал линка: подсети %s не получены — прежний фид остаётся", ", ".join(missed))
            retry_soon()
            return False
        nets_text = "\n".join(sorted(set(nets))) + "\n"
        from awgbot.util import gwlink
        digest = gwlink.feeds_hash(body, nets_text)
        if digest == self.gwlink_lan_feeds_digest():
            # источники ответили, фиды те же — сверка всё равно состоялась
            self.db.set_state(self._LAN_FEEDS_CHECKED_KEY, str(now))
            return False
        path = self._lan_feeds_path()
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"hash": digest, "domains": body, "nets": nets_text,
                                       "fetched_at": timeutil.to_iso(timeutil.now())},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            # запись не удалась (нет места) — это не повод обрывать блок тика
            log.warning("канал линка: фиды локальной сети не записаны: %s", e)
            retry_soon()
            return False
        if missed:
            # прежнего фида нет — неполный лучше пустого, но полный нужен скоро
            log.warning("канал линка: фиды локальной сети без подсетей %s — повтор скоро", ", ".join(missed))
            retry_soon()
            return True
        self.db.set_state(self._LAN_FEEDS_CHECKED_KEY, str(now))
        log.info("канал линка: фиды локальной сети обновлены")
        return True

    def gwlink_lan_feeds_checked(self) -> str:
        """Когда сервер последний раз удачно сверил фиды с источниками (epoch);
        пусто — ещё не сверял с этой версии."""
        return (self.db.get_state(self._LAN_FEEDS_CHECKED_KEY) or "").strip()

    def gwlink_lists_ack_in(self, slot_id: int, body: dict) -> None:
        self._gwlink_ack_put(self._GWLINK_LISTS_KEY, slot_id, body)

    def gwlink_lists(self, slot_id: int) -> dict:
        """Что шлюз ответил на последнюю доставку фидов: ok, hash, at, error."""
        return self._gwlink_ack_get(self._GWLINK_LISTS_KEY, slot_id)
