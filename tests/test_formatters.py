from __future__ import annotations

from services.sheets import MenuItem
from utils.formatters import format_item_caption, format_price_try


def _item(**overrides) -> MenuItem:
    defaults = dict(
        item_id="item_001",
        merchant_id="rest_001",
        name="Борщ",
        description="Свёкла, капуста, говядина. Без глютена",
        price_try=180.0,
        photo_url="https://example.com/borsch.jpg",
        is_active=True,
        is_available=True,
    )
    defaults.update(overrides)
    return MenuItem(**defaults)


def test_format_price_try_integer_amount():
    assert format_price_try(180.0) == "180 ₺"


def test_format_price_try_fractional_amount():
    assert format_price_try(179.5) == "179.50 ₺"


def test_format_item_caption_includes_name_description_price():
    item = _item()
    caption = format_item_caption(item, index=0, total=3)

    assert "<b>Борщ</b>" in caption
    assert "Свёкла, капуста, говядина. Без глютена" in caption
    assert "180 ₺" in caption


def test_format_item_caption_marks_unavailable_item():
    item = _item(is_available=False)
    caption = format_item_caption(item, index=0, total=1)

    assert "Недоступно сейчас" in caption


def test_format_item_caption_omits_empty_description():
    item = _item(description="")
    caption = format_item_caption(item, index=0, total=1)

    lines = [line for line in caption.split("\n") if line.strip()]
    assert lines[0] == "<b>Борщ</b>"
    assert lines[1] == "180 ₺"
