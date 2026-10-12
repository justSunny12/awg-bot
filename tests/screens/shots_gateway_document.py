"""Снимки агента шлюза: документ с сервера AWG — вопросы о почте и фразе, ключи линка и конфиг аплинка."""
from __future__ import annotations

from awgbot.bot.callbacks import GwCB
from awgbot.core import config

from tests.screens.shots_gateway_common import _status
from tests.screens.shots_gateway_agent import _shot


_REKEY_ASKED = {"id": "0a1b2c3d4e5f", "state": "asked", "fp": "ab12 cd34"}


async def _invite(services, bot):
    """Крючок канала после link_next: первое в очереди — новым живым меню."""
    from awgbot.bot.handlers.gateway import ask
    await ask.show_cfg_question(bot, services, config.ADMIN_ID)


def _withdraw(kind: str):
    """Приглашение показано крючком канала, затем сервер отозвал запрос
    (cancel) или прислал новый (replaced): прежнее приглашение правится
    крючком правки так же, как его ставит main — текст без клавиатуры."""
    async def run(services, bot):
        from awgbot.bot.handlers.gateway import ask
        from awgbot.runtime import linkclient
        await ask.show_cfg_question(bot, services, config.ADMIN_ID)

        async def edit(chat_id, message_id, text):
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=None)
            return True
        prev = linkclient._edit, linkclient._ask
        linkclient._edit, linkclient._ask = edit, None
        try:
            client = linkclient.LinkClient(services)
            if kind == "cancel":
                await client._rekey_cancel({"id": _REKEY_ASKED["id"]})
            else:
                services.rekey_next_in = lambda msg: {"reply": None, "ask": False,
                                                      "replaced": services.rekey_state().get("msg")}
                await client._rekey_next({"id": "1b2c3d4e5f6a"})
        finally:
            linkclient._edit, linkclient._ask = prev
    return run


