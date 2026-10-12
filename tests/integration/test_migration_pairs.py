"""Переезд профилей на второй интерфейс: пары в окне переезда — парные
мутации и выдача, видимость и уведомление, лимиты и тумблеры, хуки
маркировки, счётчик устройств.

Инфраструктура подменена — проверяем, ЧТО проецируется наружу и что
остаётся в базе, а не как выглядит вывод awg.
"""
import pytest

from awgbot.core import config
from awgbot.core.blocks import DeviceBlock
from awgbot.infra import awg as infra_awg

from tests.integration._migration_common import _seen, _twin

pytestmark = pytest.mark.integration


# ── парные мутации и выдача ──────────────────────────────────────────────────

def test_config_is_generated_from_its_own_interface(services, mig, make_active_client,
                                                    monkeypatch):
    """Конфиг собирается по параметрам ТОГО интерфейса, где живёт пир.

    Общие параметры отдали бы двойнику старый порт, старый серверный ключ и
    старую обфускацию. Отказ молчаливый: превью выглядит нормально, человек
    импортирует — и не подключается никто.
    """
    asked: list = []
    orig = infra_awg.read_server_params

    def spy(force=False, iface=None):
        asked.append(infra_awg.iface_of(iface))
        params = orig(force=force, iface=iface)
        params["listen_port"] = 443 if infra_awg.iface_of(iface) == "awg1" else 42755
        return params

    monkeypatch.setattr(infra_awg, "read_server_params", spy)

    c = make_active_client(name="c", tg_id=7020)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)

    asked.clear()
    cfg = services.generate_config(twin.id)
    assert asked == ["awg1"], "параметры взяты у чужого интерфейса"
    assert "ListenPort" not in cfg["conf"] or "443" in cfg["conf"]

    asked.clear()
    services.generate_config(dc.device_id)
    assert asked == ["awg0"], "старому устройству подсунули новые параметры"


def test_delete_removes_both_peers(services, mig, make_active_client):
    """Удаление ПАРНОЕ. Снять только видимый пир значит оставить второй
    работать — призрачный доступ у того, кого человек считает удалённым."""
    c = make_active_client(name="c", tg_id=7021)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)

    services.remove_device(twin.id)
    assert mig.peers_by_iface.get("awg1", {}) == {}
    assert mig.peers_by_iface.get("awg0", {}) == {}, "старый пир остался жив"
    assert services.db.get_device(dc.device_id) is None
    assert services.db.get_device(twin.id) is None


def test_block_covers_both_peers(services, mig, make_active_client):
    """Заблокировать один пир, оставив второй, значит оставить человеку доступ:
    блокировка перестала бы что-либо значить ровно в окне переезда."""
    c = make_active_client(name="c", tg_id=7022)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    old = services.db.get_device(dc.device_id)

    services._device_set_block(dc.device_id, DeviceBlock.USER)
    assert {old.address, twin.address} <= mig.blocked
    assert int(services.db.get_device(twin.id).block_reason) == int(DeviceBlock.USER)

    services._device_clear_block(dc.device_id, DeviceBlock.USER)
    assert not ({old.address, twin.address} & mig.blocked), "DROP снят не со всех"
    assert int(services.db.get_device(twin.id).block_reason) == 0


# ── видимость и уведомление ──────────────────────────────────────────────────

def test_user_sees_one_row_per_device(services, mig, make_active_client):
    """В окне у устройства две строки, а человек обязан видеть одну.

    Показать обе значит показать шесть устройств вместо трёх, а выдать конфиг
    не той — вручить пира уходящего интерфейса. Правило живёт в одном месте:
    пропусти вызывающего — и он покажет лишнее или выдаст не то.
    """
    c = make_active_client(name="c", tg_id=7030)
    services.add_device(c.id, "A")
    services.add_device(c.id, "B")
    services.migration_start()

    visible = services.db.list_devices(c.id)
    assert len(visible) == 2, "видно обе строки пары"
    assert all(d.iface == "awg1" for d in visible), "показан уходящий интерфейс"
    assert len(services.db.list_devices(c.id, all_rows=True)) == 4
    assert services.db.count_devices(c.id) == 2, "лимит упрётся вдвое раньше срока"


