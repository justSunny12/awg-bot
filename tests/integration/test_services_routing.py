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
    """Довести до включённого состояния: возврат требует двух хороших замеров."""
    fake_routing.probe = "ok"
    services.routing_liveness_tick()
    services.routing_liveness_tick()


def test_gateway_unreachable_disables_marking_and_alerts_admin_once(
        services, fake_routing):
    """Шлюз не пропускает → маркировка снимается, трафик уходит обычным путём.
    Админа уведомляем один раз на смену состояния, а не каждый тик."""
    _settle(services, fake_routing)
    assert fake_routing.marking is True

    fake_routing.probe = "down"
    notes = services.routing_liveness_tick()
    assert fake_routing.marking is False
    assert len(notes) == 1 and "недоступен" in notes[0].text
    assert services.routing_liveness_tick() == []       # повтор — молчим

    _settle(services, fake_routing)
    assert fake_routing.marking is True


def test_degradation_is_immediate_but_recovery_is_not(services, fake_routing):
    """Несимметрично намеренно. Выключение безопасно — у пользователя лишь
    зарубежный адрес; включение рискованно — весь трафик уезжает в тоннель, и
    если тот не пропускает, интернета нет вовсе. Поэтому гасим по первому
    плохому замеру, а возвращаем после двух хороших подряд."""
    _settle(services, fake_routing)

    fake_routing.probe = "down"
    services.routing_liveness_tick()
    assert fake_routing.marking is False, "деградация должна быть немедленной"

    fake_routing.probe = "ok"
    services.routing_liveness_tick()
    assert fake_routing.marking is False, "вернулись с одного замера"
    services.routing_liveness_tick()
    assert fake_routing.marking is True


def test_tunnel_up_but_no_internet_behind_it_is_a_failure(services, fake_routing):
    """Ровно тот отказ, который старая проверка не видела в принципе: туннель
    поднят, хендшейки идут, а за шлюзом интернета нет."""
    _settle(services, fake_routing)
    fake_routing.probe = "path"
    notes = services.routing_liveness_tick()
    assert fake_routing.marking is False
    assert len(notes) == 1 and "интернета за ним нет" in notes[0].text


def test_alert_names_the_place_to_fix(services, fake_routing):
    """«Шлюз молчит» чинят на линке, «за шлюзом нет интернета» — на самом шлюзе.
    Разный ремонт — разный текст, иначе админ идёт не туда."""
    _settle(services, fake_routing)
    fake_routing.probe = "down"
    down = services.routing_liveness_tick()[0].text
    _settle(services, fake_routing)
    fake_routing.probe = "path"
    path = services.routing_liveness_tick()[0].text
    assert down != path


def test_feature_disabled_is_a_no_op(services, make_active_client, fake_routing):
    """Фича выключена в конфиге — реконсиляция ничего не трогает и не падает."""
    fake_routing.enabled = False
    c = make_active_client()
    services.reconcile_routing()
    assert fake_routing.chain is None
    assert services.routing_liveness_tick() == []


# ── обновление базовых списков ───────────────────────────────────────────────

def test_lists_update_fills_base_set(services, fake_routing, monkeypatch, tmp_path):
    """Списки складываются в КЭШ, а не сразу в наборы: наборы пер-юзерные, их
    состав пересобирается при каждой реконсиляции — исходник нужен отдельно."""
    from awgbot.core import config
    from awgbot.infra import routing as infra_routing

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ROUTING_LISTS_SUBNET_URLS", ["http://x/nets"])
    monkeypatch.setattr(config, "ROUTING_LISTS_DOMAINS_URL", "http://x/domains")
    monkeypatch.setattr(infra_routing, "fetch", lambda url, timeout=15: (
        "1.2.3.0/24\n5.6.7.0/28\nмусор\n" if "nets" in url
        else "ipset=/netflix.com/other_set\n#комментарий\n"))

    services.routing_update_lists(force=True)
    assert services._routing_read_cache("subnets") == ["1.2.3.0/24", "5.6.7.0/28"]
    # чужое имя набора из источника отброшено — оставлен только домен
    assert services._routing_read_cache("domains") == ["netflix.com"]


def test_lists_update_survives_dead_source(services, fake_routing, monkeypatch):
    """Апстрим недоступен — прежний набор остаётся в силе. Устаревшие списки
    лучше пустых: пустой отправил бы на шлюз всё."""
    from awgbot.core import config
    from awgbot.infra import routing as infra_routing
    monkeypatch.setattr(config, "ROUTING_LISTS_SUBNET_URLS", ["http://dead/nets"])
    monkeypatch.setattr(config, "ROUTING_LISTS_DOMAINS_URL", "")
    monkeypatch.setattr(infra_routing, "fetch", lambda url, timeout=60: None)
    # прежний кэш из фикстуры остался нетронутым — устаревшие списки лучше пустых
    assert services._routing_read_cache("subnets") == ["1.2.3.0/24"]


def test_lists_update_not_blocked_by_empty_base_set(services, fake_routing, monkeypatch, tmp_path):
    """Наполнение списков НЕ должно зависеть от полной самопроверки.

    Иначе выходит взаимоблокировка: набор пуст → самопроверка говорит
    «недоступна» → код наполнения не запускается → набор остаётся пуст. На чистой
    установке функция не поднялась бы никогда."""
    from awgbot.core import config
    from awgbot.infra import routing as infra_routing

    fake_routing.enabled = True
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(infra_routing, "available", lambda: False)   # списков нет
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(config, "ROUTING_LISTS_SUBNET_URLS", ["http://x/nets"])
    monkeypatch.setattr(config, "ROUTING_LISTS_DOMAINS_URL", "")
    monkeypatch.setattr(infra_routing, "fetch", lambda url, timeout=15: "1.2.3.0/24\n")

    got = {}
    monkeypatch.setattr(infra_routing, "invalidate_self_check",
                        lambda: got.setdefault("inv", True))

    services.routing_update_lists()
    assert services._routing_read_cache("subnets") == ["1.2.3.0/24"], "наполнение не запустилось"
    assert got.get("inv"), "кэш самопроверки не сброшен после наполнения"


