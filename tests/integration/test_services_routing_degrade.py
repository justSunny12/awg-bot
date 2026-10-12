"""Условная маршрутизация: деградация при непроходимом шлюзе, пороги зонда,
отказ применения, о котором узнаёт админ.

Инфраструктура подменена фейком (fixture fake_routing) — проверяем, ЧТО именно
проецируется наружу, а не как выглядит вывод ipset.
"""
import pytest

from tests.integration._services_routing_common import _device

pytestmark = pytest.mark.integration


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
    уведомления за все такты: письмо может прийти и раньше последнего — в
    скользящем окне ещё могут лежать неудачи прошлого провала."""
    fake_routing.probe = verdict
    notes = []
    for _ in range(services._rt_fail_need()):
        notes += services.routing_liveness_tick()
    return notes


def test_gateway_unreachable_disables_marking_and_alerts_admin_once(
        services, fake_routing):
    """Шлюз не пропускает → маркировка снимается, трафик уходит обычным путём.
    Админа уведомляем один раз на смену состояния, а не каждый тик."""
    _settle(services, fake_routing)
    assert fake_routing.marking is True

    notes = _fail_until_announced(services, fake_routing)
    assert fake_routing.marking is False
    assert len(notes) == 1 and notes[0].text.startswith("🔴 Шлюз"), notes[0].text
    assert "не отвечает" in notes[0].text and not notes[0].action, "у «шлюз лёг» кнопки нет"
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
    """Подтверждённый отказ гасит маркировку; возврат — только после серии
    хороших: включение рискованно, весь трафик уезжает в тоннель, и если тот
    не пропускает, интернета нет вовсе. Гистерезис: состояние меняется на
    пересечении порогов, а не пересчётом серии — пересчёт снимал маркировку
    ровно в тот такт, когда шлюз оживал (плохой замер обнулял счётчик хороших)."""
    _settle(services, fake_routing)

    fake_routing.probe = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    assert fake_routing.marking is False, "порог набран, а маркировка стоит"

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
    _device(services, c)
    services.set_routing_allowed(c.id, True)             # есть кому маркировать — и всё равно ничего
    services.reconcile_routing()
    assert fake_routing.chain is None
    assert services.routing_liveness_tick() == []


# ── пороги зонда: короткий провал не дёргает ни режим, ни админа ─────────────

def test_short_blip_degrades_silently(services, fake_routing):
    """Ровно тот спам, что пришёл админу: «отвалился» и «снова в строю» в одну
    минуту. Короткий провал на локальном аплинке — обычное дело, и такая пара не
    несёт никакой информации, только приучает не читать уведомления.

    Такой провал не доходит и до действия: каждое переключение перекладывает
    трафик всех включённых, и на коротком провале это дороже самого провала —
    порог гашения в несколько тактов ровно за этим."""
    _settle(services, fake_routing)

    fake_routing.probe = "down"
    for i in range(services._rt_fail_need() - 1):
        assert services.routing_liveness_tick() == [], "написал админу до порога"
        assert fake_routing.marking is True, f"погасили на {i + 1}-м замере из порога"

    fake_routing.probe = "ok"
    notes = []
    for _ in range(3):
        notes += services.routing_liveness_tick()
    assert notes == [], "прислал «снова в строю» без предшествующего отвала"
    assert fake_routing.marking is True, "рубильник дёрнулся на ровном месте"


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


# ── отказ применения не должен быть молчаливым ───────────────────────────────

def test_failed_reconcile_reaches_the_admin_once(services, fake_routing, monkeypatch):
    """Падение реконсиляции — это в первую очередь не поднявшийся dnsmasq.

    Раньше оно уезжало в log.warning и больше никуда: бот продолжал работать,
    считая фичу живой, при мёртвом резолвере у всех клиентов. Внешне это
    «интернет через раз» — отрезолвленное ходит, новое нет, — и связать такое с
    маршрутизацией без подсказки почти невозможно.
    """
    from awgbot.infra import routing as _rt

    def boom(text, path=None):
        raise _rt.RoutingError("Не перезапустить dnsmasq: job failed")

    monkeypatch.setattr(_rt.feeds, "write_dnsmasq_conf", boom)
    services.reconcile_routing()

    notes = services.routing_infra_alerts()
    assert len(notes) == 1
    assert "dnsmasq" in notes[0].text
    assert services.routing_infra_alerts() == [], "доклад обязан быть однократным"


def test_recovered_reconcile_is_reported_too(services, fake_routing, monkeypatch):
    """Отбой нужен ровно потому, что тревога была: иначе админ не знает,
    чинилось ли оно само, и идёт проверять руками."""
    from awgbot.infra import routing as _rt

    # Отказ включается флагом, а не monkeypatch.undo(): undo снял бы и патчи
    # фикстуры fake_routing, после чего routing.available() стал бы ложью и
    # реконсиляция вышла бы сразу — тест «починился» бы, ничего не проверив.
    broken = {"yes": True}
    ok_write = _rt.write_dnsmasq_conf

    def maybe_boom(text, path=None):
        if broken["yes"]:
            raise _rt.RoutingError("Не перезапустить dnsmasq: job failed")
        return ok_write(text, path)

    monkeypatch.setattr(_rt.feeds, "write_dnsmasq_conf", maybe_boom)
    services.reconcile_routing()
    assert len(services.routing_infra_alerts()) == 1

    broken["yes"] = False
    services.reconcile_routing()
    notes = services.routing_infra_alerts()
    assert len(notes) == 1 and "🟢" in notes[0].text
    assert services.routing_infra_alerts() == []