def test_after_cancel_the_old_rows_come_back(services, mig, make_active_client):
    """Отмена возвращает людей на старые пиры — значит и в списке снова старые.
    Двойники при этом живы, но человеку их видеть незачем."""
    c = make_active_client(name="c", tg_id=7031)
    services.add_device(c.id, "A")
    services.migration_start()
    assert services.db.list_devices(c.id)[0].iface == "awg1"

    services.migration_cancel()
    visible = services.db.list_devices(c.id)
    assert len(visible) == 1 and visible[0].iface == ""
    assert len(services.db.list_devices(c.id, all_rows=True)) == 2, "двойник снесён"


def test_after_finish_only_the_new_row_remains(services, mig, make_active_client):
    """После завершения пар нет вовсе, и правило видимости вырождается."""
    c = make_active_client(name="c", tg_id=7032)
    dc = services.add_device(c.id, "A")
    _seen(services, dc.device_id, ago_days=1)
    services.migration_start()
    _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    services.migration_finish()

    visible = services.db.list_devices(c.id)
    assert len(visible) == 1 and visible[0].iface == "awg1"
    assert visible[0].twin_of is None


def test_traffic_limit_sums_the_pair(services, mig, make_active_client):
    """Потребление в окне размазано по паре: лимит по одной строке дал бы
    человеку двойную квоту, а в интерфейсе показал бы обнулившийся расход."""
    c = make_active_client(name="c", tg_id=7033)
    dc = services.add_device(c.id, "A")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    services.db.add_traffic_bulk([(dc.device_id, 600, 0)])
    services.db.add_traffic_bulk([(twin.id, 400, 0)])

    rows = services.db.list_devices(c.id, all_rows=True)
    assert sum(d.traffic_rx_month for d in rows) == 1000


def test_ready_is_announced_once_per_migration(services, mig, make_active_client):
    """Доклад на СМЕНУ состояния, не на тик: иначе «готово» приходило бы каждые
    три минуты. А следующий переезд обязан доложить о своей готовности сам."""
    c = make_active_client(name="c", tg_id=7034)
    dc = services.add_device(c.id, "A")
    _seen(services, dc.device_id, ago_days=1)
    services.migration_start()
    assert services.migration_ready_alerts() == [], "доложили до переезда"

    _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    notes = services.migration_ready_alerts()
    assert len(notes) == 1 and "переехали" in notes[0].text
    assert services.migration_ready_alerts() == [], "доклад повторился"

    services.migration_cancel()
    services.migration_start()
    _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    assert len(services.migration_ready_alerts()) == 1, "следующий переезд промолчал"


def test_empty_cohort_does_not_announce(services, mig, make_active_client):
    """Живых пиров не было вовсе — переезжать некому, и «все переехали» было бы
    сообщением ни о чём."""
    c = make_active_client(name="c", tg_id=7035)
    services.add_device(c.id, "Мёртвое")
    services.migration_start()
    assert services.migration_ready_alerts() == []


# ── пары в окне: видимость, лимиты, тумблеры ─────────────────────────────────

def test_dangling_twin_survives_finish_visible(services, mig, make_active_client):
    """Старую строку пары удалила сверка ещё в окне — двойник с висячей ссылкой
    обязан пережить завершение видимым.

    Раньше _finish_plan такие пары пропускал, twin_of не обнулялся, а фильтр
    «twin_of IS NULL» после выключения рычага прятал устройство из ВСЕХ списков
    навсегда: пир работает, человек подключён, а устройства нет ни у кого.
    """
    c = make_active_client(name="c", tg_id=7040)
    dc = services.add_device(c.id, "Тел")
    _seen(services, dc.device_id, ago_days=1)
    services.migration_start()
    twin = _twin(services, dc.device_id)
    _seen(services, twin.id, ago_days=0)
    services.db.delete_device(dc.device_id, archive_reason=None)  # «сверка удалила»

    services.migration_finish()
    visible = services.db.list_devices(c.id)
    assert [d.id for d in visible] == [twin.id], "двойник пропал из списка"
    assert services.db.get_device(twin.id).twin_of is None, "висячая ссылка осталась"


