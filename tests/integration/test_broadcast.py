"""Броадкаст: адресаты (дедуп клиент+друг, исключение админа, только активные
друзья), отчёт доставки, счётчик успех/провал."""
import pytest

from awgbot.bot import notifier


def test_recipients_dedup_and_exclude_admin(services, make_active_client):
    import awgbot.core.config as cfg
    # админ (tg == ADMIN_ID) + два клиента; служебный клиент tg=None не в счёт
    make_active_client(name="admin", tg_id=cfg.ADMIN_ID)
    make_active_client(name="c2", tg_id=2002)
    make_active_client(name="c3", tg_id=2003)
    ids = services.db.broadcast_recipients(exclude_tg_id=cfg.ADMIN_ID)
    assert cfg.ADMIN_ID not in ids            # админ исключён
    assert set(ids) == {2002, 2003}           # только реальные клиенты, без служебного


@pytest.mark.asyncio
async def test_broadcast_counts_ok_and_failed():
    sent = []

    class Bot:
        async def send_message(self, tg_id, text, **kw):
            if tg_id == 99:
                raise RuntimeError("blocked")
            sent.append(tg_id)

    ok, failed = await notifier.broadcast(Bot(), [1, 2, 99, 3], "привет")
    assert ok == 3 and failed == 1
    assert sent == [1, 2, 3]


@pytest.mark.asyncio
async def test_broadcast_empty_list():
    class Bot:
        async def send_message(self, *a, **k): raise AssertionError("не должно зваться")
    ok, failed = await notifier.broadcast(Bot(), [], "x")
    assert ok == 0 and failed == 0


@pytest.mark.asyncio
async def test_broadcast_pacing_and_order_preserved():
    """Порядок доставки сохранён, все не-нулевые адресаты обойдены."""
    seen = []

    class Bot:
        async def send_message(self, tg_id, text, **kw):
            seen.append(tg_id)

    ok, failed = await notifier.broadcast(Bot(), [5, 0, 7, None, 9], "x")
    assert seen == [5, 7, 9]          # 0/None пропущены
    assert ok == 3 and failed == 0
