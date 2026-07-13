"""Скриншоты к шагам гайда: маппинг и наличие файлов."""
from pathlib import Path
from awgbot.bot import guides


def test_apple_region_steps_have_images():
    assert guides.step_image("apple", 1) == "apple_region_1.jpg"
    assert guides.step_image("apple", 2) == "apple_region_2.jpg"
    assert guides.step_image("apple", 3) == "apple_region_3.jpg"


def test_non_image_steps_return_none():
    assert guides.step_image("apple", 0) is None
    assert guides.step_image("apple", 4) is None
    assert guides.step_image("android", 1) is None
    assert guides.step_image("connect_apple", 1) is None


def test_image_files_exist_in_assets():
    assets = Path(__file__).resolve().parents[2] / "awgbot" / "assets" / "guides"
    for step in (1, 2, 3):
        name = guides.step_image("apple", step)
        assert (assets / name).exists(), f"нет файла {name}"
