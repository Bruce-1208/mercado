from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit


_YANDEX_PRODUCT_IMAGE = re.compile(r"^(/get-mpic/\d+/[^/]+)(?:/[^/?#]+)?$")


def normalize_product_pictures(values: Iterable[str]) -> list[str]:
    """Keep only actual Yandex product media and canonicalize it to the original image."""
    pictures: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            parsed = urlsplit(value.strip())
        except ValueError:
            continue
        if parsed.scheme != "https" or parsed.hostname != "avatars.mds.yandex.net":
            continue
        match = _YANDEX_PRODUCT_IMAGE.match(parsed.path)
        if not match:
            continue
        normalized = urlunsplit(("https", parsed.netloc, f"{match.group(1)}/orig", "", ""))
        if normalized not in seen:
            seen.add(normalized)
            pictures.append(normalized)
        if len(pictures) >= 30:
            break
    return pictures
