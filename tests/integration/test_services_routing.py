"""Условная маршрутизация: трёхслойный флаг, личные списки, реконсиляция,
деградация при недоступном шлюзе.

Инфраструктура подменена фейком (fixture fake_routing) — проверяем, ЧТО именно
проецируется наружу, а не как выглядит вывод ipset.
"""
import pytest

from awgbot.core import config
from awgbot.core.blocks import DeviceBlock

pytestmark = pytest.mark.integration


def _srcset(client_id):
    """Имя src-набора клиента: общего набора больше нет — каждый включённый
    профиль получает своё правило и свой набор адресов."""
    from awgbot.infra import routing as _rt
    return _rt.src_set(client_id)


def _device(services, client, name="Телефон"):
    return services.add_device(client.id, name)


# ── Трёхслойный флаг ─────────────────────────────────────────────────────────

def test_both_layers_required(services, db, make_active_client, fake_routing):
    """Адрес попадает в набор только когда подняты ОБА слоя: разрешение админа и
    переключатель пользователя. Пер-девайсного слоя нет — режим на весь профиль."""
    c = make_active_client()
    dc = _device(services, c)

    services.set_routing_master(c.id, True)                  # только пользователь
    assert not fake_routing.sets.get(_srcset(c.id))

    services.set_routing_allowed(c.id, True)                 # + разрешение админа
    assert fake_routing.sets[_srcset(c.id)] == [dc.address]


def test_enabled_device_is_exempted_from_masquerade(
        services, make_active_client, fake_routing):
    """Плечо контейнера: включённое устройство должно выходить немаскараженным,
    иначе на хосте его не отличить от остальных и маркировать будет нечего."""
    c = make_active_client()
    dc = _device(services, c)
    services.set_routing_allowed(c.id, True)
    services.set_routing_master(c.id, True)
    assert fake_routing.nat_exempt == [dc.address]

    services.set_routing_master(c.id, False)
    assert fake_routing.nat_exempt == []


def test_revoking_permission_kills_effect_but_keeps_flags(
        services, db, make_active_client, fake_routing):
    """Отзыв разрешения гасит эффект, но НЕ разрушает настройку пользователя:
    вернули разрешение — всё как было, перенастраивать нечего."""
    c = make_active_client()
    dc = _device(services, c)
    services.set_routing_allowed(c.id, True)
    services.set_routing_master(c.id, True)
    assert fake_routing.sets[_srcset(c.id)] == [dc.address]

    services.set_routing_allowed(c.id, False)
    assert not fake_routing.sets.get(_srcset(c.id))
    assert db.get_client(c.id).routing_master == 1            # выбор клиента цел

    services.set_routing_allowed(c.id, True)
    assert fake_routing.sets[_srcset(c.id)] == [dc.address]


def test_master_toggle_switches_all_devices_at_once(
        services, make_active_client, fake_routing):
    """Режим на уровне профиля — под него попадают ВСЕ его устройства сразу."""
    c = make_active_client()
    d1, d2 = _device(services, c, "A"), _device(services, c, "B")
    services.set_routing_allowed(c.id, True)
    services.set_routing_master(c.id, True)
    assert sorted(fake_routing.sets[_srcset(c.id)]) == sorted(
        [d1.address, d2.address])

    services.set_routing_master(c.id, False)
    assert not fake_routing.sets.get(_srcset(c.id))


def test_blocked_device_stays_in_set(services, make_active_client, fake_routing):
    """Заблокированное устройство из набора НЕ убираем: DROP по его адресу стоит
    раньше стадии маркировки. Дублировать инвариант блокировок вторым механизмом
    значило бы завести источник расхождения."""
    c = make_active_client()
    dc = _device(services, c)
    services.set_routing_allowed(c.id, True)
    services.set_routing_master(c.id, True)

    services._device_set_block(dc.device_id, DeviceBlock.USER)
    services.reconcile_routing()
    assert fake_routing.sets[_srcset(c.id)] == [dc.address]


# ── Личные списки доменов ────────────────────────────────────────────────────

def test_add_domains_normalizes_and_reports(services, make_active_client):
    c = make_active_client()
    res = services.routing_add_domains(
        c.id, "https://www.bank.com/login\nNETFLIX.com\nмусор_тут")
    assert res.added == ["bank.com", "netflix.com"]
    assert "мусор_тут" in dict(res.rejected)


