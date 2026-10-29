"""Основа в следовании, пока её пользователи и бот у резерва (журнал данных
резерва, этап «эпоха и следование»): решения пишет только бот основы на
резерве, основа по своей устаревшей базе ничего не перерешает; возврат —
прежним путём (реплика и слияние), эпоха к основе — по реплике возврата, до
слияния.

Пример, который до этапа выходил по коду: админ продлил «профиль Иван» в
боте на «Beta», а основа в отъезде по старой дате истекала подписку — Ивану
«подписка истекла», бит блокировки; при возврате слияние брало новую дату с
резерва, а статус `expired` и бит — с основы: профиль заблокирован при сроке
в будущем. То же с лимитом трафика, поднятым на резерве.

Мир — пара из tests/integration/_reserve_move.py (записи DNS и часы автоматов
подставные), реплика бота основы на резерве — копия базы основы с правками
«людей на резерве» (`snapshot_blob`). Ключи — заглушки.

Цена ошибки: основа в отъезде блокирует продлённых и шлёт им «подписка
истекла» и «лимит исчерпан», а слияние закрепляет блокировку; возврат без
реплики, оборванный на тексте итога, — админ не узнаёт о возврате, после
аварийного переезда не выпускается новое поколение ключа и не приходит
просьба сменить токен DNS; отложенный месячный сброс, забытый при слиянии, —
второй сброс месяца после возврата, и расход месяца у людей обнуляется;
`create_device` в следовании со словами «конфликт, попробуй ещё раз» — админ
повторяет без конца то, что заведомо не пройдёт.
"""
from __future__ import annotations

import datetime
import sqlite3
import types

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.types import ServiceError
from awgbot.infra import awg
from awgbot.runtime.scheduler import setup_scheduler
from awgbot.util import timeutil
from tests.conftest import restore_settings

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import notices

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

IVAN_TG, PETR_TG = 7100, 7200
LIMIT = 1000                                   # байт: лимит устройства Петра у основы
SPENT = 5000                                   # расход Петра за месяц — за лимитом


def _iso(days: float) -> str:
    return timeutil.to_iso(timeutil.now() + datetime.timedelta(days=days))


def _refused_or(fn):
    """Вызов решения у основы в отъезде: отказ базы (`jrn:`) — ничего не сделано
    и сказать нечего; иначе — что вызов вернул."""
    try:
        return fn()
    except sqlite3.IntegrityError as e:
        if not str(e).startswith("jrn:"):
            raise
        return []


