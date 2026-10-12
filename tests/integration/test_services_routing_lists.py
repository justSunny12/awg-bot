"""Условная маршрутизация: обновление базовых списков и источники — как и
когда о них докладывают; кэш списков.

Инфраструктура подменена фейком (fixture fake_routing) — проверяем, ЧТО именно
проецируется наружу, а не как выглядит вывод ipset.
"""
import pytest

pytestmark = pytest.mark.integration


# ── обновление базовых списков ───────────────────────────────────────────────

def test_lists_update_fills_the_cache(services, fake_routing, monkeypatch, tmp_path):
    """Списки складываются в КЭШ, а не сразу в наборы: наборы пер-юзерные, их
    состав пересобирается при каждой реконсиляции — исходник нужен отдельно."""
    from awgbot.core import config
    from awgbot.infra import routing as infra_routing

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS",
                        ["http://x/outside", "http://x/ru-blocklist"])
    monkeypatch.setattr(infra_routing.feeds, "fetch", lambda url, timeout=15: ((
        "ipset=/gosuslugi.ru/other_set\n#комментарий\n" if "outside" in url
        else "ozon.ru\nsberbank.ru\n"), "", 200))

    services.routing_update_lists(force=True)
    # оба формата разобраны, чужое имя набора отброшено, дубликатов нет
    assert services._routing_read_cache("tun_domains") == [
        "gosuslugi.ru", "ozon.ru", "sberbank.ru"]


def test_lists_update_survives_dead_source(services, fake_routing, monkeypatch):
    """Апстрим недоступен — прежний кэш остаётся в силе.

    Устаревшие списки лучше пустых: пустой означал бы, что домой не уходит
    ничего, то есть российские сервисы разом увидят зарубежный адрес.
    """
    from awgbot.core import config
    from awgbot.infra import routing as infra_routing
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", ["http://dead/list"])
    monkeypatch.setattr(infra_routing.feeds, "fetch",
                        lambda url, timeout=60: (None, "HTTP Error 429: Too Many Requests", 429))
    services._routing_write_cache("tun_domains", ["ozon.ru"])
    services.routing_update_lists(force=True)
    assert services._routing_read_cache("tun_domains") == ["ozon.ru"]


def test_monitor_enables_marking_when_ready(services, fake_routing):
    """Списки на месте и шлюз пропускает — политика включается."""
    fake_routing.probe = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    assert fake_routing.marking is True


# ── источники базовых списков: как и когда о них докладывают ─────────────────



def test_stale_source_is_reported_once(services, monkeypatch):
    """Источник, который раньше отдавал списки, а теперь молчит, — событие.

    Кэш переживает недоступность источника намеренно: устаревшие списки лучше
    пустых. Оборотная сторона — источник может умереть навсегда (переехал файл,
    заброшен репозиторий), а списки застынут. routing_lists_ready смотрит на
    непустоту, а не на свежесть, поэтому иначе об этом узнать неоткуда.
    """
    from awgbot.core import config
    url = "https://example.invalid/list.lst"
    # докладываем только про НАСТРОЕННЫЕ источники: убрали источник из конфига —
    # молчание про него перестаёт быть новостью
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [url])
    services._routing_note_source(url, 500)        # раньше отдавал
    assert services.routing_source_alerts() == []  # пока всё хорошо — молчим

    services._routing_note_source(url, 0)          # ответил пустым
    notes = services.routing_source_alerts()
    assert len(notes) == 1
    assert url in notes[0].text and "500" in notes[0].text

    assert services.routing_source_alerts() == [], "доклад обязан быть однократным"

    services._routing_note_source(url, 500)        # ожил
    ok = services.routing_source_alerts()
    assert len(ok) == 1 and ok[0].text.startswith("🟢"), "восстановление тоже событие"

    services._routing_note_source(url, 0)          # и снова замолчал
    assert len(services.routing_source_alerts()) == 1, "после оживления — снова докладываем"


