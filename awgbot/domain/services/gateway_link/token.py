"""token.py — аварийная смена токена бота шлюза на ВПС: кому можно, проверка «тот же бот», запись; на шлюз токен уезжает разделом token документа."""
from __future__ import annotations

from awgbot.domain.services.types import ServiceError


class TokenMixin:
    """Токен бота шлюза меняют только аварийно: утёк — отозвали в BotFather.
    Старый токен к этому моменту мёртв, бот шлюза из Telegram пропал и сам
    ничего принять не может, поэтому смена живёт на сервере («⚙️ Настройки →
    🔧 Сервис → 🤖 Токен бота»), а на шлюз токен везёт управляющий
    канал (возможность агента token1). Только токен ТОГО ЖЕ бота: другой бот
    увёл бы чат админа со шлюзом к чужому."""

    _TOKEN_PUSH_KEY = "gw_token_push"     # отпечаток токена, введённого кнопкой: только такой едет шлюзу

    def gateway_token_replace(self, slot_id: int, token: str) -> None:
        """Новый токен слота — в env ВПС и метка «введён кнопкой»: раздел token
        документа едет только с ним. Токен, лежащий в env с настройки, мог
        устареть (меняли на шлюзе руками) — слать его шлюзу незачем."""
        from awgbot.domain import gwcfg
        self.set_gw_bot_token(token, slot_id)
        self.db.set_state(self._gw_slot_key(self._TOKEN_PUSH_KEY, slot_id), gwcfg.token_print(token.strip()))

    def gateway_token_pushed(self, slot_id: int, token: str) -> bool:
        from awgbot.domain import gwcfg
        mark = self.db.get_state(self._gw_slot_key(self._TOKEN_PUSH_KEY, slot_id)) or ""
        return bool(token) and mark == gwcfg.token_print(token.strip())

    def gateway_token_slots(self) -> list[tuple[int, str]]:
        """Шлюзы, которым токен можно довезти каналом (агент с token1):
        [(слот, «имя (подпись)»)]."""
        return [(gw.id, self._gw_display(gw)) for gw in self.db.gateways()
                if gw.device_id and "token1" in self.gwlink_agent_caps(gw.id)]

    def gateway_token_bot_hint(self, slot_id: int) -> dict:
        """Что известно о прежнем боте слота: {"id": id из прежнего токена или
        "", "username": из getMe или снимка канала}. По ним проверяется, что
        новый токен — того же бота."""
        old = self.gw_bot_token(slot_id)
        me = self.gw_bot_identity(slot_id)
        return {"id": old.split(":", 1)[0] if old else "", "username": str(me.get("username") or "")}

    def gateway_token_check(self, slot_id: int, token: str, bot_id: int, username: str) -> None:
        """Новый токен (getMe уже ответил: bot_id, username) — того же бота,
        что был у слота. Нет — ServiceError с причиной словами."""
        hint = self.gateway_token_bot_hint(slot_id)
        if str(token).split(":", 1)[0] != str(bot_id):
            raise ServiceError("Telegram назвал другого бота, чем в токене")
        if hint["id"]:
            if hint["id"] != str(bot_id):
                raise ServiceError("это токен другого бота — нужен токен того же бота шлюза")
            return
        if not hint["username"]:
            raise ServiceError("не знаю, какой бот у этого шлюза: он ещё ни разу не был на связи")
        if hint["username"].lower() != str(username or "").lower():
            raise ServiceError(f"это токен другого бота — у шлюза @{hint['username']}")
