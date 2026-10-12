"""sources.py — часть пакета reconcile (см. __init__): источники списков —
учёт исходов обращений, доклады о смене состояния, обновление и сводка."""

from __future__ import annotations

import hashlib
import time
from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import routing
from awgbot.domain import routing as domain_routing
from awgbot.domain.services.types import Notification
from awgbot.domain.services.base import _e
from ..common import log, _TXT_RT_SRC_STALE, _TXT_RT_SRC_DOWN, _TXT_RT_SRC_GONE, _TXT_RT_SRC_OK


class RoutingSourcesMixin:
    """Источники списков: исходы, доклады, обновление."""
    # ── источники списков: замечать, когда источник перестал отдавать ────────
    # Кэш переживает недоступность источника намеренно — устаревшие списки лучше
    # пустых. Но у этого есть оборотная сторона: источник может замолчать
    # навсегда (переехал, переименовали файл, репозиторий забросили), а кэш
    # останется прежним и никто не узнает. routing_lists_ready смотрит на
    # непустоту, а не на свежесть, поэтому такой отказ не виден вообще ничем.
    _RT_SRC_N = "rt_src_n:"          # последнее НЕнулевое число записей
    _RT_SRC_BAD = "rt_src_bad2:"     # подтверждённая беда: "" | "down" | "gone" | "empty"
    _RT_SRC_ERR = "rt_src_err:"      # текст ошибки — половина диагноза
    _RT_SRC_FAILS = "rt_src_fails:"  # неудач подряд, для добора попыток
    _RT_SRC_SAID = "rt_src_said:"    # о какой беде уже доложили

    # Тревога не по первой неудаче: сеть моргает, а GitHub отдаёт 429 на минуты.
    # Доклад по одному промаху приучил бы не читать эти сообщения ровно к тому
    # разу, когда источник умер по-настоящему.
    _RT_SRC_TRIES = 4                # первая попытка и три добора
    _RT_SRC_RETRY_SECS = 300         # пауза между ними

    @staticmethod
    def _routing_src_key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:12]

    def _routing_note_source(self, url: str, count: int, err: str = "",
                             code: int = 200) -> bool:
        """Запомнить исход обращения к источнику. True — нужен скорый повтор.

        Исходов четыре, и чинятся они в разных местах: отдал записи; не ответил
        (наша связь, файл при этом может быть цел); ответил 404 (файл переехал
        или удалён); ответил 200, но разбирать нечего (сменился формат). Прежде
        все, кроме первого, приходили сюда одинаковым нулём, и доклад звал
        искать переехавший файл там, где до файла попросту не дошли.

        Пустой ответ добором попыток не проверяем: он не про связь, и следующая
        попытка вернёт ровно то же самое.
        """
        k = self._routing_src_key(url)
        if count:
            self.db.set_state(self._RT_SRC_N + k, str(count))
            self.db.set_state(self._RT_SRC_FAILS + k, "0")
            self.db.set_state(self._RT_SRC_BAD + k, "")
            return False
        if not self.db.get_state(self._RT_SRC_N + k):
            return False              # ни разу не отдавал — сравнивать не с чем
        if not err:
            self.db.set_state(self._RT_SRC_BAD + k, "empty")
            return False
        fails = int(self.db.get_state(self._RT_SRC_FAILS + k) or 0) + 1
        self.db.set_state(self._RT_SRC_FAILS + k, str(fails))
        self.db.set_state(self._RT_SRC_ERR + k, err)
        if fails < self._RT_SRC_TRIES:
            return True               # рано тревожить — добираем попытки
        # 404/410 добором не лечится, но и он его проходит: правило одно на все
        # не-двухсотые, а разделяем их только в докладе.
        self.db.set_state(self._RT_SRC_BAD + k, "gone" if code in (404, 410) else "down")
        return False

    def _routing_src_state(self, k: str) -> str:
        """Что с источником сейчас. Пока доборы не исчерпаны — прежнее значение:
        неподтверждённая неудача не считается ни бедой, ни выздоровлением."""
        return self.db.get_state(self._RT_SRC_BAD + k) or ""

    def _routing_src_said(self, k: str) -> str:
        """О чём по этому источнику уже доложено."""
        return self.db.get_state(self._RT_SRC_SAID + k) or ""


    def routing_source_alerts(self) -> list[Notification]:
        """Смена состояния источника — доклад. Один на смену, не на тик.

        Докладываем и о восстановлении: молчащий источник не ломает
        маршрутизацию сегодня, поэтому увидеть своими глазами, что списки снова
        обновляются, неоткуда — как и понять, чинить ли ещё. Пока в боте были
        одни тревоги, разошедшийся сам собой лимит запросов оставался бы висеть
        нерешённым делом.

        Переход down→empty (или обратно) тоже доклад: диагноз сменился, а с ним
        и место, куда идти чинить.
        """
        notes: list[Notification] = []
        for url in config.ROUTING_LISTS_HOME_URLS:
            if not url:
                continue
            k = self._routing_src_key(url)
            state = self._routing_src_state(k)
            if state == self._routing_src_said(k):
                continue
            self.db.set_state(self._RT_SRC_SAID + k, state)
            n = self.db.get_state(self._RT_SRC_N + k) or "?"
            err = _e(self.db.get_state(self._RT_SRC_ERR + k) or "?")
            if not state:
                text = _TXT_RT_SRC_OK.format(url=_e(url), n=n)
            elif state == "down":
                text = _TXT_RT_SRC_DOWN.format(url=_e(url), err=err,
                                               tries=self._RT_SRC_TRIES)
            elif state == "gone":
                text = _TXT_RT_SRC_GONE.format(url=_e(url), err=err,
                                               tries=self._RT_SRC_TRIES)
            else:
                text = _TXT_RT_SRC_STALE.format(url=_e(url), n=n)
            notes.append(Notification(config.ADMIN_ID, text))
        return notes

    def routing_update_lists(self, force: bool = False) -> int:
        """Обновить базовый набор из внешних источников. Возвращает число записей.

        Зовётся ботом самостоятельно — при старте и по расписанию. Руками
        запускать ничего не нужно: требовать этого от админа значит гарантировать,
        что однажды забудут, а без списков режим не действует вовсе — человек
        включит тумблер и не получит ничего.

        Недоступность источника не считается ошибкой: прежний набор остаётся в
        силе. Застывший список всё ещё покрывает большинство сервисов, пустой не
        покрывает ни одного.
        """
        # Гейт НАМЕРЕННО не routing.available(): полная самопроверка требует
        # рабочего окружения, а наполнение списков — это как раз то, чем оно
        # становится рабочим. Проверять её здесь значило бы получить
        # взаимоблокировку: набор пуст → «недоступна» → наполнять не идём →
        # набор пуст. Достаточно того, что функция вообще включена.
        if not force:
            if not config.ROUTING_ENABLED:
                return 0
            if not settings.get_bool("app.routing.enabled", False):
                return 0
        now = int(time.time())
        every = int(settings.get("app.routing.lists_refresh_hours", 6)) * 3600
        if not force:
            cached = len(self._routing_read_cache("tun_domains"))
            # по расписанию ИЛИ немедленно, если кэш пуст: без списков режим не
            # действует вовсе, и ждать следующего окна незачем — в том числе на
            # старте бота, где эта же ветка и срабатывает. Момент СЛЕДУЮЩЕГО
            # похода — с джиттером ±40 %: «шесть часов с прошлого» внутри тика
            # давало метроном (правило проекта: сетевые задачи не по часам).
            nxt = self.db.get_state(self._RT_LISTS_NEXT_KEY) or ""
            last = self.db.get_state(self._RT_LISTS_KEY)
            if not nxt.isdigit() and last:
                nxt = str(int(last) + every)               # прежняя схема: один раз без джиттера
            if nxt.isdigit() and now < int(nxt) and cached > 0:
                return cached
        import random
        self.db.set_state(self._RT_LISTS_NEXT_KEY, str(now + int(every * random.uniform(0.6, 1.4))))
        try:
            # СКАЧИВАНИЕ — СНАРУЖИ ЗАМКА. Источники с таймаутом по 15 с, а под
            # замком ждёт тик живости: держать его на это время значило бы
            # менять один отказ на другой. Замок берём только на запись.
            # Российские сервисы, которым нужен российский адрес. Списки
            # заграницы (блокировки, геоблок, подсети CDN) ушли вместе с
            # обратной моделью: набор перечисляет то, что идёт на шлюз, а туда
            # заграница не ходит по определению.
            home: list[str] = []
            retry = False
            for url in config.ROUTING_LISTS_HOME_URLS:
                if not url:
                    continue
                body, err, code = routing.fetch(url)
                got = domain_routing.parse_domain_list(body) if body else []
                retry |= self._routing_note_source(url, len(got), err, code)
                home.extend(got)

            with routing.mutation_lock:
                if home:
                    self._routing_write_cache("tun_domains", sorted(set(home)))
                size = len(self._routing_read_cache("tun_domains"))
                # Неудача не должна съедать окно целиком: 429 живёт минуты, а
                # окно — часы, и следующая попытка пришлась бы на давно
                # разошедшийся лимит. Пока доборы не исчерпаны, метку сдвигаем
                # так, чтобы гейт открылся через паузу добора.
                self.db.set_state(self._RT_LISTS_KEY, str(
                    now - every + min(every, self._RT_SRC_RETRY_SECS) if retry else now))
                if retry:
                    # гейт читает момент следующего похода, не метку: без этого
                    # сдвиг метки ничего не открывал, и добор ждал полное окно
                    self.db.set_state(self._RT_LISTS_NEXT_KEY, str(now + self._RT_SRC_RETRY_SECS))
                # окружение изменилось нашими руками — прежний вердикт
                # самопроверки протух
                routing.invalidate_self_check()
                log.info("routing: списки обновлены, в базовом наборе %d записей", size)
                return size
        except routing.RoutingError as e:
            log.warning("routing_update_lists: %s", e)
            return len(self._routing_read_cache("tun_domains"))

    def routing_lists_info(self) -> dict:
        """Состояние списков для чата: сколько записей, когда обновлялись,
        период. Обновление — молчаливый процесс на тике монитора, и без этой
        сводки админ не отличит «списки свежие» от «источники умерли полгода
        назад, кэш застыл»."""
        raw = self.db.get_state(self._RT_LISTS_KEY)
        updated_at = int(raw) if raw and raw.isdigit() else None
        return {
            "count": len(self._routing_read_cache("tun_domains")),
            "updated_at": updated_at,
            "age_seconds": (int(time.time()) - updated_at) if updated_at else None,
            "every_hours": int(settings.get("app.routing.lists_refresh_hours", 6)),
            "sources": len([u for u in config.ROUTING_LISTS_HOME_URLS if u]),
        }
