"""
gateway/cfg/ — документ конфигурации с сервера AWG на агенте (части: prints —
отпечатки того, что стоит на шлюзе, для снимка; apply — порядок разделов и
ответ cfg_ack; txn — транзакция env+link с пробой и откатом; mailphrase —
правило «молча или вопрос» для почты и фразы, памятка «оставить своё»; ask —
очередь вопросов человеку; uplink — конфиг аплинка: решение, ключи
признания, наблюдение «аплинк мёртв»; uplinktxn — его транзакция под замком
обвязки, восстановление после перезапуска, аварийное правило на тике; rekey — смена ключей линка: приглашение, переключение,
отсчёт, поиск рабочего конфига; token — токен бота шлюза: тот же бот,
getMe, env, перезапуск после ответа серверу).

Сервер — источник желаемого, шлюз — источник фактов: агент сообщает в снимке
отпечатки стоящего (prints), получает документ целиком, проверяет его до
первого действия (domain/gwcfg.check) и применяет по разделам; что
применено, что ждёт человека, что отклонено и что не применилось — в ответе.
"""
from __future__ import annotations

from .prints import CfgPrintsMixin
from .mailphrase import CfgMailPhraseMixin
from .ask import CfgAskMixin
from .txn import CfgTxnMixin
from .apply import CfgApplyMixin
from .uplink import CfgUplinkMixin
from .uplinktxn import CfgUplinkTxnMixin
from .rekey import CfgRekeyMixin
from .token import CfgTokenMixin


class CfgMixin(CfgPrintsMixin, CfgMailPhraseMixin, CfgAskMixin, CfgTxnMixin, CfgApplyMixin, CfgUplinkMixin,
               CfgUplinkTxnMixin, CfgRekeyMixin, CfgTokenMixin):
    """Документ конфигурации на агенте — одним миксином в GatewayServices."""


__all__ = ["CfgMixin", "CfgPrintsMixin", "CfgMailPhraseMixin", "CfgAskMixin", "CfgTxnMixin", "CfgApplyMixin",
           "CfgUplinkMixin", "CfgUplinkTxnMixin", "CfgRekeyMixin", "CfgTokenMixin"]
