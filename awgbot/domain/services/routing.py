"""
routing.py — условная маршрутизация (docs/conditional-routing.md):
разрешения, устройства субъекта, домены, реконсиляция, источники списков,
живость шлюза.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import routing
from awgbot.domain import routing as domain_routing
from awgbot.domain.services.types import Notification, RoutingAddResult, ServiceError
from awgbot.domain.services.base import _e


log = logging.getLogger("awgbot.services")


_TXT_RT_INFRA_BAD = (
    "🚨 Условная маршрутизация не применяется:\n<code>{err}</code>\n\n"
    "Если речь о dnsmasq — у клиентов сейчас нет DNS вообще, и выглядит это как "
    "«интернет работает через раз»: уже отрезолвленное ходит, новое — нет. "
    "Проверь <code>systemctl status dnsmasq</code> и "
    "<code>/etc/dnsmasq.d/awgbot-routing.conf</code>.")
_TXT_RT_SRC_STALE = (
    "⚠️ Источник списков маршрутизации замолчал:\n<code>{url}</code>\n\n"
    "Раньше он отдавал {n} записей, сейчас отвечает пустым. Прежний список "
    "продолжает работать, но обновляться перестал: новые заблокированные ресурсы "
    "в него уже не попадут. Проверь, не переехал ли файл.")
_TXT_RT_SRC_DOWN = (
    "⚠️ Источник списков маршрутизации не отвечает:\n<code>{url}</code>\n\n"
    "<code>{err}</code>\n\n"
    "Так ответили все {tries} попытки подряд. Файл может быть цел — до него не "
    "дошли мы. Прежний список продолжает работать, но обновляться перестал: "
    "новые заблокированные ресурсы в него уже не попадут. Лимит запросов и "
    "таймаут обычно проходят сами; скажу, когда источник ответит снова.")
_TXT_RT_SRC_GONE = (
    "⚠️ Источника списков маршрутизации нет по адресу:\n<code>{url}</code>\n\n"
    "<code>{err}</code>\n\n"
    "Так ответили все {tries} попытки подряд — файл переехал или удалён, само "
    "это не пройдёт. Прежний список продолжает работать, но обновляться "
    "перестал: новые заблокированные ресурсы в него уже не попадут. Новый адрес "
    "прописывается в <code>app.routing.lists_home_urls</code>.")
_TXT_RT_SRC_OK = (
    "🟢 Источник списков маршрутизации снова отдаёт данные:\n<code>{url}</code>\n\n"
    "Записей в ответе: {n}. Списки опять обновляются.")


class RoutingMixin:
    # ── Условная маршрутизация (docs/conditional-routing.md) ─────────────────
    # Российский IP для российских сервисов. Конфиги устройств не меняются:
    # режим — серверное состояние. Проекция состояния в инфраструктуру ровно
    # одна — членство адреса устройства в наборе ipset, поэтому переключение
    # стоит одну запись и обратимо без последствий для выданных ссылок.

    _RT_LINK_KEY = "routing_link_ok"
    _RT_STREAK_KEY = "routing_link_up_streak"
    _RT_UP_STREAK = 3                     # хороших замеров подряд до возврата
    # Плохих замеров подряд до ГАШЕНИЯ маркировки. Три — столько же, сколько на
    # возврат, и одинаково в обоих режимах: решение принято сознательно, ради
    # того чтобы мелкие сетевые флуктуации не дёргали режим туда-сюда. Каждое
    # переключение перекладывает трафик всех включённых, и на коротком провале
    # это дороже самого провала.
    #
    # Цена названа и принята: при умолчании «домой» помечено почти всё, поэтому
    # пока порог набирается, помеченный трафик уходит в тоннель, который никуда
    # не ведёт. Это не «не тот адрес», а отсутствие связи — до полутора минут в
    # худшем случае. Раньше там гасили по первому замеру именно из-за этого.
    #
    # Одиночный плохой замер и так не шум: внутри него зонд делает две попытки к
    # двум целям с таймаутом 4 с, то есть подтверждает отказ секундами. Три
    # замера — это уже около минуты подтверждённой недоступности.
    #
    # Совпадает с порогом объявления, и это удобно: админ узнаёт ровно тогда,
    # когда состояние действительно сменилось, а не до или после.
    _RT_DOWN_STREAK = 3
    _RT_DOWN_KEY = "routing_link_down_streak"
    _RT_ANNOUNCED_KEY = "routing_link_announced"
    _RT_ANNOUNCE_AFTER = 3                # плохих замеров подряд до письма админу
    # Подсказка про бандл — не украшение. Обфускация линка симметрична: не сойдись
    # H1..H4/S1..S4 у сторон, хендшейка не будет вовсе. Отказ громкий (вот эта
    # самая тревога), но причина со стороны ВПС не видна, и без строки ниже её
    # ищут в аплинке и NAT, где её нет.
    _TXT_RT_BUNDLE_HINT = (
        "\n\nЕсли началось сразу после обновления — пересобери бандл шлюза "
        "(<code>awg-bot gw-bundle</code>) и переустанови его на той стороне: "
        "набор обфускации линка обязан совпадать, иначе хендшейк не проходит.")

    @staticmethod
    def _rt_effect_line() -> str:
        """Что именно почувствуют пользователи, пока маркировка снята.

        Отказ мягкий: через шлюз идут только российские сервисы, всё прочее и так
        шло мимо. Раньше, в упразднённой обратной модели, тот же отвал означал
        «у людей пропал интернет» — и текст был другой.
        """
        return ("Маркировка снята: российские сервисы временно открываются "
                "с зарубежного адреса и могут ругаться. Всё остальное и так "
                "шло мимо шлюза — на него это не влияет.")

    def _txt_rt_gw_down(self) -> str:
        return ("🔴 Шлюз условной маршрутизации недоступен. "
                + self._rt_effect_line() + self._TXT_RT_BUNDLE_HINT)

    def _txt_rt_gw_no_path(self) -> str:
        return ("🔴 Шлюз условной маршрутизации отвечает, но интернета за ним нет "
                "— проверь аплинк и NAT на самом шлюзе. " + self._rt_effect_line())

    _TXT_RT_GW_UP = "🟢 Шлюз условной маршрутизации снова в строю."

    _RT_LINK_IF = "awglink"

    def routing_provisioned(self) -> bool:
        """Обвязка развёрнута? Признак — заданный интерфейс линка в конфиге."""
        return bool(settings.get("app.routing.gw_interface", config.ROUTING_GW_INTERFACE))

    def routing_provision(self) -> str:
        """Развернуть обвязку условной маршрутизации на ВПС и поднять линк.

        Раньше это были три команды в SSH из README: поставить dnsmasq,
        прогнать routing-host-setup.sh, прогнать routing-link-setup.sh, потом
        руками вписать интерфейс в app.yaml. Каждая — с ключами, которые легко
        перепутать, и ни одна не проверяла, что предыдущая отработала.

        Возвращает хвост вывода для показа админу. Бросает ServiceError, если
        какой-то шаг не отработал: полуразвёрнутая обвязка хуже отсутствующей —
        она выглядит рабочей.
        """
        import subprocess
        base = config.BASE_DIR / "install"
        out: list[str] = []

        def run(argv: list[str], what: str, timeout: int = 600) -> None:
            try:
                proc = subprocess.run(argv, capture_output=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError) as e:
                raise ServiceError(f"{what}: не запустилось ({e})")
            text = (proc.stdout + proc.stderr).decode(errors="replace").strip()
            out.append(text)
            if proc.returncode != 0:
                tail = "\n".join(text.splitlines()[-6:])
                raise ServiceError(f"{what} не отработал:\n{tail}")

        # dnsmasq: пакет dnsmasq-base даёт только бинарь (его тянут libvirt и
        # соседи), а обвязке нужен ЮНИТ — иначе routing-host-setup.sh честно
        # остановится на полпути.
        has_unit = subprocess.run(["systemctl", "list-unit-files", "dnsmasq.service"],
                                  capture_output=True)
        if has_unit.returncode != 0 or b"dnsmasq.service" not in has_unit.stdout:
            run(["apt-get", "install", "-y", "--no-install-recommends", "dnsmasq"],
                "установка dnsmasq")
        run(["sh", str(base / "routing-host-setup.sh"), "--apply"], "обвязка хоста")
        run(["sh", str(base / "routing-host-setup.sh"), "--install-unit"],
            "закрепление обвязки от ребута")
        run(["sh", str(base / "routing-link-setup.sh"), "--apply"], "линк до шлюза")
        settings.set_value("app.routing.gw_interface", self._RT_LINK_IF)
        # Включаем и саму функцию: разворачивать обвязку и оставить тумблер
        # выключенным значило бы спрятать «🛰 Назначить шлюз» — кнопку, на
        # которую отправляет итоговое сообщение. До назначения шлюза включённая
        # функция ничего не меняет: маркировать трафик некуда.
        settings.set_value("app.routing.enabled", True)
        routing.invalidate_self_check()
        log.info("условная маршрутизация: обвязка развёрнута, линк %s", self._RT_LINK_IF)
        return "\n".join(out[-1:])[-1500:]

    def routing_status(self) -> tuple[bool, str]:
        """(работоспособна ли фича, причина) — для preflight и админ-UI."""
        return routing.self_check()

    def routing_available(self) -> bool:
        """Функция работоспособна И включена админом.

        Два слоя намеренно разные по природе: инфраструктурный (self_check —
        есть ли чем маршрутизировать) холодный, а выключатель в настройках
        горячий. Первый отвечает «можно ли», второй — «нужно ли»."""
        return bool(settings.get_bool("app.routing.enabled", False)
                    and routing.available())

    def routing_grantable_clients(self) -> list:
        """Профили, которым можно выдать РФ-доступ, — для экрана настроек.

        Служебный профиль исключён (у него нет владельца), админский тоже:
        разрешение у него по умолчанию, и строка в списке предлагала бы выдать
        то, что и так есть."""
        return self.db.list_clients(exclude_tg=config.ADMIN_ID)

    def routing_allowed_for(self, client) -> bool:
        """Разрешён ли клиенту РФ-доступ.

        Админу — всегда: разрешение выдаёт он сам, и заставлять его сначала
        отмечать галочку себе бессмысленно. Остальным — по флагу, который админ
        ставит в их профиле.
        """
        if client is None:
            return False
        if client.tg_id and client.tg_id == config.ADMIN_ID:
            return True
        if client.routing_allowed:
            return True
        # держатель чужих устройств: разрешение — у их владельца (docs/guest-role.md)
        for dev in self.db.list_held_devices(client.id):
            owner = self.db.get_client(dev.client_id)
            if owner is not None and (owner.routing_allowed or owner.tg_id == config.ADMIN_ID):
                return True
        return False

    def routing_client_visible(self, client) -> bool:
        """Показывать ли фичу клиенту вообще. Пока админ не выдал разрешение,
        она невидима: иначе каждый первый пойдёт спрашивать, что это за пункт."""
        return bool(self.routing_allowed_for(client) and self.routing_available())

    def routing_device_counts(self, client_id: int) -> tuple[int, int]:
        """(включено, всего) — и заголовок кнопки, и состояние профиля разом.
        По устройствам СУБЪЕКТА с разрешённым РФ-доступом: свои непереданные и
        удерживаемые."""
        return self.db.routing_device_counts(client_id, config.ADMIN_ID)

    def routing_devices(self, client_id: int) -> list:
        """Устройства субъекта для экрана переключателей: свои, затем
        удерживаемые чужие."""
        return self.db.list_routing_devices(client_id, config.ADMIN_ID)

    def routing_lent_out(self, client_id: int) -> list:
        """Свои устройства, переданные другим: в разделе видны без
        переключателя — управляет держатель."""
        return self.db.list_lent_out_devices(client_id)

    def routing_profile_on(self, client_id: int) -> bool:
        """Режим у профиля включён ⇔ включён хоть на одном устройстве.

        ВЫВОДИМ, а не храним. Отдельная колонка на профиле существовала и была
        убрана: она обязана была совпадать с флагами устройств, а свестись
        обратно при расхождении ей было негде.
        """
        return self.db.routing_device_counts(client_id, config.ADMIN_ID)[0] > 0

    def routing_health_for_client(self, client) -> Optional[bool]:
        """Показывать ли клиенту статусную строку и что в ней.

        None — не показывать вовсе: админ функцию этому профилю не разрешил, и
        рассказывать про механизм тому, кому он недоступен, — шум.

        Разрешил — строка есть ВСЕГДА, в том числе когда сам режим у человека
        выключен. Она отвечает на вопрос «а работает ли оно вообще», который
        иначе задаётся заходом в раздел; знать это полезно как раз перед
        включением. Значение берём из кэша монитора, чтобы открытие меню не
        порождало сетевых вызовов.
        """
        if not self.routing_client_visible(client):
            return None
        return self.db.get_state(self._RT_LINK_KEY) != "0"

    def set_routing_allowed(self, client_id: int, allowed: bool) -> list["Notification"]:
        """Разрешение админа — верхний слой флага. Возвращает уведомление
        владельцу (0 или 1).

        Выдача включает режим на ВСЕХ устройствах профиля сразу: человеку
        обещано «функция включена для всех твоих устройств», и включать их
        руками после выдачи приходилось админу. Отзыв флаги устройств НЕ
        трогает: он гасит эффект, а не разрушает настройку.
        """
        client = self.db.get_client(client_id)
        if client is None:
            return []
        changed = bool(client.routing_allowed) != bool(allowed)
        self.db.update_client_fields(client_id, routing_allowed=1 if allowed else 0)
        if allowed:
            self.db.set_owner_devices_routing(client_id, True)   # и переданные тоже
        self.reconcile_routing()
        if not changed:
            return []
        from awgbot.bot import texts                   # ленивый, как в соседних миксинах
        notes: list[Notification] = []
        if client.tg_id:
            notes.append(Notification(client.tg_id, texts.ROUTING_GRANTED_NOTICE if allowed
                                      else texts.ROUTING_REVOKED_NOTICE))
        # держателям переданных устройств — то же, с оговоркой, от кого
        seen: set[int] = set()
        for dev in self.db.list_lent_out_devices(client_id):
            if dev.holder_tg_id and dev.holder_tg_id not in seen:
                seen.add(dev.holder_tg_id)
                notes.append(Notification(
                    dev.holder_tg_id,
                    texts.routing_granted_holder_notice(client) if allowed
                    else texts.routing_revoked_holder_notice(client)))
        return notes

    def set_routing_all(self, client_id: int, on: bool) -> int:
        """Массовое включение/выключение по всему профилю. Возвращает, сколько
        устройств изменилось.

        Досева при включении здесь нет. Он существовал для обратной модели, где
        набор означал «за границу»: без него сервис с закэшированным у клиента
        адресом уезжал на шлюз и получал отказ. В нынешней модели набор означает
        «домой», и промах даёт мягкий эффект — российский сервис просто
        продолжит ходить как ходил, пока кэш не истечёт. Резолвить ради этого
        шестьсот доменов по нажатию тумблера значило бы подвесить обработчик.
        """
        n = self.db.set_devices_routing(client_id, on)
        if n:
            self.reconcile_routing()
        return n

    def set_routing_device(self, device_id: int, on: bool) -> None:
        """Переключатель одного устройства. ПАРНЫЙ: в окне переезда человек
        видит и щёлкает двойника, а его реальный трафик до переимпорта идёт со
        СТАРОГО адреса. Тронь одну строку — и тумблер перестаёт делать что-либо:
        выключение не выключает, включение не включает, оба молча."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return
        for peer in self._device_pair(dev):
            self.db.update_device_fields(peer.id, routing_on=1 if on else 0)
        self.reconcile_routing()

    def toggle_routing_device(self, device_id: int) -> Optional[bool]:
        """Инвертировать флаг устройства. Возвращает новое состояние, None —
        устройства нет (колбэк из старого сообщения в истории чата)."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return None
        new_state = not bool(dev.routing_on)
        self.set_routing_device(device_id, new_state)
        return new_state

    # ── Личный список доменов ────────────────────────────────────────────────

    def routing_domains(self, client_id: int) -> list[str]:
        return self.db.list_routing_domains(client_id)

    def routing_add_domains(self, client_id: int, text: str) -> "RoutingAddResult":
        """Добавить домены пачкой. Возвращает разбор: что взято, что отброшено.

        Пользователь вставляет списком из мессенджера, и молча проглотить часть
        нельзя — он должен видеть причину по каждой строке, иначе решит, что
        кнопка сломана.
        """
        limit = int(settings.get("app.routing.user_domains_max", 100))
        accepted, rejected = domain_routing.parse_batch(
            text, denylist=config.routing_denylist())
        existing = set(self.db.list_routing_domains(client_id))
        free = max(0, limit - len(existing))
        added: list[str] = []
        over = 0
        for dom in accepted:
            if dom in existing:
                rejected.append((dom, "уже в списке"))
                continue
            if len(added) >= free:
                over += 1
                continue
            if self.db.add_routing_domain(client_id, dom):
                added.append(dom)
        if added:
            self.reconcile_routing()
            self._routing_preseed(client_id, added)
        return RoutingAddResult(added=added, rejected=rejected,
                                over_limit=over, limit=limit)

    _RT_PRESEED_MAX = 20                  # доменов за раз; дальше ждём резолва клиента

    def _routing_preseed(self, client_id: int, domains: list[str]) -> None:
        """Досеять набор адресами только что добавленных доменов.

        Без этого набор для домена пуст до первого DNS-запроса клиента, а его
        не будет, пока не истечёт кэш браузера, — со стороны выглядит как
        «добавил, но не работает; потом само заработало».

        Не критично: резолв может не удаться, набор всё равно наполнится по
        запросам клиента. Поэтому все ошибки глотаем и работу не срываем.
        """
        if not routing.available():
            return
        addrs: list[str] = []
        for dom in domains[:self._RT_PRESEED_MAX]:
            addrs += routing.resolve_a(dom)
        if not addrs:
            return
        try:
            routing.add_networks(routing.user_set(client_id), addrs)
        except routing.RoutingError as e:
            log.warning("routing_preseed: %s", e)

    def routing_remove_domain(self, client_id: int, domain: str) -> bool:
        removed = self.db.remove_routing_domain(client_id, domain)
        if removed:
            self.reconcile_routing()
        return removed

    def routing_clear_domains(self, client_id: int) -> int:
        n = self.db.clear_routing_domains(client_id)
        if n:
            self.reconcile_routing()
        return n

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
        base_domains = list(self._routing_read_cache("home_domains"))

        # плечо контейнера: выпустить трафик включённых устройств
        # немаскараженным, иначе на хосте их не отличить от остальных
        routing.sync_nat_exempt([a for lst in addrs.values() for a in lst])

        # ДИФФ ПЕРЕД ЗАПИСЬЮ. Реконсиляция идёт каждый тик монитора, и раньше
        # она каждый раз пересобирала все наборы (6 exec на профиль) и цепочку
        # (флаш + правило на профиль) — ~24 000 exec в сутки ради состояния,
        # которое меняется нажатием тумблера. Один `ipset save` даёт состав
        # всех наборов; пишем только те, что разошлись. Не удалось прочитать —
        # пересобираем всё, как прежде.
        live = routing.snapshot_sets()
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
        return config.DATA_DIR / f"routing-{kind}.lst"

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
                                 "🟢 Условная маршрутизация снова применяется.")]
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
            last = self.db.get_state(self._RT_LISTS_KEY)
            cached = len(self._routing_read_cache("home_domains"))
            # по расписанию ИЛИ немедленно, если кэш пуст: без списков режим не
            # действует вовсе, и ждать следующего окна незачем — в том числе на
            # старте бота, где эта же ветка и срабатывает
            if last and now - int(last) < every and cached > 0:
                return cached
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
                    self._routing_write_cache("home_domains", sorted(set(home)))
                size = len(self._routing_read_cache("home_domains"))
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
            return len(self._routing_read_cache("home_domains"))

    def routing_lists_info(self) -> dict:
        """Состояние списков для чата: сколько записей, когда обновлялись,
        период. Обновление — молчаливый процесс на тике монитора, и без этой
        сводки админ не отличит «списки свежие» от «источники умерли полгода
        назад, кэш застыл»."""
        raw = self.db.get_state(self._RT_LISTS_KEY)
        updated_at = int(raw) if raw and raw.isdigit() else None
        return {
            "count": len(self._routing_read_cache("home_domains")),
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
        """Замер прямо сейчас. Отдельно от routing_link_ok, чтобы было видно,
        где реальные пинги, а где чтение кэша."""
        # Две цели по умолчанию: один внешний хост — сам по себе точка отказа,
        # и его заминка выглядела бы как отвал шлюза.
        targets = settings.get("app.routing.probe_targets", None) or ["77.88.8.8", "8.8.8.8"]
        port = int(settings.get("app.routing.probe_port", 53))
        return routing.probe_gateway(list(targets), port)

    def routing_liveness_tick(self) -> list[Notification]:
        """Замер живости шлюза и деградация. Тикает часто (десятки секунд).

        Раньше это жило в трёхминутном мониторе и решало по возрасту хендшейка.
        Метрика была неверна и остаётся таковой: возраст хендшейка говорит,
        поднят ли туннель, а не ходит ли через него трафик.

        Частота досталась от обратной модели, где шлюз был ОСНОВНЫМ путём и его
        отказ означал «нет интернета». Сейчас он означает «российские сервисы
        ругаются на адрес», и такой срочности нет. Такт оставлен коротким
        сознательно: замер — это TCP-коннект, стоит копейки, а быстрый возврат
        из деградации полезен сам по себе.

        Пороги СИММЕТРИЧНЫ — три замера в обе стороны, см. _RT_DOWN_STREAK.
        Асимметрия имела смысл, пока включение было рискованнее выключения;
        теперь дороже всего дребезг, потому что каждое переключение
        перекладывает трафик всех включённых профилей.
        """
        if not routing.available():
            return []
        # Зондируем, только если маркировка вообще должна быть включена: при
        # выключенной фиче рубильник обязан стоять в «выкл» независимо от того,
        # что там со шлюзом.
        engaged = self.routing_engaged()
        # Зонд СНАРУЖИ замка: он длится секунды (сеть), и держать на это время
        # реконсиляцию значило бы менять один отказ на другой.
        verdict = self.routing_probe() if engaged else routing.PROBE_DOWN
        # Тик только МЕРИТ. Обвязку утверждаем по событию — смена вердикта,
        # первый тик после старта — и страховочно каждый 10-й тик (5 мин):
        # утверждать маршрут и правила каждые 30 с значило ~14 000 exec/сутки
        # ради состояния, которое меняется раз в неделю.
        self._rt_tick = getattr(self, "_rt_tick", -1) + 1
        last_verdict = getattr(self, "_rt_last_verdict", None)
        if engaged and (verdict != last_verdict or self._rt_tick % 10 == 0):
            routing.ensure_policy()
        self._rt_last_verdict = verdict

        # ГИСТЕРЕЗИС, а не пересчёт с нуля каждый тик. Пока порог гашения был
        # равен единице, пересчёт совпадал с гистерезисом и разницы не было. С
        # порогом больше единицы он ломается: плохой замер обнуляет счётчик
        # хороших, и следующий же хороший такт даёт good=1 < порога возврата —
        # то есть маркировка снимается ИМЕННО ТОГДА, когда шлюз ожил. Состояние
        # обязано меняться только на пересечении порогов, а не выводиться из
        # текущей серии.
        was_on = self.db.get_state(self._RT_LINK_KEY) == "1"
        # Стрики с потолком на пороге (сравнения только «>= порога» / «< порога»)
        # и одной транзакцией: в установившемся состоянии тик каждые 30 с не
        # пишет на диск вовсе — раньше это было 3 коммита × 2880 в сутки.
        down_cap = max(self._RT_DOWN_STREAK, self._RT_ANNOUNCE_AFTER)
        with self.db.transaction():
          if not engaged:
            # РЕШЕНИЕ, а не измерение. Гистерезис сглаживает дребезг сети, но
            # выключение фичи админом — не дребезг, и ждать три такта тут значит
            # не выполнить прямое указание. Раньше разницы не было: «выключено»
            # выражалось тем же плохим вердиктом, а порог гашения равнялся
            # единице, и оба пути совпадали.
            self.db.set_state(self._RT_STREAK_KEY, "0")
            down = 0
            ok = False
          elif verdict == routing.PROBE_OK:
            good = min(int(self.db.get_state(self._RT_STREAK_KEY) or 0) + 1, self._RT_UP_STREAK)
            self.db.set_state(self._RT_STREAK_KEY, str(good))
            down = 0
            ok = True if was_on else good >= self._RT_UP_STREAK
          else:
            self.db.set_state(self._RT_STREAK_KEY, "0")
            down = min(int(self.db.get_state(self._RT_DOWN_KEY) or 0) + 1, down_cap)
            # держим маркировку, пока порог гашения не набран
            ok = was_on and down < self._RT_DOWN_STREAK
          self.db.set_state(self._RT_DOWN_KEY, str(down))

        try:
            # ПОД ЗАМКОМ: реконсиляция под ним же пересобирает ту цепочку, чей
            # рубильник мы дёргаем. _routing_apply перекладывает и наборы, и
            # состав правил; включить маркировку посреди этого значит открыть
            # хук в цепочку, собранную наполовину, — метить не тем набором и не
            # для тех профилей.
            with routing.mutation_lock:
                routing.set_marking_enabled(ok)
        except routing.RoutingError as e:
            log.warning("routing_liveness_tick: %s", e)
            return []

        self.db.set_state(self._RT_LINK_KEY, "1" if ok else "0")

        # ДЕЙСТВИЕ и ОБЪЯВЛЕНИЕ — разные пороги, и это не педантизм. Написать
        # админу дорого: короткий провал на домашнем аплинке — обычное дело, и
        # пара «отвалился/поднялся» в одну минуту не несёт ему информации, только
        # приучает не читать. Об отказе сообщаем, лишь когда он подтвердился
        # несколькими замерами. Сейчас пороги СОВПАДАЮТ (три и три), то есть
        # админ узнаёт ровно в момент смены состояния; разными они остаются по
        # смыслу — это разные решения, и разводить их можно, не трогая второе.
        announced = self.db.get_state(self._RT_ANNOUNCED_KEY) == "1"

        if ok:
            # «Снова в строю» — только если об отвале действительно сообщали.
            # Иначе админ получал бы одинокое «всё хорошо» на ровном месте.
            if not announced:
                return []
            self.db.set_state(self._RT_ANNOUNCED_KEY, "0")
            return [Notification(config.ADMIN_ID, self._TXT_RT_GW_UP)]

        if announced or down < self._RT_ANNOUNCE_AFTER:
            return []
        self.db.set_state(self._RT_ANNOUNCED_KEY, "1")
        # Разные причины — разный ремонт, поэтому и текст разный: «шлюз молчит»
        # чинят на линке, «за шлюзом нет интернета» — на самом шлюзе.
        text = (self._txt_rt_gw_no_path() if verdict == routing.PROBE_NO_PATH
                else self._txt_rt_gw_down())
        return [Notification(config.ADMIN_ID, text, critical=True)]
