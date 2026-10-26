"""tables.py — часть пакета reconcile (см. __init__): реконсиляция наборов,
цепочки и конфига dnsmasq, кэш скачанных списков, доклад о падении реконсиляции."""

from __future__ import annotations

from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import routing
from awgbot.domain import routing as domain_routing
from awgbot.domain.services.types import Notification
from awgbot.domain.services.base import _e
from ..common import log, _TXT_RT_INFRA_BAD


class RoutingTablesMixin:
    """Реконсиляция таблиц, кэш списков, доклад о падении реконсиляции."""
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
            # ВСЕХ пользователей нет DNS. Бот при этом продолжал бы работать, считая
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
        РЕЗОЛВЕРА, а рестарт роняет кэш и на секунды лишает DNS всех пользователей
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
        не дожидаясь, пока устройство переспросит DNS.
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
        """Снести наборы удалённых пользователей.

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
        смене mtime, inode или размера (одного mtime мало — на Linux две записи
        подряд его не различают): раньше ~600 строк читались с диска каждый тик."""
        path = self._routing_cache(kind)
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_ino, st.st_size)
        except OSError:
            return []
        mem = self.__dict__.setdefault("_routing_cache_mem", {})   # на экземпляр
        hit = mem.get(str(path))
        if hit and hit[0] == key:
            return list(hit[1])
        try:
            items = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        except OSError:
            return []
        mem[str(path)] = (key, items)
        return list(items)

    # Реконсиляция упала. Отдельный ключ, а не флаг рядом с источниками: там
    # «списки застыли», здесь «примениться не удалось», и чинятся они в разных
    # местах. Хранится текст ошибки — он же и есть половина диагноза.
    _RT_INFRA_BAD = "rt_infra_bad"
    _RT_INFRA_ANNOUNCED = "rt_infra_announced"

    def routing_infra_alerts(self) -> list[Notification]:
        """Реконсиляция маршрутизации падает — сказать админу. Один доклад.

        Главный случай — не поднявшийся после правки конфига dnsmasq: у пользователей
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