def test_add_domain_twice_is_reported_not_duplicated(services, make_active_client):
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    res = services.routing_add_domains(c.id, "bank.com")
    assert res.added == []
    assert dict(res.rejected)["bank.com"] == "уже в списке"
    assert services.routing_domains(c.id) == ["bank.com"]


def test_ru_domain_is_accepted_after_inversion(services, make_active_client):
    """После инверсии логики .ru-домен в личном списке ОСМЫСЛЕН: список описывает
    то, чему нужна заграница, и российский домен туда добавляют осознанно —
    например, если сервис требует зарубежный адрес."""
    c = make_active_client()
    res = services.routing_add_domains(c.id, "example.ru")
    assert res.added == ["example.ru"]


def test_denylist_blocks_own_infrastructure(services, make_active_client, monkeypatch):
    monkeypatch.setattr(config, "routing_denylist", lambda: ["vpn.example.org"])
    c = make_active_client()
    res = services.routing_add_domains(c.id, "mail.vpn.example.org good.com")
    assert res.added == ["good.com"]
    assert dict(res.rejected)["mail.vpn.example.org"] == "этот домен добавлять нельзя"


def test_limit_is_enforced(services, make_active_client, monkeypatch):
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get",
                        lambda key, default=None: 2 if "user_domains_max" in key else default)
    c = make_active_client()
    res = services.routing_add_domains(c.id, "a.com b.com c.com d.com")
    assert res.added == ["a.com", "b.com"]
    assert res.over_limit == 2
    assert res.limit == 2


def test_remove_and_clear(services, make_active_client):
    c = make_active_client()
    services.routing_add_domains(c.id, "a.com b.com")
    assert services.routing_remove_domain(c.id, "a.com") is True
    assert services.routing_remove_domain(c.id, "a.com") is False
    assert services.routing_domains(c.id) == ["b.com"]
    assert services.routing_clear_domains(c.id) == 1
    assert services.routing_domains(c.id) == []


# ── Проекция в dnsmasq ───────────────────────────────────────────────────────

def test_dnsmasq_conf_uses_per_client_set(
        services, make_active_client, fake_routing):
    """Личный домен идёт в набор своего профиля — общего набора нет вовсе."""
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    assert f"ipset=/bank.com/{config.ROUTING_SET_USER_PREFIX}{c.id}" in fake_routing.conf


def test_dnsmasq_not_rewritten_when_nothing_changed(
        services, make_active_client, fake_routing):
    """Дифф-скип: рестарт dnsmasq роняет кэш ВСЕМ клиентам, дёргать его на
    каждой плановой реконсиляции — портить резолвинг на ровном месте."""
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    writes = fake_routing.conf_writes
    services.reconcile_routing()
    services.reconcile_routing()
    assert fake_routing.conf_writes == writes


# ── Наборы и цепочка ─────────────────────────────────────────────────────────

def test_orphan_sets_of_deleted_client_are_destroyed(
        services, db, make_active_client, fake_routing):
    """Осиротевший набор однажды совпал бы по имени с новым client_id — и чужие
    домены достались бы другому человеку."""
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    user_set = f"{config.ROUTING_SET_USER_PREFIX}{c.id}"
    assert user_set in fake_routing.sets

    db.delete_client(c.id)
    services.reconcile_routing()
    assert user_set not in fake_routing.sets


def test_chain_lists_only_live_clients(services, make_active_client, fake_routing):
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    assert fake_routing.chain == [c.id]


# ── Деградация при непроходимом шлюзе ────────────────────────────────────────

def _settle(services, fake_routing):
    """Довести до включённого состояния: возврат требует _RT_UP_STREAK хороших
    замеров подряд. Тикаем по константе, а не по числу: порог уже меняли, и
    захардкоженное число тихо разъезжается с боевым поведением."""
    fake_routing.probe = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()


def _fail_until_announced(services, fake_routing, verdict="down"):
    """Провалить зонд столько раз, сколько нужно для письма админу. Возвращает
    уведомления последнего тика."""
    fake_routing.probe = verdict
    notes = []
    for _ in range(services._RT_ANNOUNCE_AFTER):
        notes = services.routing_liveness_tick()
    return notes


