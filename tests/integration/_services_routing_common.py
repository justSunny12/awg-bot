"""Общие помощники тестов условной маршрутизации (test_services_routing_*):
src-набор профиля, устройство профиля, «все устройства» раздела.
"""


def _srcset(client_id):
    """Имя src-набора пользователя: общего набора больше нет — каждый включённый
    профиль получает своё правило и свой набор адресов."""
    from awgbot.infra import routing as _rt
    return _rt.src_set(client_id)


def _device(services, client, name="Телефон"):
    return services.add_device(client.id, name)


def _routing_all(services, client_id, on, devices=None) -> int:
    """«Все устройства» и «✅ Применить» раздела РФ-доступа: отметки всех устройств
    из списка раздела (routing_devices — свои непереданные и удерживаемые, без
    шлюза) — одной операцией set_routing_devices, как routing_apply. `devices` —
    свои id вместо списка раздела: флаг устройства профиля без разрешения
    (раздел его не покажет, но флаг ждёт разрешения)."""
    ids = devices if devices is not None else [d.id for d in services.routing_devices(client_id)]
    return services.set_routing_devices({int(i): bool(on) for i in ids})