@pytest.fixture()
def world(mv, fake_awg, make_active_client, monkeypatch):
    """Основа до передачи бота: «Иван» — срок по её базе вчера (продление было на
    резерве), «Пётр» — устройство с лимитом 1000 байт и расходом 5000 (лимит
    подняли на резерве). Снимок base-7 — финальная реплика передачи; пользователи
    и бот у резерва, резерв их несёт; возврат — «сразу», шлюзов нет."""
    svc = mv.svc
    mv.rsv.gw = False
    settings.set_value("app.reserve.return", "immediate")
    ivan = make_active_client(name="Иван", tg_id=IVAN_TG)
    idev = svc.add_device(ivan.id, "Телефон")
    svc.db.update_client_fields(ivan.id, period_end=_iso(-1))
    petr = make_active_client(name="Пётр", tg_id=PETR_TG)
    pdev = svc.add_device(petr.id, "Ноутбук")
    svc.db.update_device_fields(pdev.device_id, traffic_limit=LIMIT)
    svc.db.add_traffic_bulk([(pdev.device_id, SPENT // 2, SPENT // 2)])
    rm.put_base(svc, gen=7)
    rm.go_away(svc, gen=7)
    rm.st_in(svc, **{"in": "serving"})
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    mv.ivan, mv.idev, mv.petr, mv.pdev = ivan.id, idev.device_id, petr.id, pdev.device_id
    mv.fake = fake_awg
    return mv


async def _run_jobs(svc, bot, monkeypatch) -> None:
    """Такты планировщика основы в отъезде: опрос трафика (рукопожатия и рост
    счётчиков), сроки, месячный сброс и его догон, почта, имена аккаунтов."""
    devs = {d.id: d for d in svc.db.list_all_devices()}
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: [
        {"public_key": d.public_key, "rx": 10_000, "tx": 10_000, "last_handshake": 1_700_000_000}
        for d in devs.values()], raising=False)
    sched = setup_scheduler(svc, bot, svc.db)
    try:
        for jid in ("poll", "expiry", "monthly", "monthly_catchup", "email_resume", "tg_names", "monitor"):
            await sched.get_job(jid).func()
    finally:
        restore_settings()


def _state(svc, w) -> dict:
    c = svc.db.get_client(w.ivan)
    return {"ivan_status": str(c.status), "ivan_block": int(c.block_reason),
            "ivan_dev_block": int(svc.db.get_device(w.idev).block_reason),
            "petr_dev_block": int(svc.db.get_device(w.pdev).block_reason)}


# ── воспроизведение: продление на резерве и основа в отъезде ────────────────

async def test_an_extension_made_on_the_reserve_is_not_undone_by_the_primary_away(world, fake_bot, monkeypatch):
    """Основа в отъезде прогоняет сроки, лимиты и задания планировщика — ни
    `expired`, ни бита, ни уведомлений; после возврата со слиянием Иван активен
    с новым сроком, устройство Петра не заблокировано и с новым лимитом."""
    svc = world.svc
    calm = _state(svc, world)
    await _run_jobs(svc, fake_bot, monkeypatch)
    notes = _refused_or(svc.check_expiry) + _refused_or(svc.check_traffic_limits)
    assert _state(svc, world) == calm, f"основа в отъезде перерешила по устаревшей базе: {_state(svc, world)}"
    assert notes == [], f"основа в отъезде собрала уведомления людям: {[n.text for n in notes]}"
    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert sent == [], f"основа в отъезде написала людям: {sent}"
    addrs = {svc.db.get_device(world.idev).address, svc.db.get_device(world.pdev).address}
    assert not addrs & world.fake.blocked, f"основа в отъезде наложила DROP: {addrs & world.fake.blocked}"

    future = _iso(30)
    pdev, ivan = world.pdev, world.ivan

    def on_the_reserve(db):                                    # что админ сделал в боте на «Beta»
        db.update_client_fields(ivan, period_end=future)
        db.update_device_fields(pdev, traffic_limit=10 ** 12)
    svc.reserve_return_start("button")
    assert svc.reserve_released_in(rm.snapshot_blob(svc, on_the_reserve, gen=7)) == [("restart",)], \
        svc.rsv_get("rsv_out").get("merge_error")
    c = svc.db.get_client(ivan)
    assert (str(c.status), int(c.block_reason), c.period_end) == ("active", 0, future), \
        f"после слияния профиль Иван заблокирован при сроке в будущем: {c.status}, {c.block_reason}, {c.period_end}"
    d = svc.db.get_device(pdev)
    assert (int(d.block_reason), int(d.traffic_limit)) == (0, 10 ** 12), (d.block_reason, d.traffic_limit)


# ── прежний путь возврата: эпоха до слияния ─────────────────────────────────

def test_the_epoch_comes_back_with_the_return_replica_right_after_the_merge(world):
    """Реплика возврата пришла — бот на резерве остановлен. Пока слияние пишет живую
    базу сырым соединением (запрет его не касается), основа ещё следует: задания
    решений по устаревшей базе в это окно не идут; сразу после слияния право
    записи — у основы, правки резерва — в базе."""
    svc = world.svc
    assert svc.reserve_following() is True
    seen = []
    real = svc._return_merge

    def merge(blob, gen):
        seen.append(svc.db.jrn_following())
        return real(blob, gen)
    svc._return_merge = merge
    ivan = world.ivan

    def on_the_reserve(db):
        db.update_client_fields(ivan, period_end=_iso(60))
    svc.reserve_return_start("button")
    assert svc.reserve_released_in(rm.snapshot_blob(svc, on_the_reserve, gen=7)) == [("restart",)]
    assert seen == [True], f"на слиянии основа уже решала сама: {seen}"
    assert svc.reserve_following() is False
    assert svc.db.get_client(ivan).period_end > _iso(59), "слияние сырым соединением не записало правку резерва"


def test_a_failed_merge_does_not_give_the_epoch_back_to_the_reserve(world):
    """Отказ слияния (реплика не под тем K) оставляет пользователей у резерва
    по VPN, но гостя там уже нет — писать решения больше некому, кроме основы."""
    svc = world.svc
    svc.reserve_return_start("button")
    svc.reserve_released_in(rm.snapshot_blob(svc, k=b"\x02" * 32))
    assert svc.reserve_out() == "away" and svc.rsv_get("rsv_out").get("merge_error")
    assert svc.reserve_following() is False, "после отказа слияния решения не пишет никто"


# ── возврат без реплики — до конца ──────────────────────────────────────────

def _finish_return(svc) -> list:
    """Telegram снова здесь, запись вернулась, резерв снял хранилище (wiped)."""
    assert svc.reserve_tg_taken_mark()
    svc._out_step("relay", 300)
    return svc.reserve_wiped_in()


@pytest.mark.parametrize("kind, go_sent, rotate", [("dead", False, True), ("button", True, False)])
def test_a_return_without_a_replica_reaches_the_end_with_its_report(world, kind, go_sent, rotate):
    """Пустая реплика — возврат без слияния доходит до `_return_done`: итог
    возврата админу со строкой «Гостя на … уже не было»; после аварийного
    переезда (пакет открыт не моим K) — новое поколение ключа и просьба сменить
    токен DNS, после переезда кнопкой — ни того, ни другого."""
    svc = world.svc
    svc.rsv_update("rsv_dns", provider="cloudflare", token="DUMMY")   # токен был — конверт DNS в пакете
    svc.rsv_update("rsv_out", kind=kind, go_sent=go_sent)
    gen_before = int(svc.rsv_get("rsv_keys").get("m_gen") or 0)
    svc.reserve_return_start("button")
    assert svc.reserve_released_in(b"") == [("take_tg",), ("live",)]
    acts = _finish_return(svc)
    assert svc.reserve_out() == "home", svc.rsv_get("rsv_out")
    texts = [n.text for n in notices(acts)]
    report = [t for t in texts if t.startswith("↩️")]
    assert len(report) == 1, f"итог возврата не ушёл админу: {texts}"
    assert "Переехавшего инстанса на «Beta» уже не было — изменения, сделанные там, будут утеряны" in report[0], report[0]
    assert "&lt;" not in report[0] and "&amp;" not in report[0], f"строка отчёта экранирована дважды: {report[0]}"
    token = [t for t in texts if "Токен API DNS" in t]
    gen_after = int(svc.rsv_get("rsv_keys").get("m_gen") or 0)
    if rotate:
        assert len(token) == 1, f"после аварийного переезда нет просьбы сменить токен DNS: {texts}"
        assert gen_after == gen_before + 1, "после аварийного переезда не выпущено новое поколение"
    else:
        assert token == [] and gen_after == gen_before, "после переезда кнопкой — лишняя смена поколения или токена"
    assert svc.reserve_following() is False


def test_a_return_without_a_guest_on_the_reserve_gives_the_epoch_back_at_once(world):
    """Резерв на связи, но гостя у него уже нет (in = idle после начала возврата):
    реплики не будет и писать решения на резерве некому — право записи сразу у
    основы, как при пустой реплике; иначе до конца пересылки (минуты, а при
    ручной записи — до получаса) бот основы отвечает в Telegram, но каждое
    действие с профилями получает отказ."""
    svc = world.svc
    svc.reserve_return_start("button")
    world.clock.advance(1)
    rm.st_in(svc, **{"in": "idle"})
    acts = svc.reserve_out_tick()
    assert ("take_tg",) in acts, f"возврат без реплики не начат: {acts}"
    assert svc.reserve_following() is False, "возврат без гостя: основа всё ещё следует за тем, кого нет"


# ── месячный сброс в следовании ─────────────────────────────────────────────

def _month_now() -> str:
    return timeutil.now().strftime("%Y-%m")


async def _catchup(svc, bot) -> None:
    sched = setup_scheduler(svc, bot, svc.db)
    try:
        await sched.get_job("monthly_catchup").func()
    finally:
        restore_settings()


async def test_a_monthly_reset_put_off_by_following_is_settled_by_the_merge(world, fake_bot):
    """Сброс месяца пришёлся на следование — его сделал бот на резерве, и
    слияние принесло уже сброшенные счётчики: после слияния месяц отмечен, и
    догон не сбрасывает второй раз (расход после сброса на резерве остаётся)."""
    svc = world.svc
    svc.db.set_state("last_monthly_reset", "2020-01")
    svc.reserve_monthly_defer(_month_now())
    pdev = world.pdev

    def on_the_reserve(db):                                    # сброс месяца там и новый расход после него
        db.reset_month_traffic_all()
        db.add_traffic_bulk([(pdev, 300, 300)])
    svc.reserve_return_start("button")
    assert svc.reserve_released_in(rm.snapshot_blob(svc, on_the_reserve, gen=7)) == [("restart",)]
    assert svc.db.get_state("last_monthly_reset") == _month_now(), \
        "отложенный сброс не учтён слиянием — догон сбросит месяц второй раз"
    before = int(svc.db.get_device(pdev).traffic_rx_month)
    await _catchup(svc, fake_bot)
    assert int(svc.db.get_device(pdev).traffic_rx_month) == before, "догон сбросил месяц второй раз после слияния"


async def test_without_a_merge_the_catch_up_does_the_put_off_reset_here(world, fake_bot):
    """Возврат без реплики: сброса держателя в базе нет — его делает догон основы."""
    svc = world.svc
    svc.db.set_state("last_monthly_reset", "2020-01")
    svc.reserve_monthly_defer(_month_now())
    svc.reserve_return_start("button")
    svc.reserve_released_in(b"")
    _finish_return(svc)
    assert svc.db.get_state("last_monthly_reset") == "2020-01"
    await _catchup(svc, fake_bot)
    d = svc.db.get_device(world.pdev)
    assert (int(d.traffic_rx_month), svc.db.get_state("last_monthly_reset")) == (0, _month_now()), \
        "без слияния отложенный сброс месяца так и не сделан"


def test_the_put_off_month_only_moves_forward(world):
    svc = world.svc
    svc.reserve_monthly_defer("2026-11")
    svc.reserve_monthly_defer("2026-10")                       # поздний такт со старым месяцем не откатывает
    assert svc.rsv_get("rsv_follow")["monthly_deferred"] == "2026-11"


# ── отказ базы словами раздела ──────────────────────────────────────────────

def test_creating_a_device_while_following_says_away_not_conflict(world):
    svc = world.svc
    with pytest.raises(ServiceError) as ei:
        svc.add_device(world.ivan, "Планшет")
    text = str(ei.value)
    assert "на резерве «Beta»" in text and "Конфликт" not in text, f"отказ следования назван конфликтом: {text}"
    assert len(svc.db.list_devices(world.ivan)) == 1
    assert not [p for p, (_psk, ip) in world.fake.peers.items() if ip not in
                {d.address for d in svc.db.list_all_devices()}], "пир заведён в конфиг без устройства в базе"


def test_a_migration_twin_while_following_says_away_not_address_conflict(world, monkeypatch):
    svc = world.svc
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.1")
    with pytest.raises(ServiceError) as ei:
        svc._birth_twin(svc.db.get_device(world.idev))
    text = str(ei.value)
    assert "на резерве «Beta»" in text and "конфликт" not in text.lower(), text


def test_the_peer_reconcile_of_the_follower_neither_deletes_nor_adopts(world, monkeypatch):
    """Конфиг следует за базой: пропавший пир не удаляет устройство, чужой пир
    не заводится в служебный профиль — это решения бота на резерве. У держателя
    та же сверка чужой пир замечает (контроль подмены конфига)."""
    svc = world.svc
    conf = ("[Interface]\nPrivateKey = DUMMY\nListenPort = 43125\n\n"
            "[Peer]\nPublicKey = DUMMY-stranger\nPresharedKey = DUMMY-psk\nAllowedIPs = 10.8.1.200/32\n")
    monkeypatch.setattr(awg, "read_file", lambda path: conf, raising=False)
    for _ in range(config.MISSING_SWEEPS_THRESHOLD + 1):
        assert svc.reconcile_peers() == []
    assert svc.db.get_device(world.idev) is not None, "следующий удалил устройство по пропавшему пиру"
    assert "DUMMY-stranger" not in {d.public_key for d in svc.db.list_all_devices()}, "чужой пир заведён следующим"
    svc.db.jrn_pass(holder="self", role="holder", why="тест: контроль")
    assert svc.reconcile_peers(), "контроль: у держателя чужой пир не замечен — подмена конфига не работает"


def test_the_telegram_name_seen_by_the_follower_is_not_an_error(world):
    """Человек написал основе, пока решения у резерва: имя аккаунта не пишется,
    но и сообщение не падает."""
    from awgbot.bot.middleware import AccessMiddleware
    mw = AccessMiddleware(world.svc.db)
    client = world.svc.db.get_client(world.ivan)
    user = types.SimpleNamespace(full_name="Иван Новый", username="ivan_new")
    got = mw._touch_tg_name(client, user)
    assert got is not None and got.id == world.ivan and got.tg_name == client.tg_name