def test_gateway_unreachable_disables_marking_and_alerts_admin_once(
        services, fake_routing):
    """Шлюз не пропускает → маркировка снимается, трафик уходит обычным путём.
    Админа уведомляем один раз на смену состояния, а не каждый тик."""
    _settle(services, fake_routing)
    assert fake_routing.marking is True

    notes = _fail_until_announced(services, fake_routing)
    assert fake_routing.marking is False
    assert len(notes) == 1 and "недоступен" in notes[0].text
    assert services.routing_liveness_tick() == []       # повтор — молчим

    _settle(services, fake_routing)
    assert fake_routing.marking is True


def test_admin_switch_off_is_immediate_unlike_a_gateway_blip(services, fake_routing,
                                                             monkeypatch):
    """Гистерезис сглаживает ДРЕБЕЗГ СЕТИ, но не откладывает РЕШЕНИЕ.

    Выключение фичи админом и неготовность списков выражаются тем же плохим
    вердиктом, что и отказ шлюза. Пока порог гашения равнялся единице, разницы
    не было и оба пути совпадали. С порогом в три такта рубильник перестал бы
    срабатывать сразу — то есть прямое указание админа исполнялось бы через
    полторы минуты.
    """
    from awgbot.core import settings as st
    _settle(services, fake_routing)
    assert fake_routing.marking is True

    real = st.get_bool
    monkeypatch.setattr(st, "get_bool",
                        lambda k, d=False: False if k == "app.routing.enabled" else real(k, d))
    services.routing_liveness_tick()
    assert fake_routing.marking is False, "выключение ждало набора порога"


def test_recovery_takes_the_full_streak(services, fake_routing):
    """Возврат — только после серии хороших: включение рискованно, весь трафик
    уезжает в тоннель, и если тот не пропускает, интернета нет вовсе."""
    _settle(services, fake_routing)

    fake_routing.probe = "down"
    for _ in range(services._RT_DOWN_STREAK):
        services.routing_liveness_tick()
    assert fake_routing.marking is False

    fake_routing.probe = "ok"
    for i in range(services._RT_UP_STREAK - 1):
        services.routing_liveness_tick()
        assert fake_routing.marking is False, f"вернулись с {i + 1} замеров"
    services.routing_liveness_tick()
    assert fake_routing.marking is True


def test_tunnel_up_but_no_internet_behind_it_is_a_failure(services, fake_routing):
    """Ровно тот отказ, который старая проверка не видела в принципе: туннель
    поднят, хендшейки идут, а за шлюзом интернета нет."""
    _settle(services, fake_routing)
    notes = _fail_until_announced(services, fake_routing, "path")
    assert fake_routing.marking is False
    assert len(notes) == 1 and "интернета за ним нет" in notes[0].text


def test_alert_names_the_place_to_fix(services, fake_routing):
    """«Шлюз молчит» чинят на линке, «за шлюзом нет интернета» — на самом шлюзе.
    Разный ремонт — разный текст, иначе админ идёт не туда."""
    _settle(services, fake_routing)
    down = _fail_until_announced(services, fake_routing, "down")[0].text
    _settle(services, fake_routing)
    path = _fail_until_announced(services, fake_routing, "path")[0].text
    assert down != path


def test_feature_disabled_is_a_no_op(services, make_active_client, fake_routing):
    """Фича выключена в конфиге — реконсиляция ничего не трогает и не падает."""
    fake_routing.enabled = False
    c = make_active_client()
    services.reconcile_routing()
    assert fake_routing.chain is None
    assert services.routing_liveness_tick() == []


# ── обновление базовых списков ───────────────────────────────────────────────

def test_lists_update_fills_the_cache(services, fake_routing, monkeypatch, tmp_path):
    """Списки складываются в КЭШ, а не сразу в наборы: наборы пер-юзерные, их
    состав пересобирается при каждой реконсиляции — исходник нужен отдельно."""
    from awgbot.core import config
    from awgbot.infra import routing as infra_routing

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS",
                        ["http://x/outside", "http://x/ru-blocklist"])
    monkeypatch.setattr(infra_routing, "fetch", lambda url, timeout=15: (
        "ipset=/gosuslugi.ru/other_set\n#комментарий\n" if "outside" in url
        else "ozon.ru\nsberbank.ru\n"))

    services.routing_update_lists(force=True)
    # оба формата разобраны, чужое имя набора отброшено, дубликатов нет
    assert services._routing_read_cache("home_domains") == [
        "gosuslugi.ru", "ozon.ru", "sberbank.ru"]


