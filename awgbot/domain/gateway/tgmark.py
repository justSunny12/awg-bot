"""tgmark.py — путь к Telegram и GitHub: маркировка диапазонов, подсети соседей."""

from __future__ import annotations

from awgbot.domain.gateway.base import GwCheck


class TgMarkMixin:
    """Путь к Telegram и GitHub: маркировка диапазонов, подсети соседей."""
    # ── путь к Telegram: маркировка диапазонов (этап 2) ──────────────────────

    # Диапазоны Telegram (AS62014/62041/59930/44907) — стабильны годами. Трафик
    # к ним метится 0x1 и уходит в туннель до ВПС по уже существующей политике
    # шлюза (fwmark 0x1 → table 100 → awg0): без этого агент нем — Telegram в
    # юрисдикции шлюза заблокирован. Персист — в PostUp клиентского конфига
    # шлюза; агент лишь реассертит недостающее: правило идемпотентно, и
    # автоматика здесь хуже не сделает.
    TG_RANGES = ("91.108.4.0/22", "91.108.8.0/22", "91.108.12.0/22",
                 "91.108.16.0/22", "91.108.20.0/22", "91.108.56.0/22",
                 "149.154.160.0/20", "185.76.151.0/24")
    # GitHub той же меткой в аплинк: с него агент обновляется, а в юрисдикции
    # шлюза он без туннеля недоступен. Список — как в routing-gw-setup.sh.
    GH_RANGES = ("140.82.112.0/20", "143.55.64.0/20", "185.199.108.0/22", "192.30.252.0/22")

    _guard_info: dict | None = None
    _REASSERT_MIN_INTERVAL = 10 * 60
    _last_reassert: float = float("-inf")   # monotonic: 0.0 на свежезагруженной малине откладывал бы первый реассерт

    def tg_mark_missing(self, info: dict | None = None) -> list[str]:
        """Диапазоны Telegram, которых нет в set tg_nets4 таблицы. Таблица —
        одно целое: пропали диапазоны — значит пропала (или устарела) вся она."""
        if info is None:
            from awgbot.infra import gwguard
            try:
                info = gwguard.table_info()
            except gwguard.GwGuardError:
                info = None
        if info is None:
            return list(self.TG_RANGES)
        present = info["sets"].get("tg_nets4", set())
        return [n for n in self.TG_RANGES if n not in present]

    def gh_route_check(self, info: dict | None) -> GwCheck:
        """Отдельно от Telegram: реассерт тут не поможет — старый скрипт
        обвязки набора gh_nets4 не знает, нужен новый файл конфигурации с ВПС."""
        present = (info or {}).get("sets", {}).get("gh_nets4", set())
        missing = [n for n in self.GH_RANGES if n not in present]
        return GwCheck("маршрут к GitHub", not missing,
                       "" if not missing else
                       "обвязка без маршрута GitHub в аплинк — агент не сможет обновляться; "
                       "перевыпусти конфигурацию шлюза с сервера AWG")

    @staticmethod
    def peer_nets_missing(info: dict | None) -> list[str] | None:
        """Чего из PEER_HOME_NETS нет в наборе peer_nets4. None — переменная
        пуста, функции на этом шлюзе нет. Отдельно от проверки, потому что тот
        же ответ нужен снимку для канала — но строкой для человека, собранной
        здесь, там делать нечего."""
        from awgbot.infra import gwguard
        want = gwguard.unit_env("PEER_HOME_NETS").split()
        if not want:
            return None
        present = (info or {}).get("sets", {}).get("peer_nets4", set())
        return [n for n in want if n not in present]

    def peer_nets_check(self, info: dict | None) -> GwCheck | None:
        """Подсети за другими шлюзами: набор
        peer_nets4 против PEER_HOME_NETS из юнита. Переменная пустая — проверки
        нет: функции на этом шлюзе нет."""
        missing = self.peer_nets_missing(info)
        if missing is None:
            return None
        return GwCheck("локальные подсети других шлюзов", not missing,
                       "" if not missing else
                       f"в таблице нет {', '.join(missing)}; перевыпусти конфигурацию шлюза с сервера AWG")

    def tg_mark_ensure(self, missing: list[str] | None = None) -> int:
        """Таблицу правит только скрипт: недостающее восстанавливаем рестартом
        юнита (скрипт идемпотентен, линк без нужды не трогает). Не чаще раза в
        10 минут — иначе устаревший бандл дёргал бы юнит каждый тик."""
        if missing is None:
            missing = self.tg_mark_missing()
        if not missing:
            return 0
        ok = self._reassert_throttled(f"не хватало {len(missing)} диапазонов Telegram")
        return len(missing) if ok else 0
