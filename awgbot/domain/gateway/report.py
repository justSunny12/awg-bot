"""report.py — что агент докладывает серверу каналом и что знает о сервере из
`role`: первый выход, роль слота и возможности сервера,
версия сервера (условие `needs_main` при команде обновления), адреса в туннеле своей сети (только показ), пометка."""

from __future__ import annotations

import ipaddress
import re

from awgbot.util import timeutil

_SERVER_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(\.\d+)?$")


def _server_version(raw) -> str:
    """Версия сервера из `role` — данные с чужой машины: только форма X.Y.Z[.P],
    иначе — «old» (сервер, не приславший версию, старше 3.3.0)."""
    from awgbot.domain.selfupdate import SERVER_OLD
    val = raw.strip() if isinstance(raw, str) else ""
    return val if len(val) <= 32 and _SERVER_VERSION_RE.match(val) else SERVER_OLD


_LAN_VIRTUAL_MAX = 64          # подсетей на слот у сервера (_LAN_SUBNETS_MAX) — двойников не больше


def _lan_virtual(raw) -> list[str]:
    """Адреса в туннеле своей сети из `role` — данные с чужой машины, только
    для показа: список не длиннее 64, каждая запись — сеть (без битов хоста)
    с префиксом /16–/30 внутри диапазона, где может лежать пространство
    (lanspace.SPACE_RANGES: 10/8, 172.16/12). Что-то не так — поле целиком
    отбрасывается (пусто): показывать половину чужого списка незачем."""
    from awgbot.util import lanspace
    if not isinstance(raw, list) or len(raw) > _LAN_VIRTUAL_MAX:
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or len(item) > 18:
            return []
        try:
            net = ipaddress.IPv4Network(item)
        except ValueError:
            return []
        if not 16 <= net.prefixlen <= 30 or not any(net.subnet_of(r) for r in lanspace.SPACE_RANGES):
            return []
        if str(net) not in out:
            out.append(str(net))
    return out