def test_dangling_twin_visible_even_without_finish(services, mig, make_active_client):
    """Тот же случай, но рычаг выключен отменой: битая ссылка читается как
    «пары нет», а не как «прятать вечно»."""
    c = make_active_client(name="c", tg_id=7041)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    services.db.delete_device(dc.device_id, archive_reason=None)
    services.migration_cancel()

    assert [d.id for d in services.db.list_devices(c.id)] == [twin.id]


def test_device_limit_counts_the_pair_as_one(services, mig, make_active_client):
    """Лимит устройства в окне — СУММА по паре против лимита пары.

    Проверка каждой строки против её собственного лимита давала двойную квоту,
    у которой ни одна половина не дотягивает до порога: 30+30 при лимите 50
    не блокировали никого.
    """
    from awgbot.core.blocks import DeviceBlock as DB_
    GB = 1024 ** 3
    c = make_active_client(name="c", tg_id=7042)
    dc = services.add_device(c.id, "Тел", traffic_limit=50 * GB)
    services.migration_start()
    twin = _twin(services, dc.device_id)
    services.db.add_traffic_bulk([(dc.device_id, 30 * GB, 0)])
    services.db.add_traffic_bulk([(twin.id, 30 * GB, 0)])

    services.check_traffic_limits()
    assert int(services.db.get_device(twin.id).block_reason) & int(DB_.TRAFFIC_USER), \
        "пара выпила 60 из 50 и не заблокирована"
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DB_.TRAFFIC_USER), \
        "блокировка не парная"


def test_routing_toggle_is_pairwise(services, mig, make_active_client, fake_routing):
    """Человек щёлкает видимого двойника, а его реальный трафик до переимпорта
    идёт со СТАРОГО адреса. Непарный тумблер не делал ничего: выключение не
    выключало, включение не включало — молча."""
    c = make_active_client(name="c", tg_id=7043)
    dc = services.add_device(c.id, "Тел")
    services.set_routing_allowed(c.id, True)
    services.set_routing_device(dc.device_id, True)
    services.migration_start()
    twin = _twin(services, dc.device_id)

    services.set_routing_device(twin.id, False)          # щёлкнули видимого
    assert services.db.get_device(dc.device_id).routing_on == 0, \
        "старая строка осталась включённой — реальный трафик всё ещё метится"

    services.set_routing_device(dc.device_id, True)      # и в обратную сторону
    assert services.db.get_device(twin.id).routing_on == 1


def test_friend_sees_the_device_once(services, mig, make_active_client):
    """Активная связь копируется на обе строки пары, и без фильтра друг видел
    устройство дважды — и мог вытащить конфиг уходящего интерфейса."""
    c = make_active_client(name="c", tg_id=7044)
    dc = services.add_device(c.id, "Общее")
    code = services.make_device_friendly(dc.device_id)
    services.activate_friend(code, tg_id=7099)
    services.migration_start()

    holder = services.db.get_client_by_tg(7099)
    rows = services.db.list_held_devices(holder.id)
    assert len(rows) == 1, "друг видит устройство дважды"
    assert rows[0].iface == "awg1", "другу показан уходящий интерфейс"

    services.migration_cancel()
    rows = services.db.list_held_devices(holder.id)
    assert len(rows) == 1 and rows[0].iface == "", "после отмены друг не вернулся на старую"


def test_reassign_moves_the_pair(services, mig, make_active_client):
    """Перенос одной строки разрывал пару между профилями: старый пир оставался
    у донора, а завершение слило бы трафик и заархивировало устройство не тому."""
    c1 = make_active_client(name="Донор", tg_id=7045)
    c2 = make_active_client(name="Получатель", tg_id=7046, device_limit=5)
    dc = services.add_device(c1.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)

    services.reassign_device(twin.id, c2.id)
    assert services.db.get_device(dc.device_id).client_id == c2.id, \
        "старая строка пары осталась у донора"
    assert services.db.get_device(twin.id).client_id == c2.id


