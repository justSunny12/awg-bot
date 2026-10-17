"""token.py — аварийная смена токена бота шлюза на ВПС: кому можно, проверка «бот не сервера и не другого шлюза», запись; на шлюз токен уезжает разделом token документа."""
from __future__ import annotations

from awgbot.domain.services.types import ServiceError


class TokenMixin:
    """Токен бота шлюза меняют только аварийно: утёк — отозвали в BotFather.
    Старый токен к этому моменту мёртв, бот шлюза из Telegram пропал и сам
    ничего принять не может, поэтому смена живёт на сервере («⚙️ Настройки →
    🔧 Сервис → 🤖 Токен бота»), а на шлюз токен везёт канал конфигурации
    (раздел token документа конфигурации). Токен может быть и другого бота (тот же
    утёк целиком, завели нового) — вся прежняя настройка шлюза остаётся; нельзя
    только бот сервера AWG и бот, уже занятый другим шлюзом."""

    _TOKEN_PUSH_KEY = "gw_token_push"     # отпечаток токена, введённого кнопкой: только такой едет шлюзу

    def gateway_token_replace(self, slot_id: int, token: str) -> None:
        """Новый токен слота — в env ВПС и метка «введён кнопкой»: раздел token
        документа едет только с ним. Токен, лежащий в env с настройки, мог
        устареть (меняли на шлюзе руками) — слать его шлюзу незачем."""
        from awgbot.domain import gwcfg
        self.set_gw_bot_token(token, slot_id)
        self.db.set_state(self._gw_slot_key(self._TOKEN_PUSH_KEY, slot_id), gwcfg.token_print(token.strip()))
        self._backup_event(slot_id, "token")

    def gateway_token_pushed(self, slot_id: int, token: str) -> bool:
        from awgbot.domain import gwcfg
        mark = self.db.get_state(self._gw_slot_key(self._TOKEN_PUSH_KEY, slot_id)) or ""
        return bool(token) and mark == gwcfg.token_print(token.strip())

    def gateway_token_slots(self) -> list[tuple[int, str]]:
        """Шлюзы, которым токен можно довезти каналом (агент не ниже 3.3.0):
        [(слот, «имя (подпись)»)]."""
        return [(gw.id, self._gw_display(gw)) for gw in self.db.gateways()
                if gw.device_id and not self.gwlink_agent_outdated(gw.id)]

    def gateway_token_check(self, slot_id: int, token: str, bot_id: int, username: str) -> dict:
        """Новый токен (getMe уже ответил — токен живой: bot_id, username)
        годится этому слоту: бот не сервера AWG и не занят другим шлюзом (два
        агента на одном боте перехватывали бы апдейты друг у друга). Бот может
        быть и новым — тогда шлюз продолжит со всей прежней настройкой под
        другим ботом. Нет — ServiceError с причиной словами. Возвращает
        {"new_bot": бот сменился, "username"}."""
        bot = str(bot_id)
        if str(token).split(":", 1)[0] != bot:
            raise ServiceError("Telegram назвал другого бота, чем в токене")
        from awgbot.core import config
        if config.BOT_TOKEN and config.BOT_TOKEN.split(":", 1)[0] == bot:
            raise ServiceError("это токен бота сервера AWG — шлюзу нужен свой бот")
        name = str(username or "").lower()
        for gw in self.db.gateways():
            if gw.id == int(slot_id):
                continue
            other_token = self.gw_bot_token(gw.id)
            other_name = str(self.gw_bot_identity(gw.id).get("username") or "").lower()
            if (other_token and other_token.split(":", 1)[0] == bot) or (name and other_name == name):
                raise ServiceError(f"этот бот уже у шлюза {self._gw_display(gw)} — нужен свой бот для каждого шлюза")
        old = self.gw_bot_token(slot_id)
        old_name = str(self.gw_bot_identity(slot_id).get("username") or "").lower()
        if old:
            new_bot = old.split(":", 1)[0] != bot
        else:
            new_bot = bool(old_name) and old_name != name
        return {"new_bot": new_bot, "username": str(username or "")}