class ReportMixin:
    """Что агент докладывает серверу каналом: первый выход, итог применения, роль слота, пометка."""
    # ── первый выход на связь после установки — серверу каналом ─────────────
    _FIRST_START_KEY = "agent_first_start_at"
    _INSTALLED_SENT_KEY = "gwlink_installed_sent"
    _INSTALLED_REPORT_WINDOW_S = 3600

    def first_start_note(self, fresh_db: bool) -> None:
        """Отметить первый запуск агента — только когда базы до этого запуска не
        было (её создал установщик). Обновлённый со старой версии агент отметки
        не имеет и не получает: иначе первый запуск после обновления сошёл бы за
        установку, и админ получил бы «успешно настроен» по каждому шлюзу.
        Повторные запуски отметку не двигают."""
        if fresh_db and not self.db.get_state(self._FIRST_START_KEY):
            self.db.set_state(self._FIRST_START_KEY, timeutil.to_iso(timeutil.now()))
            if hasattr(self, "keep_base_note"):
                self.keep_base_note()                     # настройки по умолчанию — мерило чистоты установки

    def installed_report_pending(self) -> bool:
        """Сказать ли серверу «установлен»: один раз, и только если агент
        впервые запустился меньше часа назад. Канала в этот час не было —
        уведомления не будет вовсе: «шлюз настроен» спустя дни только пугает."""
        if self.db.get_state(self._INSTALLED_SENT_KEY):
            return False
        try:
            age = timeutil.elapsed(timeutil.parse_iso(self.db.get_state(self._FIRST_START_KEY) or ""))
        except ValueError:
            return False
        return age <= self._INSTALLED_REPORT_WINDOW_S

    def installed_report_done(self) -> None:
        self.db.set_state(self._INSTALLED_SENT_KEY, timeutil.to_iso(timeutil.now()))


    # ── роль слота и диагностика по каналу ──
    _LINK_ROLE_KEY = "gwlink_role"

    _LINK_STANDBY_KEY = "gwlink_standby"
    _LINK_SLOT_NAME_KEY = "gwlink_slot_name"

    _LINK_SERVER_CAPS_KEY = "gwlink_server_caps"

    _LINK_SERVER_VERSION_KEY = "gwlink_server_version"

    _LINK_LAN_VIRTUAL_KEY = "gwlink_lan_virtual"

    _LINK_SERVER_BOT_KEY = "gwlink_server_bot"

    def set_link_role(self, active: bool, standby=None, name=None, caps=None, server=...,
                      lan_virtual=..., bot=...) -> None:
        """Роль этого шлюза, как её видит сервер: несёт он трафик или в резерве;
        standby — есть ли другой шлюз (для честных хвостов про синхронизацию и
        РФ-доступ), name — имя слота на сервере (заголовок рецепта роутера).
        Сам агент узнать это не может — решает автомат переключения на ВПС.
        caps — возможности сервера (список имён, контракт канала). Старый сервер
        шлёт только active — тогда прочее не трогаем. server — версия сервера AWG из `role` (с 3.3.0):
        по ней агент проверяет команду обновления (`roles.gw.needs_main`); `role` без
        версии или с чужой формой — сервер старше 3.3.0 («old»). lan_virtual —
        адреса в туннеле своей сети (с 3.3.0, только для показа): нет поля
        (None) или мусор — пусто. server и lan_virtual не переданы вовсе (...) —
        сохранённое не трогаем."""
        self.db.set_state(self._LINK_ROLE_KEY, "active" if active else "standby")
        if server is not ...:
            self.db.set_state(self._LINK_SERVER_VERSION_KEY, _server_version(server))
        if lan_virtual is not ...:
            self.db.set_state(self._LINK_LAN_VIRTUAL_KEY, " ".join(_lan_virtual(lan_virtual)))
        if bot is not ...:
            # username бота сервера (с 3.4.0) — ссылкой на его чат; мусор — пусто
            user = str(bot or "")
            ok = 3 <= len(user) <= 64 and all(ch.isalnum() or ch == "_" for ch in user) and user.isascii()
            self.db.set_state(self._LINK_SERVER_BOT_KEY, user if ok else "")
        if caps is not None:
            good = [c for c in (caps if isinstance(caps, list) else []) if isinstance(c, str)]
            self.db.set_state(self._LINK_SERVER_CAPS_KEY,
                              " ".join("".join(ch for ch in c if ch.isalnum())[:16] for c in good[:16]))
        if standby is not None:
            self.db.set_state(self._LINK_STANDBY_KEY, "1" if standby else "0")
        if name is not None:
            self.db.set_state(self._LINK_SLOT_NAME_KEY, str(name)[:80])

    def link_role(self) -> str:
        """active | standby | "" — сервер не сообщал (канала нет)."""
        return (self.db.get_state(self._LINK_ROLE_KEY) or "").strip()

    def link_standby_known(self):
        """True — другой шлюз есть, False — нет, None — сервер не говорил."""
        raw = self.db.get_state(self._LINK_STANDBY_KEY)
        return None if raw is None or raw == "" else raw == "1"

    def link_server_bot(self) -> dict:
        """{'username'} бота сервера AWG из `role`; сервер не говорил — пусто."""
        user = self.db.get_state(self._LINK_SERVER_BOT_KEY) or ""
        return {"username": user} if user else {}

    def link_server_caps(self) -> list[str]:
        """Возможности сервера из его первого `role`; пусто — сервер прежний или не говорил."""
        return (self.db.get_state(self._LINK_SERVER_CAPS_KEY) or "").split()

    def server_version(self) -> str:
        """Версия сервера AWG из последнего `role`: «3.3.0»; «old» — сервер
        старше 3.3.0 (версию не шлёт); "" — `role` с версией ещё не приходил."""
        return (self.db.get_state(self._LINK_SERVER_VERSION_KEY) or "").strip()

    def lan_virtual(self) -> list[str]:
        """Адреса в туннеле своей сети по последнему `role`: как сеть видна
        снаружи шлюза — устройствам админа и подсетям других шлюзов. Пусто —
        сервер старше 3.3.0, подсетей нет или адреса в туннеле выключены."""
        return (self.db.get_state(self._LINK_LAN_VIRTUAL_KEY) or "").split()

    def link_slot_name(self) -> str:
        return (self.db.get_state(self._LINK_SLOT_NAME_KEY) or "").strip()

    def carries_traffic(self) -> bool:
        """Шлюз сейчас несёт трафик клиентов: сервер назначил его активным и
        линк жив по последнему снимку. Только тогда предупреждаем, что
        РФ-доступ прервётся."""
        if self.link_role() != "active":
            return False
        st = self.cached_status(900)
        return bool(st is not None and self.link_ok(st))

    def _rf_note(self, text: str) -> str:
        """Хвост «РФ-доступ … не работает» к алерту — только если шлюз нёс
        трафик и резерва, на который сервер мог переключиться, нет."""
        if self.link_role() == "active" and self.link_standby_known() is not True:
            return text
        return ""
