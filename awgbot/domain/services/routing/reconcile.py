"""reconcile.py — реконсиляция таблиц и источники списков."""

from __future__ import annotations

import hashlib
import time
from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import routing
from awgbot.domain import routing as domain_routing
from awgbot.domain.services.types import Notification
from awgbot.domain.services.base import _e
from awgbot.domain.evidence import EvidenceProbe
from .common import log, _TXT_RT_INFRA_BAD, _TXT_RT_SRC_STALE, _TXT_RT_SRC_DOWN, _TXT_RT_SRC_GONE, _TXT_RT_SRC_OK


class RoutingReconcileMixin:
    """Реконсиляция таблиц и источники списков."""
    # ── Реконсиляция и мониторинг ────────────────────────────────────────────

    def reconcile_routing(self) -> None:
        """Привести инфраструктуру к состоянию БД. Идемпотентно.

        Единственная точка, где состояние фичи проецируется наружу: и тумблеры,
        и правки списков зовут её же. Дублировать «точечные» обновления рядом с
        полной сверкой значило бы завести второй путь, который однажды разойдётся
        с первым.
        """
        if not routing.available():          # инфраструктуры нет — трогать нечего
            return
        try:
            with routing.mutation_lock:
                if not settings.get_bool("app.routing.enabled", False):
                    self._routing_stand_down()
                else:
                    self._routing_apply()
        except routing.RoutingError as e:
            # Не только в лог. Сюда прилетает и отказ рестарта dnsmasq, а это не
            # «маршрутизация не применилась», а «резолвер лежит» — то есть у
            # ВСЕХ клиентов нет DNS. Бот при этом продолжал бы работать, считая
            # фичу живой, и сказать об этом было некому: журнал на сервере
            # читают, когда уже пришли разбираться.
            log.warning("reconcile_routing: %s", e)
            self.db.set_state(self._RT_INFRA_BAD, str(e)[:300])
            return
        self.db.set_state(self._RT_INFRA_BAD, "")

    def _routing_stand_down(self) -> None:
        """Снять всё, что фича делает с трафиком.

        Не «просто выйти»: выключатель обязан выключать уже размеченный трафик,
        а не только запрещать новые включения. Состояние в БД при этом цело —
        вернули условия, и следующая же реконсиляция всё восстановит.

        Единственный повод сюда попасть — выключение функции админом: пустой
        набор профилей равносилен выключенной функции сам по себе, и гасить
        ради него нечего.
        """
        routing.sync_nat_exempt(())
        routing.rebuild_chain(())
        routing.set_marking_enabled(False)

    def _routing_apply(self) -> None:
        """Разложить состояние БД по наборам, цепочке и конфигу dnsmasq.

        ДВА СОСТАВА ПРОФИЛЕЙ, и это главное здесь.

        `active` — чей трафик метить прямо сейчас. Меняется от каждого нажатия
        тумблера пользователем, и всё, что от него зависит, обязано быть
        дешёвым: членство в ipset и правила в своей цепочке.

        `known` — у кого вообще есть свой набор: кому админ разрешил функцию,
        плюс те, у кого есть личный список. Меняется только решением админа. На
        нём держится конфиг dnsmasq — потому что его применение стоит РЕСТАРТА
        РЕЗОЛВЕРА, а рестарт роняет кэш и на секунды лишает DNS всех клиентов
        разом, включая тех, кто ничего не переключал.

        Пока конфиг зависел от `active`, каждое нажатие тумблера любым
        пользователем переписывало все ~600 строк и перезапускало dnsmasq всем.
        Снаружи это выглядит как «включил режим — на минуту всё отвалилось», а
        для того, кто в этот момент резолвил имя, — как отказ на ровном месте.

        Работает разделение потому, что правило маркировки требует ОБОИХ
        совпадений: источник в `rt_src_u<N>` И назначение в `vpn_u<N>`. У
        выключенного профиля src-набор пуст, поэтому его набор назначений может
        спокойно наполняться — метить всё равно нечего. Побочная выгода: к
        моменту включения набор уже прогрет, и режим работает с первой секунды,
        не дожидаясь, пока клиент переспросит DNS.
        """
        addrs = self.db.routing_active_addresses(config.ADMIN_ID)
        domains = self.db.routing_domains_by_client()
        active_ids = sorted(set(addrs) | set(domains))
        known_ids = sorted(set(self.db.routing_allowed_client_ids(config.ADMIN_ID))
                           | set(domains))
        # Набор означает ДОМОЙ и наполняется только доменами: все скачиваемые
        # списки подсетей были про заграницу (Cloudflare, Google, Telegram) и
        # ушли вместе с обратной моделью. Российских подсетей сопровождаемого
        # источника не существует, поэтому здесь их нет.
        base_domains = list(self._routing_read_cache("tun_domains"))

        # плечо контейнера: выпустить трафик включённых устройств
        # немаскараженным, иначе на хосте их не отличить от остальных
        routing.sync_nat_exempt([a for lst in addrs.values() for a in lst])

        # ДИФФ ПЕРЕД ЗАПИСЬЮ. Реконсиляция идёт каждый тик монитора, и раньше
        # она каждый раз пересобирала все наборы (6 exec на профиль) и цепочку
        # (флаш + правило на профиль) — ~24 000 exec в сутки ради состояния,
        # которое меняется нажатием тумблера. Один `ipset save` даёт состав
        # всех наборов; пишем только те, что разошлись. Не удалось прочитать —
        # пересобираем всё, как прежде.
        live = routing.snapshot_sets(only=[routing.src_set(cid) for cid in known_ids])
        for cid in known_ids:
            # src-набор наш — перезаписываем целиком (у выключенного профиля он
            # станет пустым, и это ровно то, что нужно); набор назначений только
            # СОЗДАЁМ: наполняет его dnsmasq по мере резолва доменов, и любая
            # запись с нашей стороны стёрла бы накопленное
            src = routing.src_set(cid)
            routing.replace_members(src, "hash:ip", addrs.get(cid, ()),
                                    current=None if live is None else live.get(src))
            usr = routing.user_set(cid)
            routing.ensure_set(usr, "hash:net", exists=live is not None and usr in live)

        routing.rebuild_chain(active_ids)
        self._routing_drop_orphan_sets(known_ids, names=None if live is None else list(live))
        routing.write_dnsmasq_conf(domain_routing.build_dnsmasq_conf(
            base_domains=base_domains,
            domains_by_client=domains,
            client_ids=known_ids,
            set_user_prefix=config.ROUTING_SET_USER_PREFIX,
        ))

    def _routing_drop_orphan_sets(self, live_ids, names=None) -> None:
        """Снести наборы удалённых клиентов.

        Осиротевший набор сам по себе безвреден (правила на него уже нет), но
        накапливается и однажды совпадёт по имени с новым client_id — тогда
        чужие домены достанутся другому человеку. Ровно та же логика, по которой
        remove_device снимает осиротевший DROP.
        """
        live = {int(c) for c in live_ids}
        prefixes = (config.ROUTING_SET_USER_PREFIX, config.ROUTING_SET_SRC_PREFIX)
        for name in (routing.list_sets() if names is None else names):
            for pref in prefixes:
                if not name.startswith(pref) or name.endswith("_tmp"):
                    continue
                tail = name[len(pref):]
                if tail.isdigit() and int(tail) not in live:
                    routing.destroy_set(name)

    _RT_LISTS_KEY = "routing_lists_updated_at"

    # Скачанные списки лежат в КЭШЕ рядом с БД, а не сразу в наборах: наборы
    # пер-юзерные, их состав вычисляется при каждой реконсиляции, и держать
    # исходник отдельно от результата — единственный способ пересобрать состав
    # при появлении нового профиля, не выкачивая всё заново.
    def _routing_cache(self, kind: str):
        path = config.DATA_DIR / f"routing-{kind}.lst"
        if kind == "tun_domains" and not path.exists():
            # прежнее имя кэша — переносим, чтобы до первого обновления списков
            # туннель не остался без доменов
            old = config.DATA_DIR / "routing-home_domains.lst"
            try:
                if old.exists():
                    old.rename(path)
            except OSError as e:
                log.warning("routing: кэш %s не перенесён: %s", old, e)
        return path

    def _routing_write_cache(self, kind: str, items) -> None:
        path = self._routing_cache(kind)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text("\n".join(items) + "\n", encoding="utf-8")
            tmp.replace(path)                 # атомарно: без полуфайла
        except OSError as e:
            log.warning("routing: не записать кэш %s (%s)", kind, e)

    def _routing_read_cache(self, kind: str) -> list[str]:
        """Пусто — значит списки ещё не качали. Файл перечитывается только при
        смене mtime: раньше ~600 строк читались с диска каждый тик."""
        path = self._routing_cache(kind)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        mem = self.__dict__.setdefault("_routing_cache_mem", {})   # на экземпляр
        hit = mem.get(str(path))
        if hit and hit[0] == mtime:
            return list(hit[1])
        try:
            items = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        except OSError:
            return []
        mem[str(path)] = (mtime, items)
        return list(items)


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

    # Реконсиляция упала. Отдельный ключ, а не флаг рядом с источниками: там
    # «списки застыли», здесь «примениться не удалось», и чинятся они в разных
    # местах. Хранится текст ошибки — он же и есть половина диагноза.
    _RT_INFRA_BAD = "rt_infra_bad"
    _RT_INFRA_ANNOUNCED = "rt_infra_announced"

    def routing_infra_alerts(self) -> list[Notification]:
        """Реконсиляция маршрутизации падает — сказать админу. Один доклад.

        Главный случай — не поднявшийся после правки конфига dnsmasq: у клиентов
        при этом умирает DNS целиком, а внешне это «интернет работает через раз»,
        потому что всё уже отрезолвленное продолжает ходить. Связать такое с
        маршрутизацией без подсказки почти невозможно.
        """
        err = self.db.get_state(self._RT_INFRA_BAD) or ""
        announced = self.db.get_state(self._RT_INFRA_ANNOUNCED) == "1"
        if not err:
            if not announced:
                return []
            self.db.set_state(self._RT_INFRA_ANNOUNCED, "0")
            return [Notification(config.ADMIN_ID,
                                 "🟢 РФ-доступ снова работает")]
        if announced:
            return []
        self.db.set_state(self._RT_INFRA_ANNOUNCED, "1")
        return [Notification(config.ADMIN_ID, _TXT_RT_INFRA_BAD.format(err=_e(err)), critical=True)]

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

    def routing_link_ok(self) -> bool:
        """Проходит ли трафик через шлюз — по последнему замеру зонда.

        Читаем сохранённый результат, а не зондируем на месте: метод дёргают
        экраны и preflight, а зонд — это пинги с таймаутами, им в отрисовке
        интерфейса не место. Замер делает routing_liveness_tick.
        """
        return self.db.get_state(self._RT_LINK_KEY) != "0"

    def routing_engaged(self) -> bool:
        """Должна ли маркировка быть включена в принципе — до вопроса о живости.

        Один предикат на всех, кто трогает рубильник. Реконсиляция и тик живости
        уже однажды разошлись во мнениях и спорили за него: один снимал политику,
        другой немедленно возвращал. Пока условие живёт в одном месте, разойтись
        им негде. routing.available() — это здоровье ОБВЯЗА, а не выключатель
        фичи; их легко перепутать, и тут они сведены явно.
        """
        return (routing.available()
                and settings.get_bool("app.routing.enabled", False))

    def routing_probe(self) -> str:
        """Замер прямо сейчас — АКТИВНОГО шлюза, тем путём, которым ходят
        клиенты (метка фичи, таблица фичи). Отдельно от routing_link_ok, чтобы
        было видно, где реальные пинги, а где чтение кэша."""
        targets, port = self._rt_probe_targets()
        active = self.active_gateway()
        return routing.probe_gateway(list(targets), port,
                                     iface=active.link_if if active else "")

    @staticmethod
    def _rt_probe_targets() -> tuple[list, int]:
        # Две цели по умолчанию: один внешний хост — сам по себе точка отказа,
        # и его заминка выглядела бы как отвал шлюза.
        targets = settings.get("app.routing.probe_targets", None) or ["77.88.8.8", "8.8.8.8"]
        port = int(settings.get("app.routing.probe_port", 53))
        return list(targets), port

    # Зонд резерва: те же цели и две попытки, но короче таймаут — резерв не
    # держит трафик, и его ответ ничего не откладывает.
    _RT_STANDBY_TIMEOUT = 2.0
    # Свежесть хендшейка резерва: keepalive линка — диапазон из client_config
    # (по умолчанию 25–35 с, как у клиентских пиров), хендшейк раз в ~2 мин.
    _RT_STANDBY_HANDSHAKE_MAX = 180

    def _probe_slot(self, gw, *, active: bool) -> str:
        targets, port = self._rt_probe_targets()
        if active:
            return routing.probe_gateway(targets, port, iface=gw.link_if)
        return routing.probe_gateway(targets, port, timeout=self._RT_STANDBY_TIMEOUT,
                                     iface=gw.link_if, mark=gw.mark)

    def _rt_standby_interval(self) -> int:
        """Минут между зондами резерва; 0 — каждый такт (тесты)."""
        return settings.get_int("app.routing.failover.standby_probe_minutes", 5)

    def _standby_verdict(self, gw) -> str:
        """Живость РЕЗЕРВА без маячка каждые полминуты: наружу зондируем редко и
        с джиттером (строго периодический коннект с домашнего адреса — сигнатура
        для ТСПУ), а между зондами живость — по свежести хендшейка линка (это
        локальный exec, наружу ничего не уходит). Резерв «в порядке» = последний
        зонд прошёл И хендшейк свежий: такой шлюз примет нагрузку."""
        import random
        import time as _time
        nxt = self.__dict__.setdefault("_rt_standby_next", {})
        last = self.__dict__.setdefault("_rt_standby_last", {})
        every = self._rt_standby_interval()
        now = _time.monotonic()
        if every <= 0 or gw.id not in last or now >= nxt.get(gw.id, 0.0):
            last[gw.id] = self._probe_slot(gw, active=False)
            nxt[gw.id] = now + every * 60 * random.uniform(0.6, 1.4)
        # Хендшейк — всегда, и в такт зонда тоже: пройденный зонд без свежего
        # хендшейка означает, что путь ушёл не через линк (обвязка слота не на
        # месте) — такой резерв нагрузку не примет.
        age = routing.link_handshake_age(gw.link_if)
        if age is None or age > self._RT_STANDBY_HANDSHAKE_MAX:
            return routing.PROBE_DOWN
        return last[gw.id]

    def _standby_forget(self, slot_id: int) -> None:
        """Забыть память зондов слота — и резервную, и активную: после смены
        роли или удаления слота кэшированный вердикт относится к другому миру."""
        self.__dict__.setdefault("_rt_standby_next", {}).pop(slot_id, None)
        self.__dict__.setdefault("_rt_standby_last", {}).pop(slot_id, None)
        self.__dict__.setdefault("_rt_active_probe", {}).pop(slot_id, None)

    # Рост rx линка за такт, который НЕ подделать служебным трафиком: keepalive
    # это 32 байта, хендшейк вместе с джанком AmneziaWG (Jc ≤ 10 пакетов по
    # 8–70 байт, S1/S2 ≤ 150) — меньше килобайта. Всё, что выше, прислал шлюз,
    # а прислать ему нечего, кроме обратного трафика клиентов.
    _RT_RETURN_BYTES = 4096

    def _rt_idle_probe_interval(self) -> float:
        """Секунд между зондами активного слота, когда через линк никто не
        ходит; 0 — каждый такт (тесты)."""
        base = settings.get_int("app.routing.probe_seconds", 30)
        return base * float(settings.get("app.routing.probe_idle_multiplier", 10) or 0)

    def _active_verdict(self, gw) -> str:
        """Живость АКТИВНОГО слота: сначала бесплатные улики, потом зонд
        (domain/evidence.EvidenceProbe — одна механика с агентом).

        Вернувшийся через линк трафик клиентов — прямое доказательство пути
        ВПС → линк → шлюз → интернет → обратно, и оно сильнее зонда: зонд
        проверяет одну цель на 53-м порту, а это настоящие сессии живых людей.
        Улика засчитывается только при живом хендшейке; между зондами живость
        держит свежесть хендшейка — это локальный exec, наружу не уходит ничего.
        """
        probes = self.__dict__.setdefault("_rt_active_probe", {})
        st = routing.link_peer_state(gw.link_if)
        if st is None:
            probes.pop(gw.id, None)
            return self._probe_slot(gw, active=True)
        ep = probes.get(gw.id)
        if ep is None:
            ep = probes[gw.id] = EvidenceProbe(self._RT_RETURN_BYTES)
        fresh = st["age"] is not None and st["age"] <= self._RT_STANDBY_HANDSHAKE_MAX
        result, src = ep.verdict(
            st["rx"], st["tx"], minus=lambda rx, tx: self.channel.minus(gw.id, rx, tx),
            probe=lambda: self._probe_slot(gw, active=True), every=self._rt_idle_probe_interval(),
            on_traffic=lambda _prev: routing.PROBE_OK, back_is_rx=True, fresh=fresh)
        if src == "cache" and not fresh:
            # кэшированный «в порядке» живёт ровно до тех пор, пока жив
            # хендшейк: молчащий шлюз обязан проявиться сам, без зонда
            return routing.PROBE_DOWN
        return result

    def _probe_slots(self, slots, active) -> dict:
        """Все слоты разом: зонды независимы, а последовательно при лежащем
        резерве такт держал бы поток ещё на 2 × 2 с."""
        if len(slots) == 1:
            g = slots[0]
            if active is not None and active.id == g.id:
                return {g.id: self._active_verdict(g)}
            return {g.id: self._standby_verdict(g)}
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(slots)) as pool:
            futs = {}
            for g in slots:
                if active is not None and active.id == g.id:
                    futs[g.id] = pool.submit(self._active_verdict, g)
                else:
                    futs[g.id] = pool.submit(self._standby_verdict, g)
            return {sid: f.result() for sid, f in futs.items()}
