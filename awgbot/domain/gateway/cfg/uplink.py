"""uplink.py — конфиг аплинка с сервера AWG: сравнение со стоящим, ключи признания, наблюдение «аплинк мёртв», решение, ожидающий конфиг файлом."""
from __future__ import annotations

import os
import time

from awgbot.domain import gwcfg
from awgbot.util import wgconf

from .. import base

UPLINK_ALIVE_SECONDS = 180      # хендшейк моложе — аплинк жив (keepalive 25-35 с, рукопожатие раз в 2 мин)
UPLINK_DEAD_SECONDS = 600       # аварийное правило: столько подряд агент сам видит аплинк без хендшейка
_POLICY_KEYS = ("PostUp", "PostDown")   # дописывает скрипт обвязки, в сравнение не идут


class CfgUplinkMixin:
    """Аплинк — единственная дорога агента в Telegram, поэтому новый конфиг
    ставится только с согласия человека в чате (приглашение с отпечатком).
    Молча — только ключи признания, если стоящий аплинк ими по-прежнему
    признаётся, и аварийно — если агент сам 10 минут подряд видит аплинк
    без хендшейка, а приехавший конфиг сервер назвал конфигом этого шлюза:
    подтвердить в чате тогда нечем. Аварийное правило проверяется и при
    приходе документа, и на каждом тике, пока вопрос висит: событие, уронившее
    аплинк (финал переезда), обычно и рождает документ — секунды спустя,
    когда «10 минут» ещё не набежали. Транзакция — uplinktxn."""

    # ── что стоит ────────────────────────────────────────────────────────────

    def cfg_uplink_if(self, cached: bool = False) -> str:
        """Интерфейс аплинка. cached — без единого вызова команд (для снимка):
        то, что тик уже определил, или решение скрипта из файла статуса."""
        from awgbot.core import config
        from awgbot.infra import gwguard
        if not cached:
            return gwguard.uplink_interface()
        state = self.__dict__.get("_uplink_state")
        if state and state[0]:
            return state[0]
        return config.GW_UPLINK_IF or gwguard.script_status().get("UPLINK_IF", "").strip()

    def cfg_uplink_text(self, cached: bool = False) -> str:
        from awgbot.infra import gwguard
        iface = self.cfg_uplink_if(cached)
        if not iface:
            return ""
        try:
            return base.pathlib_read(gwguard.uplink_conf_path(iface))
        except OSError:
            return ""

    @staticmethod
    def cfg_uplink_keys() -> tuple[str, str]:
        """Ключи признания из юнита обвязки: (GATEWAY_PUBKEY, GATEWAY_PREV_PUBKEY)."""
        from awgbot.infra import gwguard
        return gwguard.unit_env("GATEWAY_PUBKEY"), gwguard.unit_env("GATEWAY_PREV_PUBKEY")

    def cfg_uplink_print(self, cached: bool = False) -> str:
        """Отпечаток стоящего аплинка: конфиг без строк политики и ключи
        признания из юнита. Аплинка нет — пусто. cached — для снимка, без команд."""
        text = self.cfg_uplink_text(cached)
        if not text:
            return ""
        pub, prev = self.cfg_uplink_keys()
        return gwcfg.uplink_print(text, pub, prev)

    @staticmethod
    def _cfg_uplink_device_pub(text: str) -> str:
        try:
            return wgconf.pubkey_of(wgconf.parse(text).get("Interface", "PrivateKey"))
        except wgconf.WgConfError:
            return ""

    @staticmethod
    def cfg_uplink_fp(value: dict) -> str:
        """Отпечаток пары ключей нового аплинка — устройство и сервер: тот же
        показывает карточка слота на ВПС."""
        try:
            conf = wgconf.parse(value.get("conf", ""))
        except wgconf.WgConfError:
            return ""
        return gwcfg.key_fp(wgconf.pubkey_of(conf.get("Interface", "PrivateKey")), conf.get("Peer", "PublicKey"))

    @staticmethod
    def _cfg_uplink_canon(text: str) -> str:
        try:
            return wgconf.canonical(wgconf.drop_keys(wgconf.parse(text), _POLICY_KEYS), "uplink")
        except wgconf.WgConfError:
            return ""

    # ── «аплинк мёртв» — только по наблюдению ────────────────────────────────

    def cfg_uplink_observe(self) -> bool:
        """Замер и ответ «мёртв ли аплинк». Мёртв — агент сам, по монотонным
        часам, видит его без свежего хендшейка UPLINK_DEAD_SECONDS подряд
        (интерфейса нет, пир не отвечал ни разу или хендшейк старше
        UPLINK_ALIVE_SECONDS). Сбой `awg show` — «не знаю»: окно не начинает и
        не обрывает. Перезапуск агента начинает наблюдение заново."""
        from awgbot.infra import gwguard
        iface = self.cfg_uplink_if()
        state, age = gwguard.handshake_state(iface) if iface else ("absent", None)
        now = time.monotonic()
        since = self.__dict__.get("_uplink_dead_since")
        if state == "ok" and age is not None and age <= UPLINK_ALIVE_SECONDS:
            self.__dict__["_uplink_dead_since"] = None
            return False
        if state == "unknown":
            return since is not None and now - since >= UPLINK_DEAD_SECONDS
        if since is None:
            self.__dict__["_uplink_dead_since"] = since = now
        return now - since >= UPLINK_DEAD_SECONDS

    # ── решение ──────────────────────────────────────────────────────────────

    def cfg_uplink_decide(self, value: dict) -> tuple[str, str]:
        """Что делать с разделом uplink: same — то же; keys — отличаются только
        ключи признания, а стоящий аплинк ими признаётся (молча); declined —
        от этого серверного значения уже отказались; emergency — аплинк мёртв
        по наблюдению (без вопроса); ask — приглашение человеку. И отпечаток
        серверного значения. Ключ устройства в конфиге проверка документа уже
        сверила с pubkey слота: сервер называет его конфигом этого шлюза."""
        server = gwcfg.uplink_print(value.get("conf", ""), value.get("pubkey", ""), value.get("prev_pubkey", ""))
        standing = self.cfg_uplink_text()
        if standing and gwcfg.uplink_print(standing, *self.cfg_uplink_keys()) == server:
            return "same", server
        new_canon = self._cfg_uplink_canon(value.get("conf", ""))
        if standing and self._cfg_uplink_canon(standing) == new_canon:
            mine = self._cfg_uplink_device_pub(standing)
            if mine and mine in (value.get("pubkey"), value.get("prev_pubkey")):
                return "keys", server
        if self.cfg_keep().get("uplink") == server:
            return "declined", server
        new_pub = self._cfg_uplink_device_pub(value.get("conf", ""))
        if new_pub and new_pub in (value.get("pubkey"), value.get("prev_pubkey")) and self.cfg_uplink_observe():
            return "emergency", server
        return "ask", server

    def cfg_uplink_same_device(self, value: dict) -> bool:
        """Ключ устройства в новом конфиге — тот же, что у стоящего аплинка."""
        mine = self._cfg_uplink_device_pub(self.cfg_uplink_text())
        return bool(mine) and mine == self._cfg_uplink_device_pub(value.get("conf", ""))

    # ── ожидающий конфиг — файлом, не в базе ─────────────────────────────────
    # Пока приглашение висит, конфиг с приватным ключом устройства лежит в
    # /etc/awg-gw/uplink.pending (0600 root), в базе — только ключи признания.

    @staticmethod
    def _cfg_uplink_pending_path() -> str:
        from awgbot.infra import gwguard
        return f"{gwguard.GW_ETC}/uplink.pending"

    def _cfg_uplink_pending_put(self, value: dict) -> dict:
        """Конфиг — в файл; возвращает значение для базы (без конфига)."""
        self._cfg_write_private(self._cfg_uplink_pending_path(), str(value.get("conf") or ""))
        return {k: v for k, v in value.items() if k != "conf"}

    def _cfg_uplink_pending_fill(self, value: dict) -> dict:
        """Значение из базы + конфиг из файла (нет файла — пустой конфиг)."""
        if "conf" in value:
            return value
        try:
            conf = base.pathlib_read(self._cfg_uplink_pending_path())
        except OSError:
            conf = ""
        return {**value, "conf": conf}

    def _cfg_uplink_pending_drop(self) -> None:
        try:
            os.unlink(self._cfg_uplink_pending_path())
        except OSError:
            pass
