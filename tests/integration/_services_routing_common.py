"""Общие помощники тестов условной маршрутизации (test_services_routing_*):
src-набор профиля, устройство профиля.
"""


def _srcset(client_id):
    """Имя src-набора пользователя: общего набора больше нет — каждый включённый
    профиль получает своё правило и свой набор адресов."""
    from awgbot.infra import routing as _rt
    return _rt.src_set(client_id)


def _device(services, client, name="Телефон"):
    return services.add_device(client.id, name)