def test_finish_keeps_running_when_server_refuses(services, mig, make_active_client,
                                                  monkeypatch):
    """Снятие пира не удалось — пару не трогаем ВОВСЕ и рычаг не гасим.

    Прежний порядок сливал историю и удалял строку до снятия: пир оставался в
    конфиге без строки в БД (следующая сверка — карантин с тревогой), а
    повторное завершение слило бы трафик дважды.
    """
    from awgbot.infra import awg as infra_awg
    c = make_active_client(name="c", tg_id=7047)
    dc = services.add_device(c.id, "Тел")
    _seen(services, dc.device_id, ago_days=1)
    services.db.add_traffic_bulk([(dc.device_id, 700, 0)])
    services.migration_start()
    twin = _twin(services, dc.device_id)
    _seen(services, twin.id, ago_days=0)

    def refuse(pub, iface=None):
        raise infra_awg.AwgError("нет связи")
    monkeypatch.setattr(infra_awg, "remove_peer", refuse)

    removed, dropped, failed = services.migration_finish()
    assert removed == 0 and failed == ["Тел"]
    assert services.migration_running(), "рычаг погашен при живых старых пирах"
    assert services.db.get_device(dc.device_id) is not None, "строка удалена без снятия пира"
    assert services.db.get_device(twin.id).traffic_rx_month == 0, "история слита до снятия"

    monkeypatch.setattr(infra_awg, "remove_peer",
                        lambda pub, iface=None: mig.removed.append((pub, iface)))
    removed, _, failed = services.migration_finish()
    assert removed == 1 and failed == []
    assert services.db.get_device(twin.id).traffic_rx_month == 700, \
        "история слита не один раз либо потеряна"
    assert not services.migration_running()


def test_start_aborts_when_nothing_is_born(services, mig, make_active_client,
                                           monkeypatch):
    """awg1 ещё не поднят: ни один двойник не родился — рычаг НЕ включается.

    Включиться в этом состоянии значило бы молча фоллбэчить всю выдачу на
    старые конфиги при формально идущем переезде.
    """
    from awgbot.infra import awg as infra_awg
    c = make_active_client(name="c", tg_id=7048)
    dc = services.add_device(c.id, "Тел")
    _seen(services, dc.device_id, ago_days=1)

    def down(pub, psk, ip, iface=None):
        raise infra_awg.AwgError("интерфейс не поднят")
    monkeypatch.setattr(infra_awg, "add_peer", down)

    res = services.migration_start()
    assert res.started is False and res.failed == ["Тел"]
    assert not services.migration_running(), "рычаг включился без единого двойника"
    assert services.db.cohort_ids() == set(), "когорта осталась замороженной"


def test_cancel_returns_pending_invite_to_the_old_row(services, mig,
                                                      make_active_client):
    """Код при рождении переносили на двойника — отмена обязана перенести его
    обратно: двойник прячется, и активация присланной ссылки включила бы
    невидимую строку."""
    c = make_active_client(name="c", tg_id=7049)
    dc = services.add_device(c.id, "Тел")
    code = services.make_device_friendly(dc.device_id)
    services.migration_start()
    assert services.db.get_device(dc.device_id).friend_code is None  # переносился

    services.migration_cancel()
    assert services.db.get_device(dc.device_id).friend_code == code
    assert services.db.get_device_by_friend_code(code).id == dc.device_id


def test_rename_is_pairwise(services, mig, make_active_client):
    """Иначе отмена вернула бы старую строку со старым именем — переименование
    молча откатилось бы."""
    c = make_active_client(name="c", tg_id=7050)
    dc = services.add_device(c.id, "Старое имя")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    services.rename_device(twin.id, "Новое имя")

    services.migration_cancel()
    assert services.db.list_devices(c.id)[0].name == "Новое имя"


def test_visibility_honours_the_config_kill_switch(services, mig, make_active_client,
                                                   monkeypatch):
    """Очистка ключей в app.yaml — аварийный рубильник. Пока фильтр читал сырое
    состояние, механика выключалась, а видимость нет: людям показывались
    двойники, чьи конфиги больше не выдаются."""
    c = make_active_client(name="c", tg_id=7051)
    services.add_device(c.id, "Тел")
    services.migration_start()
    assert services.db.list_devices(c.id)[0].iface == "awg1"

    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "")
    assert services.db.list_devices(c.id)[0].iface == "", \
        "рубильник выключил механику, но не видимость"