def test_lists_update_survives_dead_source(services, fake_routing, monkeypatch):
    """Апстрим недоступен — прежний кэш остаётся в силе.

    Устаревшие списки лучше пустых: пустой означал бы, что домой не уходит
    ничего, то есть российские сервисы разом увидят зарубежный адрес.
    """
    from awgbot.core import config
    from awgbot.infra import routing as infra_routing
    monkeypatch.setattr(config, "ROUTING_LISTS_HOME_URLS", ["http://dead/list"])
    monkeypatch.setattr(infra_routing, "fetch", lambda url, timeout=60: None)
    services._routing_write_cache("home_domains", ["ozon.ru"])
    services.routing_update_lists(force=True)
    assert services._routing_read_cache("home_domains") == ["ozon.ru"]


def test_monitor_enables_marking_when_ready(services, fake_routing):
    """Списки на месте и шлюз пропускает — политика включается."""
    fake_routing.probe = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    assert fake_routing.marking is True


# ── Досев набора при добавлении домена ───────────────────────────────────────

def test_added_domain_is_preseeded_into_the_set(
        services, make_active_client, fake_routing):
    """Домен добавляют ровно тогда, когда сайт только что не открылся, — адрес
    уже в кэше браузера, и клиент переспросит DNS не скоро. Если ждать его
    запроса, набор стоит пустым и сайт продолжает не открываться: «добавил, но
    не работает; потом само заработало». Поэтому резолвим сами.
    """
    from awgbot.infra import routing
    c = make_active_client()
    fake_routing.dns["bank.com"] = ["203.0.113.7", "203.0.113.8"]
    services.routing_add_domains(c.id, "bank.com")

    members = fake_routing.sets[routing.user_set(c.id)]
    assert "203.0.113.7" in members and "203.0.113.8" in members


def test_preseed_keeps_what_dnsmasq_collected(
        services, make_active_client, fake_routing):
    """Досев ДОПИСЫВАЕТ: в наборе живут адреса, накопленные dnsmasq по запросам
    клиента, и перезапись оставила бы маркировать нечем."""
    from awgbot.infra import routing
    c = make_active_client()
    services.routing_add_domains(c.id, "first.com")
    fake_routing.sets[routing.user_set(c.id)].append("198.51.100.1")   # «от dnsmasq»

    fake_routing.dns["second.com"] = ["203.0.113.9"]
    services.routing_add_domains(c.id, "second.com")
    assert "198.51.100.1" in fake_routing.sets[routing.user_set(c.id)]


def test_failed_resolve_does_not_break_the_add(
        services, make_active_client, fake_routing):
    """Резолв — не критичный путь: набор всё равно наполнится по запросам
    клиента, поэтому неудача не должна ронять добавление."""
    def boom(dom, timeout=3.0):
        raise OSError("нет сети")
    from awgbot.infra import routing
    import pytest
    monkey = pytest.MonkeyPatch()
    monkey.setattr(routing, "resolve_a", lambda dom, timeout=3.0: [])
    try:
        c = make_active_client()
        res = services.routing_add_domains(c.id, "unresolvable.invalid")
        assert res.added == ["unresolvable.invalid"]
    finally:
        monkey.undo()


def test_hot_switch_off_wins_over_a_healthy_gateway(services, fake_routing, monkeypatch):
    """Реконсиляция и тик живости уже однажды разошлись во мнениях и спорили за
    рубильник: один снимал политику, другой немедленно возвращал. Условие «фича
    выключена» обязано перевешивать любой вердикт зонда."""
    from awgbot.core import settings
    fake_routing.probe = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    assert fake_routing.marking is True

    real = settings.get_bool
    monkeypatch.setattr(settings, "get_bool",
                        lambda k, d=None: False if k == "app.routing.enabled"
                        else real(k, d))
    services.routing_liveness_tick()
    services.routing_liveness_tick()
    assert fake_routing.marking is False, "зонд пересилил выключенную фичу"


# ── зеркальный случай: сервисы, отказывающие российским адресам ──────────────

def _allowed_client(services, make_active_client, tg_id):
    """Клиент с устройством и выданным разрешением админа. Устройство важно:
    без него профиль не попадает в набор адресов, и конфиг выходит пустым."""
    c = make_active_client(tg_id=tg_id)
    _device(services, c)
    services.set_routing_allowed(c.id, True)
    return services.db.get_client(c.id)