def test_failure_does_not_burn_the_refresh_window(services, fake_routing, monkeypatch):
    """Отказ не съедает окно обновления целиком.

    Окно — часы, а 429 живёт минуты: пока метка времени ставилась при любом
    исходе, добор попыток был невозможен в принципе — следующее обращение
    пришлось бы на давно разошедшийся лимит. Кэш при этом непуст, и аварийный
    обход «пустой кэш качаем немедленно» тут не срабатывает.
    """
    import time as _t
    from awgbot.core import config, settings as st
    from awgbot.infra import routing as infra_rt
    url = "http://x/list"
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [url])
    services._routing_write_cache("tun_domains", ["ozon.ru"])
    services.db.set_state(services._RT_SRC_N + services._routing_src_key(url), "480")
    every = int(st.get("app.routing.lists_refresh_hours", 6)) * 3600

    monkeypatch.setattr(infra_rt.feeds, "fetch", lambda u, timeout=15: (
        None, "HTTP Error 429: Too Many Requests", 429))
    services.routing_update_lists()
    stamp = int(services.db.get_state(services._RT_LISTS_KEY))
    waited = every - (int(_t.time()) - stamp)
    assert 0 < waited <= services._RT_SRC_RETRY_SECS, "повтор отложен на целое окно"
    # гейт читает момент следующего похода — он тоже обязан открыться через паузу добора
    nxt = int(services.db.get_state(services._RT_LISTS_NEXT_KEY))
    assert 0 < nxt - int(_t.time()) <= services._RT_SRC_RETRY_SECS, "гейт открыт лишь через полное окно"

    # доборы исчерпаны — торопиться больше некуда, дальше обычным расписанием
    for _ in range(services._RT_SRC_TRIES):
        services.routing_update_lists(force=True)
    stamp = int(services.db.get_state(services._RT_LISTS_KEY))
    assert every - (int(_t.time()) - stamp) == every

    monkeypatch.setattr(infra_rt.feeds, "fetch",
                        lambda u, timeout=15: ("sberbank.ru\n", "", 200))
    services.routing_update_lists(force=True)
    stamp = int(services.db.get_state(services._RT_LISTS_KEY))
    assert every - (int(_t.time()) - stamp) == every, "успех — обычное окно"


def _fail_all_tries(services, url, err="timed out", code=0):
    """Довести источник до подтверждённой беды: тревога — только после доборов."""
    for _ in range(services._RT_SRC_TRIES):
        services._routing_note_source(url, 0, err, code)


def test_alarm_waits_for_the_retries(services, monkeypatch):
    """Одна неудача — не новость: сеть моргает, 429 живёт минуты. Тревога уходит
    после первой попытки и трёх доборов, и до тех пор молчим, а не «докладываем
    и тут же отзываем»."""
    from awgbot.core import config
    url = "https://example.invalid/list.lst"
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [url])
    services._routing_note_source(url, 480)
    services.routing_source_alerts()

    for attempt in range(1, services._RT_SRC_TRIES):
        assert services._routing_note_source(url, 0, "timed out", 0) is True, \
            "недобранная попытка обязана просить скорый повтор"
        assert services.routing_source_alerts() == [], f"тревога на попытке {attempt}"

    assert services._routing_note_source(url, 0, "timed out", 0) is False, \
        "доборы исчерпаны — торопиться больше некуда"
    assert len(services.routing_source_alerts()) == 1


def test_recovery_within_the_retries_stays_silent(services, monkeypatch):
    """Источник моргнул и вернулся до конца доборов — не было ни тревоги, ни
    выздоровления. Иначе о каждом моргании приходила бы пара сообщений."""
    from awgbot.core import config
    url = "https://example.invalid/list.lst"
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [url])
    services._routing_note_source(url, 480)
    services.routing_source_alerts()

    services._routing_note_source(url, 0, "timed out", 0)
    services._routing_note_source(url, 480)
    assert services.routing_source_alerts() == []

    # и счётчик обнулился: следующая беда получает свои полные доборы
    for _ in range(services._RT_SRC_TRIES - 1):
        services._routing_note_source(url, 0, "timed out", 0)
    assert services.routing_source_alerts() == []