def test_empty_base_set_disables_marking(services, make_active_client, fake_routing,
                                         monkeypatch, tmp_path):
    """Пустой базовый набор при инверсии = «на шлюз уходит ВСЁ». Маркировку в
    таком состоянии не включаем: отказ безопасный, трафик идёт обычным путём."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)    # кэши пусты
    c = make_active_client()
    _device(services, c)
    services.set_routing_allowed(c.id, True)
    services.set_routing_master(c.id, True)

    assert fake_routing.chain == []          # цепочка пуста
    assert fake_routing.marking is False     # политика снята


def test_monitor_does_not_enable_marking_without_lists(services, fake_routing,
                                                       monkeypatch, tmp_path):
    """Монитор не должен включать политику, пока списков нет.

    Раньше он смотрел только на живость шлюза и возвращал политику сразу после
    того, как реконсиляция её сняла: два места спорили за один рубильник, и
    состояние выходило противоречивым — цепочка пуста, а ip rule жив."""
    from awgbot.core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)      # кэши пусты
    fake_routing.probe = "ok"                              # шлюз пропускает
    services.routing_liveness_tick()
    services.routing_liveness_tick()
    assert fake_routing.marking is False


def test_monitor_enables_marking_when_ready(services, fake_routing):
    """Списки на месте и шлюз пропускает — политика включается."""
    fake_routing.probe = "ok"
    services.routing_liveness_tick()
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
    services.routing_liveness_tick()
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


def test_services_that_block_russia_go_abroad(services, make_active_client, fake_routing):
    """Внешние списки знают только про блокировки СО СТОРОНЫ России. Обратный
    случай — сервис сам отказывает российским адресам — в них не попадает, и при
    инверсии логики такой домен уходит на шлюз, то есть ровно через тот адрес,
    который ему закрыт. Встроенный список обязан идти наравне со скачанным."""
    from awgbot.core import config
    c = _allowed_client(services, make_active_client, 77)
    services.set_routing_master(c.id, True)
    conf = fake_routing.conf or ""
    for dom in ("example.com", "example.com", "openai.com"):
        assert f"/{dom}/" in conf, f"{dom} не попал в конфиг dnsmasq:\n{conf}"


def test_enabling_the_mode_preseeds_the_abroad_list(
        services, make_active_client, fake_routing):
    """Досев в момент включения. Иначе первый заход идёт по адресу из кэша
    браузера — мимо набора, на шлюз, и сервис отказывает. Пользователь видит
    «включил режим — отвалился этот сервис» и выключает обратно."""
    from awgbot.infra import routing
    c = _allowed_client(services, make_active_client, 78)
    fake_routing.dns["example.com"] = ["203.0.113.44"]
    services.set_routing_master(c.id, True)
    assert "203.0.113.44" in fake_routing.sets[routing.user_set(c.id)]


def test_disabling_does_not_resolve_anything(services, make_active_client, fake_routing):
    """Досев — это резолв; при выключении он бессмыслен."""
    from awgbot.infra import routing
    c = _allowed_client(services, make_active_client, 79)
    services.set_routing_master(c.id, True)
    fake_routing.dns["example.com"] = ["203.0.113.99"]     # появился ПОСЛЕ включения
    services.set_routing_master(c.id, False)
    assert "203.0.113.99" not in fake_routing.sets.get(routing.user_set(c.id), [])


def test_both_domain_sources_land_in_one_cache(services, fake_routing, monkeypatch):
    """Списки блокировок и геоблокировок описывают зеркальные случаи —
    «блокирует Россия» и «блокируют Россию», — но для нас оба означают одно:
    домену нужен зарубежный адрес. Значит и кэш один, и потерять второй источник
    нельзя."""
    from awgbot.core import config
    from awgbot.infra import routing as _rt
    bodies = {
        config.ROUTING_LISTS_DOMAINS_URL: "ipset=/rutracker.org/vpn_domains\n",
        config.ROUTING_LISTS_GEOBLOCK_URL: "example.com\n\nexample.com\n",
    }
    monkeypatch.setattr(_rt, "fetch", lambda url, timeout=15: bodies.get(url, ""))
    services.routing_update_lists(force=True)
    cached = services._routing_read_cache("domains")
    assert "rutracker.org" in cached, "потерян список блокировок"
    assert "example.com" in cached and "example.com" in cached, "потерян геоблок"


def test_geoblock_source_failure_keeps_the_other(services, fake_routing, monkeypatch):
    """Недоступность одного источника не должна ронять второй: устаревший или
    неполный набор лучше пустого — при инверсии логики пустой отправляет на шлюз
    вообще всё."""
    from awgbot.core import config
    from awgbot.infra import routing as _rt
    monkeypatch.setattr(_rt, "fetch", lambda url, timeout=15:
                        "ipset=/rutracker.org/vpn_domains\n"
                        if url == config.ROUTING_LISTS_DOMAINS_URL else "")
    services.routing_update_lists(force=True)
    assert "rutracker.org" in services._routing_read_cache("domains")
