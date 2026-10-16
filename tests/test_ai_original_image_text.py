from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from erp import ai_original_image_text as text
from erp import ai_original_products as products


def test_removes_printing_without_changing_surrounding_product(monkeypatch):
    image = Image.new("RGB", (240, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 210, 210), fill=(30, 90, 180))
    draw.text((80, 100), "SALE 123", fill="white")
    detections = iter([[[[75, 95], [140, 95], [140, 115], [75, 115]]], []])
    monkeypatch.setattr(text, "detect_text", lambda image: next(detections))
    result = text.remove_cover_text(image)
    assert result.getpixel((0, 0)) == (255, 255, 255)
    assert result.getpixel((40, 40)) == (30, 90, 180)
    assert max(abs(a-b) for a, b in zip(result.getpixel((100, 105)), (30, 90, 180))) < 15


@pytest.mark.parametrize("ai", [False, True])
def test_residual_text_prevents_cover_being_saved(monkeypatch, tmp_path, ai):
    monkeypatch.setattr(text, "detect_text", lambda image: [[[20, 20], [80, 20], [80, 40], [20, 40]]])
    buffer = BytesIO()
    Image.new("RGB", (100, 100), "white").save(buffer, "PNG")
    options = dict(source_bytes=buffer.getvalue(), image_dir=tmp_path)
    if ai:
        generate = products.generate_ai_white_background_image
        options["image_generate"] = lambda **kw: buffer.getvalue()
    else:
        generate = products.create_white_background_image
        options["background_remove"] = lambda image: image.convert("RGBA")
    with pytest.raises(ValueError, match="仍检测到文字"):
        generate("https://cbu01.alicdn.com/source.png", "123456", **options)
    assert not list(tmp_path.iterdir())


def test_ocr_failure_is_not_treated_as_no_text(monkeypatch):
    def fail(image):
        raise RuntimeError("OCR unavailable")
    monkeypatch.setattr(text, "detect_text", fail)
    with pytest.raises(RuntimeError, match="OCR unavailable"):
        text.remove_cover_text(Image.new("RGB", (100, 100), "white"))