def test_empty_answer_is_not_retried(services, monkeypatch):
    """Ответ 200 с пустым разбором добором не проверяем: он не про связь, и
    следующая попытка вернёт ровно то же самое."""
    from awgbot.core import config
    url = "https://example.invalid/list.lst"
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [url])
    services._routing_note_source(url, 480)
    services.routing_source_alerts()

    assert services._routing_note_source(url, 0) is False
    assert len(services.routing_source_alerts()) == 1


def test_404_says_the_file_moved_not_that_we_failed_to_reach_it(services, monkeypatch):
    """404 приходит тем же исключением, что 429 и таймаут, — а совет по ним
    противоположный: там ждать, здесь менять адрес в конфиге."""
    from awgbot.core import config
    url = "https://example.invalid/list.lst"
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [url])
    services._routing_note_source(url, 39)
    services.routing_source_alerts()

    _fail_all_tries(services, url, "HTTP Error 404: Not Found", 404)
    notes = services.routing_source_alerts()
    assert len(notes) == 1
    assert "404" in notes[0].text
    assert "lists_home_urls" in notes[0].text, "не сказано, где менять адрес"
    assert "обычно проходят сами" not in notes[0].text, "404 сам не пройдёт"


def test_unreachable_source_is_not_blamed_for_moving(services, monkeypatch):
    """«Не достучались» и «ответили пустым» — разные диагнозы и разные адреса.

    Обе беды приходят в учёт нулём записей, и раньше на обе шёл один доклад,
    советовавший проверить, не переехал ли файл. На 429 от GitHub это отправляло
    искать целый и никуда не девавшийся файл, пока чинить надо было связь.
    """
    from awgbot.core import config
    url = "https://example.invalid/list.lst"
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [url])
    services._routing_note_source(url, 480)
    services.routing_source_alerts()

    _fail_all_tries(services, url, "HTTP Error 429: Too Many Requests", 429)
    notes = services.routing_source_alerts()
    assert len(notes) == 1
    assert "429" in notes[0].text, "причина обязана быть в докладе, а не только в логе"
    assert "не переехал ли файл" not in notes[0].text

    # диагноз сменился — это новость, хотя источник плох и до, и после
    services._routing_note_source(url, 0)
    swap = services.routing_source_alerts()
    assert len(swap) == 1 and "не переехал ли файл" in swap[0].text


def test_recovery_is_reported_for_both_kinds(services, monkeypatch):
    """О починке говорим вслух — иначе тревога висит нерешённой.

    Молчащий источник не ломает маршрутизацию сегодня, поэтому увидеть своими
    глазами, что списки снова обновляются, неоткуда. Лимит запросов расходится
    сам собой, и без этого доклада админ ходил бы чинить уже починившееся.
    """
    from awgbot.core import config
    url = "https://example.invalid/list.lst"
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [url])
    for err in ("HTTP Error 429: Too Many Requests", ""):
        services._routing_note_source(url, 39)
        services.routing_source_alerts()            # исходное состояние — норма
        if err:
            _fail_all_tries(services, url, err, 429)
        else:
            services._routing_note_source(url, 0)
        assert len(services.routing_source_alerts()) == 1

        services._routing_note_source(url, 39)
        ok = services.routing_source_alerts()
        assert len(ok) == 1, f"после «{err or 'пусто'}» о восстановлении не сказали"
        assert "39" in ok[0].text
        assert services.routing_source_alerts() == [], "и это тоже однократно"


def test_flap_between_ticks_is_not_a_second_alarm(services, monkeypatch):
    """Источник мигнул и снова упал в промежутке между тиками — админу об этом
    сказать нечего: он знает «плохо», и оно по-прежнему плохо. Прежняя схема
    сняла бы флаг и прислала второй такой же доклад."""
    from awgbot.core import config
    url = "https://example.invalid/list.lst"
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [url])
    services._routing_note_source(url, 480)
    services.routing_source_alerts()
    _fail_all_tries(services, url, "timed out")
    assert len(services.routing_source_alerts()) == 1

    services._routing_note_source(url, 480)        # ожил и умер, тика между ними нет
    _fail_all_tries(services, url, "timed out")
    assert services.routing_source_alerts() == []


