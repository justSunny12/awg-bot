"""Общие помощники тестов переезда профилей на второй интерфейс
(test_migration_*): хендшейк устройства и двойник устройства.
"""


def _seen(services, device_id, ago_days=0):
    """Отметить хендшейк: «подключался ago_days назад»."""
    from awgbot.util import timeutil
    ts = int(timeutil.now().timestamp()) - int(ago_days * 86400)
    services.db.update_device_fields(device_id, last_handshake=ts)


def _twin(services, old_id):
    return services.db.get_device(services.db.twins_by_origin()[old_id])