def test_short_blip_degrades_silently(services, fake_routing):
    """Ровно тот спам, что пришёл админу: «отвалился» и «снова в строю» в одну
    минуту. Короткий провал на домашнем аплинке — обычное дело, и такая пара не
    несёт никакой информации, только приучает не читать уведомления.

    Теперь такой провал не доходит и до действия: порог гашения поднят до трёх
    тактов ровно за этим — чтобы мелкие флуктуации не дёргали режим."""
    _settle(services, fake_routing)

    fake_routing.probe = "down"
    assert services.routing_liveness_tick() == [], "написал админу с первого замера"
    assert fake_routing.marking is True, "погасил от одного плохого замера"

    fake_routing.probe = "ok"
    notes = []
    for _ in range(3):
        notes += services.routing_liveness_tick()
    assert notes == [], "прислал «снова в строю» без предшествующего отвала"
    assert fake_routing.marking is True


def test_recovery_is_announced_only_after_a_real_alert(services, fake_routing):
    """Зеркальное: если об отвале сообщили, о возврате обязаны сообщить тоже —
    иначе админ останется думать, что всё ещё сломано."""
    _settle(services, fake_routing)
    assert len(_fail_until_announced(services, fake_routing)) == 1

    fake_routing.probe = "ok"
    notes = []
    for _ in range(services._RT_UP_STREAK):
        notes += services.routing_liveness_tick()
    assert len(notes) == 1 and "в строю" in notes[0].text


# ── подсети сервисов со СВОИМ адресным блоком ────────────────────────────────



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

    services._routing_note_source(url, 0)          # замолчал
    notes = services.routing_source_alerts()
    assert len(notes) == 1
    assert url in notes[0].text and "500" in notes[0].text

    assert services.routing_source_alerts() == [], "доклад обязан быть однократным"

    services._routing_note_source(url, 500)        # ожил
    services._routing_note_source(url, 0)          # и снова замолчал
    assert len(services.routing_source_alerts()) == 1, "после оживления — снова докладываем"


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


# ── пороги зонда ─────────────────────────────────────────────────────────────

def test_marking_survives_short_blips(services, fake_routing):
    """Мелкие сетевые флуктуации не должны дёргать режим: каждое переключение
    перекладывает трафик всех включённых, и на коротком провале это дороже
    самого провала."""
    _settle(services, fake_routing)
    assert fake_routing.marking is True

    fake_routing.probe = "down"
    for i in range(services._RT_DOWN_STREAK - 1):
        services.routing_liveness_tick()
        assert fake_routing.marking is True, f"погасили на {i + 1}-м из трёх"

    fake_routing.probe = "ok"
    services.routing_liveness_tick()
    assert fake_routing.marking is True, "рубильник дёрнулся на ровном месте"


def test_confirmed_failure_disables_marking(services, fake_routing):
    """Порог подняли не ради того, чтобы перестать реагировать вовсе."""
    _settle(services, fake_routing)
    fake_routing.probe = "down"
    for _ in range(services._RT_DOWN_STREAK):
        services.routing_liveness_tick()
    assert fake_routing.marking is False, "порог набран, а маркировка стоит"


def test_recovery_needs_the_full_streak(services, fake_routing):
    """Гистерезис: состояние меняется на пересечении порогов, а не пересчётом
    серии. Пересчёт снимал маркировку ровно в тот такт, когда шлюз оживал —
    плохой замер обнулял счётчик хороших, и первый же хороший давал good=1
    меньше порога возврата."""
    _settle(services, fake_routing)
    fake_routing.probe = "down"
    for _ in range(services._RT_DOWN_STREAK):
        services.routing_liveness_tick()
    assert fake_routing.marking is False

    fake_routing.probe = "ok"
    for i in range(services._RT_UP_STREAK - 1):
        services.routing_liveness_tick()
        assert fake_routing.marking is False, f"вернулись с {i + 1} замеров"
    services.routing_liveness_tick()
    assert fake_routing.marking is True


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
    monkeypatch.setattr(infra_rt, "fetch", lambda url: fetched.append(url) or "")

    services._routing_write_cache("home_domains", [])
    services.db.set_state(services._RT_LISTS_KEY, str(int(_t.time())))
    services.routing_update_lists()
    assert fetched, "ранний выход по расписанию при пустом кэше"

    fetched.clear()
    services._routing_write_cache("home_domains", ["ozon.ru"])
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