def test_source_that_never_worked_is_not_reported(services, monkeypatch):
    """Пустой ответ от источника, который и раньше ничего не давал, — не новость.

    Иначе первый же старт с недоступной сетью завалил бы админа докладами про
    все восемь источников разом.
    """
    from awgbot.core import config
    url = "https://example.invalid/never.lst"
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [url])
    services._routing_note_source(url, 0)
    assert services.routing_source_alerts() == []


def test_dropped_source_stops_being_reported(services, monkeypatch):
    """Источник убрали из конфига — доклады про него прекращаются.

    Иначе метка в state пережила бы саму настройку и админ получал бы жалобы на
    список, который сам же и отключил.
    """
    from awgbot.core import config
    url = "https://example.invalid/dropped.lst"
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [url])
    services._routing_note_source(url, 10)
    services._routing_note_source(url, 0)
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", [])
    assert services.routing_source_alerts() == []


def test_empty_home_cache_forces_a_refresh(services, monkeypatch):
    """Пустой кэш списков — повод качать немедленно, не дожидаясь окна.

    Раньше наполненность считалась суммой кэшей, и непустота одного покрывала
    пустоту другого: после обновления новый кэш оставался пуст до истечения
    шести часов, и режим приезжал мёртвым.
    """
    import time as _t
    from awgbot.core import config, settings as st
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(st, "get_bool", lambda k, d=False: True)

    fetched: list = []
    from awgbot.infra import routing as infra_rt
    monkeypatch.setattr(infra_rt.feeds, "fetch", lambda url: fetched.append(url) or ("", "", 200))

    services._routing_write_cache("tun_domains", [])
    services.db.set_state(services._RT_LISTS_KEY, str(int(_t.time())))
    services.routing_update_lists()
    assert fetched, "ранний выход по расписанию при пустом кэше"

    fetched.clear()
    services._routing_write_cache("tun_domains", ["ozon.ru"])
    services.db.set_state(services._RT_LISTS_KEY, str(int(_t.time())))
    services.routing_update_lists()
    assert fetched == [], "качаем вне расписания при полном кэше"


def test_empty_lists_are_safe_not_fatal(services, fake_routing, monkeypatch, tmp_path):
    """Пустой набор больше не авария и не повод не подниматься.

    В упразднённой обратной модели он означал «на шлюз уходит ВСЁ» — включая
    заблокированное, которое с российского адреса не откроется. Поэтому
    существовал сторож, не дававший функции подняться без списков. Теперь набор
    перечисляет то, что идёт на шлюз, и пустой означает «не идёт ничего», то
    есть равносилен выключенной функции.

    Проверяем ПОВЕДЕНИЕ, а не предикат: сторож был снят вырождением
    routing_lists_ready в константу, и тест на константу переживал бы любое
    возвращение сторожа другим путём.
    """
    from awgbot.core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)      # кэши пусты
    assert services.routing_engaged() is True
    services.reconcile_routing()
    # Различаем по конфигу dnsmasq: его пишет только _routing_apply, а
    # _routing_stand_down не трогает вовсе. Цепочка для этого не годится —
    # stand_down тоже её пересобирает, только пустой.
    assert fake_routing.conf is not None, "реконсиляция ушла в stand_down"


def test_the_old_cache_name_is_carried_over_once(services):
    """Кэш РФ-списков переименован (home_domains → tun_domains): файл прежнего
    имени переносится при первом обращении, иначе до обновления списков
    туннель остался бы без доменов."""
    from awgbot.core import config
    new = config.DATA_DIR / "routing-tun_domains.lst"
    new.unlink(missing_ok=True)
    old = config.DATA_DIR / "routing-home_domains.lst"
    old.write_text("ozon.ru\n", encoding="utf-8")
    assert services._routing_read_cache("tun_domains") == ["ozon.ru"]
    assert new.exists() and not old.exists()