DOCUMENT = [
    # ── документ с сервера AWG: вопросы о почте и фразе ─────────────────────
    _shot("gw.cfg.pending", press=[GwCB(action="panel")], tick=_status(), mail=True, cfg_ask=("mail", "backup"),
          passphrase=True, title="панель: «⏳ Ждут решения: почта, ключевая фраза бэкапов» и кнопка «⏳ Решить»"),
    _shot("gw.cfg.ask.mail", press=[GwCB(action="panel"), GwCB(action="cfg_ask")], tick=_status(), mail=True,
          cfg_ask=("mail",), title="вопрос о почте: отличается ящик"),
    _shot("gw.cfg.ask.backup", press=[GwCB(action="panel"), GwCB(action="cfg_ask")], tick=_status(),
          passphrase=True, cfg_ask=("backup",), title="вопрос о фразе: своя фраза"),
    _shot("gw.cfg.ask.backup.key", press=[GwCB(action="panel"), GwCB(action="cfg_ask")], tick=_status(),
          backup_key=True, cfg_ask=("backup",), title="вопрос о фразе: шлюз на случайном ключе"),
    _shot("gw.cfg.ask.none", press=[GwCB(action="panel"), GwCB(action="cfg_ask")], tick=_status(),
          title="«⏳ Решить» без вопросов — всплывашка и панель"),
    _shot("gw.cfg.keep.mail", press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_no", val="mail")],
          tick=_status(), mail=True, cfg_ask=("mail",), title="«Оставить своё»: итог правкой вопроса, панель следом"),
    _shot("gw.cfg.accept.mail", press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_ok!", val="mail")],
          tick=_status(), mail=True, cfg_ask=("mail",), title="«Принять с сервера AWG»: почта проверена"),
    _shot("gw.cfg.accept.mail.fail", press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_ok!", val="mail")],
          tick=_status(), mail=True, mail_error="SMTP: авторизация отклонена", cfg_ask=("mail",),
          title="«Принять с сервера AWG»: почта сохранена, проверка не прошла"),
    _shot("gw.cfg.accept.backup", press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_ok!", val="backup")],
          tick=_status(), passphrase=True, cfg_ask=("backup",), title="«Принять с сервера AWG»: фраза принята"),
    _shot("gw.cfg.queue", press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_no", val="mail")],
          tick=_status(), mail=True, passphrase=True, cfg_ask=("mail", "backup"),
          title="очередь: после ответа о почте открывается вопрос о фразе"),
    # ── документ с сервера AWG: приглашения — ключи линка и конфиг аплинка ──
    _shot("gw.cfg.pending.all", press=[GwCB(action="panel")], tick=_status(), mail=True, rekey=_REKEY_ASKED,
          cfg_ask=("uplink", "mail"),
          title="панель: «⏳ Ждут решения: ключи линка, конфиг аплинка, почта» — в порядке очереди"),
    _shot("gw.cfg.invite.link", call=("приглашение с сервера", _invite), tick=_status(), rekey=_REKEY_ASKED,
          title="link_next принят — приглашение новым живым меню, шлюз трафик не несёт"),
    _shot("gw.cfg.invite.withdrawn", call=("приглашение показано, сервер отозвал запрос", _withdraw("cancel")),
          tick=_status(), rekey=_REKEY_ASKED,
          title="link_cancel — правится само приглашение, кнопки сняты"),
    _shot("gw.cfg.invite.replaced", call=("приглашение показано, сервер прислал новое", _withdraw("replaced")),
          tick=_status(), rekey=_REKEY_ASKED,
          title="новый link_next поверх висящего — прежнее приглашение «заменено более новым»"),
    _shot("gw.cfg.ask.link", press=[GwCB(action="panel"), GwCB(action="cfg_ask")], tick=_status(),
          rekey=_REKEY_ASKED, title="приглашение «🔑 Новые ключи линка»: шлюз трафик не несёт — без «РФ-доступ прервётся»"),
    _shot("gw.cfg.ask.link.carries", press=[GwCB(action="panel"), GwCB(action="cfg_ask")], tick=_status(),
          rekey=_REKEY_ASKED, carries=True, title="приглашение о ключах линка: шлюз несёт трафик — цена с обрывом"),
    _shot("gw.cfg.ask.uplink", press=[GwCB(action="panel"), GwCB(action="cfg_ask")], tick=_status(),
          cfg_ask=("uplink",), title="приглашение «🔐 Новый конфиг аплинка»: ключ устройства и отпечаток пары"),
    _shot("gw.cfg.accept.link", press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_ok!", val="link")],
          tick=_status(), rekey=_REKEY_ASKED, title="«🔑 Применить»: согласие ушло серверу, переключит он — панель следом"),
    _shot("gw.cfg.cancel.link", press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_no", val="link")],
          tick=_status(), rekey=_REKEY_ASKED, title="«⬅️ Отмена» ключей: итог правкой, панель следом"),
    _shot("gw.cfg.answered.link", press=[GwCB(action="cfg_ok!", val="link")], tick=_status(),
          title="«🔑 Применить» со старого приглашения, запрос уже решён или отозван — всплывашка и панель"),
    _shot("gw.cfg.accept.uplink", press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_ok!", val="uplink")],
          tick=_status(), cfg_ask=("uplink",),
          title="«🔐 Применить»: сначала «⏳ Применяю…», итог правкой, панель следом"),
    _shot("gw.cfg.accept.uplink.fail", press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_ok!", val="uplink")],
          tick=_status(), cfg_ask=("uplink",), uplink_ok=False, title="новый аплинк не поднялся — вернул прежний"),
    _shot("gw.cfg.accept.uplink.revert_failed",
          press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_ok!", val="uplink")],
          tick=_status(), cfg_ask=("uplink",), uplink_ok=False,
          uplink_fail=("revert_failed", "прежний конфиг аплинка не встал"),
          title="новый аплинк не поднялся, и прежний не вернулся — с причиной"),
    _shot("gw.cfg.accept.uplink.refused",
          press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_ok!", val="uplink")],
          tick=_status(), cfg_ask=("uplink",), uplink_ok=False,
          uplink_fail=("refused", "аплинк этого устройства не найден"),
          title="скрипт обвязки отказал — новый конфиг не применён, ничего не тронуто"),
    _shot("gw.cfg.accept.uplink.busy",
          press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_ok!", val="uplink")],
          tick=_status(), cfg_ask=("uplink",), uplink_ok=False, uplink_fail=("busy", "обвязка занята"),
          title="обвязка занята прогоном юнита — снова вопрос со строкой «нажми через минуту»"),
    _shot("gw.cfg.answered.uplink", press=[GwCB(action="cfg_ok!", val="uplink")], tick=_status(),
          title="«🔐 Применить» со старого приглашения, вопрос уже решён — всплывашка и панель"),
    _shot("gw.cfg.cancel.uplink", press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_no", val="uplink")],
          tick=_status(), cfg_ask=("uplink",), title="«⬅️ Отмена» аплинка: аплинк остался прежним"),
    _shot("gw.cfg.stale.uplink", press=[GwCB(action="cfg_ok!", val="uplink")], tick=_status(), cfg_ask=("uplink",),
          uplink_after="beef" * 4, title="аплинк поправили руками, пока висело приглашение — вопрос снят"),
    _shot("gw.cfg.queue.link", press=[GwCB(action="panel"), GwCB(action="cfg_ask"), GwCB(action="cfg_no", val="link")],
          tick=_status(), rekey=_REKEY_ASKED, cfg_ask=("uplink",),
          title="очередь: после ответа о ключах линка открывается приглашение об аплинке"),
]