def test_iface_normalizes_after_the_default_flip(services, mig, make_active_client,
                                                 monkeypatch):
    """Эндшпиль: админ переключил дефолт на новый интерфейс — явные метки
    схлопываются в пустую строку. Две записи одного смысла — класс расхождения,
    который уже стрелял."""
    c = make_active_client(name="c", tg_id=7052)
    dc = services.add_device(c.id, "Тел")
    _seen(services, dc.device_id, ago_days=1)
    services.migration_start()
    twin = _twin(services, dc.device_id)
    _seen(services, twin.id, ago_days=0)
    services.migration_finish()

    monkeypatch.setattr(config, "AWG_INTERFACE", "awg1")   # «переключил app.yaml»
    n = services.db.normalize_default_iface(config.AWG_INTERFACE)
    assert n == 1
    assert services.db.get_device(twin.id).iface == ""


def test_marking_hook_covers_every_client_subnet(monkeypatch):
    """Хук маркировки ставится на КАЖДУЮ клиентскую подсеть.

    Сужённый одной, он оставлял переехавших вообще без маркировки — и отказ был
    молчаливым и обманчивым: туннель работает, DNS отвечает, интернет есть, а
    российские сервисы видят зарубежный адрес. То есть ровно то, ради чего
    функция существует, не работает, и связать это с переездом неоткуда.
    """
    from awgbot.infra import routing as rt
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    monkeypatch.setattr(config, "ROUTING_CLIENT_SUBNET", "10.8.1.0/24")
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.1")

    present: set = set()
    added: list = []

    def host_ok(args):
        if args[:2] == ["iptables", "-t"] and "-C" in args:
            return " ".join(args) in present
        return True

    def mangle(args, check=True):
        line = " ".join(["iptables", "-t", "mangle", "-C"] + args[1:])
        if args[0] == "-I":
            present.add(line); added.append(args)
        elif args[0] == "-D":
            present.discard(line)

    monkeypatch.setattr(rt.base, "_host_ok", host_ok)
    monkeypatch.setattr(rt.marking, "_mangle", mangle)
    monkeypatch.setattr(rt.policy, "ensure_policy", lambda: None)

    rt.set_marking_enabled(True)
    subnets = {a[a.index("-s") + 1] for a in added}
    assert subnets == {"10.8.1.0/24", "10.9.1.0/24"}, \
        "переехавшие остались без маркировки"
    assert rt._hook_present() is True

    rt.set_marking_enabled(False)
    assert rt._hook_present() is False
    assert not present, "часть хуков осталась висеть после снятия рубильника"


def test_partial_hook_set_counts_as_absent(monkeypatch):
    """Один хук стоит, второй нет — это «не включено», а не «включено».

    Иначе рубильник показывал бы рабочее состояние, пока половина людей идёт
    мимо маркировки.
    """
    from awgbot.infra import routing as rt
    monkeypatch.setattr(config, "ROUTING_CLIENT_SUBNET", "10.8.1.0/24")
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.1")
    monkeypatch.setattr(rt.base, "_host_ok",
                        lambda args: "10.8.1.0/24" in " ".join(args))
    assert rt._hook_present() is False


def test_device_counter_shows_visible_rows(services, mig, make_active_client):
    """«Включено на N из M» считается по видимым строкам: в окне переезда у
    каждого устройства их две, и сырой счёт показывал человеку удвоенное."""
    c = make_active_client(name="c", tg_id=7060)
    services.add_device(c.id, "A")
    b = services.add_device(c.id, "B")
    services.set_routing_allowed(c.id, True)          # выдача включает все
    services.set_routing_device(b.device_id, False)   # одно выключили руками
    services.migration_start()

    assert services.routing_device_counts(c.id) == (1, 2)
    assert len(services.routing_devices(c.id)) == 2
