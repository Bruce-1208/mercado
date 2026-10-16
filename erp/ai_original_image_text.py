"""Local text removal and fail-closed OCR verification for listing covers."""
import threading

TEXT_POLICY_VERSION = "text-free-v1"
_engine = None
_lock = threading.Lock()


def detect_text(image):
    """Return text polygons; serialize access to the shared ONNX engine."""
    global _engine
    try:
        import cv2
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError("缺少主图去字依赖，请安装 bit/requirements-server.txt") from exc
    with _lock:
        if _engine is None:
            _engine = RapidOCR()
        result, _ = _engine(cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR))
    return [row[0] for row in (result or []) if str(row[1]).strip()]


def remove_cover_text(image):
    """Erase detected text, including product printing, then check the result.

    OCR cannot prove that every possible glyph is absent. Failed inference or
    text remaining after two repairs must never produce a successful cover.
    """
    import cv2
    import numpy as np
    from PIL import Image

    cleaned = image.convert("RGB")
    for attempt in range(3):
        polygons = detect_text(cleaned)
        if not polygons:
            return cleaned
        if attempt == 2:
            raise ValueError("主图仍检测到文字，去字未通过，请更换无字原图后重试")
        mask = np.zeros((cleaned.height, cleaned.width), dtype=np.uint8)
        for polygon in polygons:
            points = np.rint(polygon).astype(np.int32)
            if points.shape != (4, 2):
                raise ValueError("主图文字识别区域无效，无法确认去字结果")
            cv2.fillPoly(mask, [points], 255)
        # Include antialiased glyph edges and outlines around the OCR boxes.
        mask = cv2.dilate(mask, np.ones((7, 7), np.uint8))
        cleaned = Image.fromarray(cv2.inpaint(np.array(cleaned), mask, 5, cv2.INPAINT_TELEA))
    raise AssertionError("unreachable")
