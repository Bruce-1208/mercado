"""Copy a Mercado Libre listing into an authorized seller account.

OAuth credentials are read from environment variables and tokens are kept in an
ignored local JSON file.  The module supports both local Mercado Libre sellers
(``POST /items``) and Global Selling sellers (``POST /global/items``).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, urlparse

import requests

from erp.mercadolibre_attribute_rules import (
    is_read_only_attribute,
    is_required_attribute,
    match_enumerated_value,
    normalize_rule_key,
    resolve_schema_attribute_id,
    semantic_value_key,
)
from erp.mercadolibre_translation import (
    ListingTranslationError,
    marketplace_language,
    normalize_marketplace_site,
    translate_texts,
)


API_BASE_URL = "https://api.mercadolibre.com"
DEFAULT_REDIRECT_URI = "https://wuhanzeshun.com/zs"
DEFAULT_TOKEN_FILE = Path(__file__).with_name("tokens.json")
ITEM_ID_PATTERN = re.compile(r"\b(ML[A-Z]|CBT)-?(\d+)\b", re.IGNORECASE)


class MercadoLibreError(RuntimeError):
    """A Mercado Libre API request failed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def extract_item_id(value: str) -> str:
    """Return a normalized item id from an id or listing URL."""
    match = ITEM_ID_PATTERN.search(value or "")
    if not match:
        raise ValueError(f"无法从链接中识别 Mercado Libre 商品编号: {value!r}")
    return f"{match.group(1).upper()}{match.group(2)}"


def extract_authorization_code(value: str) -> str:
    """Return the TG authorization code from a callback URL or raw code."""
    raw = (value or "").strip()
    if raw.startswith("TG-"):
        return raw
    values = parse_qs(urlparse(raw).query).get("code", [])
    if values and values[0].startswith("TG-"):
        return values[0]
    raise ValueError("授权回调链接中缺少有效的 TG code")


def save_tokens(path: Path, data: Mapping[str, Any]) -> None:
    """Atomically save an OAuth response without printing its secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(dict(data), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def load_tokens(path: Path) -> dict[str, Any]:
    """Load a saved OAuth response."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MercadoLibreError(f"token 文件不存在: {path}") from exc


def exchange_authorization_code(
    callback_or_code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    token_file: Path = DEFAULT_TOKEN_FILE,
    session: requests.Session | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Exchange a one-time authorization code and persist the token response."""
    code = extract_authorization_code(callback_or_code)
    http = session or requests.Session()
    response = http.post(
        f"{API_BASE_URL}/oauth/token",
        headers={"Accept": "application/json"},
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=timeout,
    )
    if not response.ok:
        raise MercadoLibreError(
            f"兑换授权码失败 (HTTP {response.status_code}): {_api_message(response)}"
        )
    data = response.json()
    if not data.get("access_token"):
        raise MercadoLibreError("兑换授权码成功，但响应中没有 access_token")
    save_tokens(token_file, data)
    return data


def _api_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:1000]
    if not isinstance(payload, dict):
        return str(payload)[:1000]
    message = payload.get("message") or payload.get("error")
    details = payload.get("cause")
    if message and details:
        return f"{message}; cause={json.dumps(details, ensure_ascii=False, default=str)}"[:2000]
    return str(message or details or payload)[:2000]


class MercadoLibreClient:
    """Small authenticated API client with automatic access-token refresh."""

    def __init__(
        self,
        token_file: Path,
        *,
        client_id: str,
        client_secret: str,
        session: requests.Session | None = None,
        timeout: int = 30,
    ) -> None:
        self.token_file = token_file
        self.tokens = load_tokens(token_file)
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self.timeout = timeout
        self._uploaded_picture_metadata: dict[str, Mapping[str, Any]] = {}

    def _refresh(self) -> None:
        refresh_token = self.tokens.get("refresh_token")
        if not refresh_token:
            raise MercadoLibreError("access token 已失效，且没有 refresh token")
        response = self.session.post(
            f"{API_BASE_URL}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
            },
            timeout=self.timeout,
        )
        if not response.ok:
            raise MercadoLibreError(
                f"刷新 token 失败 (HTTP {response.status_code}): {_api_message(response)}"
            )
        self.tokens = response.json()
        save_tokens(self.token_file, self.tokens)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        url = path if path.startswith("http") else f"{API_BASE_URL}{path}"
        method_name = method.upper()
        auth_refreshed = False
        transient_retries = 0
        while True:
            headers = {"Accept": "application/json"}
            if authenticated:
                headers["Authorization"] = f"Bearer {self.tokens['access_token']}"
            try:
                response = self.session.request(
                    method_name,
                    url,
                    params=dict(params or {}),
                    json=dict(json_body) if json_body is not None else None,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                is_idempotent = method_name in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
                if is_idempotent and transient_retries < 3:
                    delay = min(8.0, 2 ** transient_retries) + random.uniform(0, 0.5)
                    transient_retries += 1
                    time.sleep(delay)
                    continue
                raise MercadoLibreError(
                    f"{method_name} {path} 请求异常: {exc}"
                ) from exc
            if authenticated and response.status_code == 401 and not auth_refreshed:
                self._refresh()
                auth_refreshed = True
                continue
            retryable_status = response.status_code == 429 or (
                500 <= response.status_code < 600
                and method_name in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
            )
            if retryable_status and transient_retries < 3:
                retry_after = str(response.headers.get("Retry-After") or "").strip()
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = min(8.0, 2 ** transient_retries)
                delay = max(0.25, min(delay, 30.0)) + random.uniform(0, 0.5)
                transient_retries += 1
                time.sleep(delay)
                continue
            if not response.ok:
                raise MercadoLibreError(
                    f"{method_name} {path} 失败 (HTTP {response.status_code}): "
                    f"{_api_message(response)}",
                    status_code=int(response.status_code),
                )
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    def upload_picture_from_url(self, source_url: str) -> str:
        """Download a source image and upload it for a User Products listing."""
        parsed_source = urlparse(str(source_url or ""))
        image_bytes = b""
        content_type = "image/jpeg"
        if (
            (
                not parsed_source.netloc
                or parsed_source.hostname in {"127.0.0.1", "localhost"}
            )
            and parsed_source.path.startswith("/api/ai-original-products/images/")
        ):
            # AI-original images are owned by this process. Reading the
            # validated file directly avoids a loopback HTTP deadlock when the
            # workbench server is running with a single request worker.
            from erp.ai_original_products import IMAGE_DIR

            filename = Path(parsed_source.path).name
            if not re.fullmatch(r"1688-[A-Za-z0-9_-]+-(?:ai-)?white\.jpg", filename):
                raise MercadoLibreError(f"AI 原创图片地址无效: {source_url}")
            local_path = (IMAGE_DIR / filename).resolve()
            if local_path.parent != IMAGE_DIR.resolve() or not local_path.is_file():
                raise MercadoLibreError(f"AI 原创图片不存在: {source_url}")
            image_bytes = local_path.read_bytes()
        else:
            source_response = self.session.get(source_url, timeout=self.timeout)
            if not source_response.ok:
                raise MercadoLibreError(
                    f"下载源图片失败 (HTTP {source_response.status_code}): {source_url}"
                )
            image_bytes = source_response.content
            content_type = source_response.headers.get(
                "Content-Type", "image/jpeg"
            ).split(";", 1)[0]
        if len(image_bytes) > 10 * 1024 * 1024:
            raise MercadoLibreError(f"源图片超过 10 MB: {source_url}")
        if content_type not in {"image/jpeg", "image/jpg", "image/png", "image/webp"}:
            raise MercadoLibreError(f"源图片格式不受支持 ({content_type}): {source_url}")
        try:
            from PIL import Image, ImageOps

            with Image.open(io.BytesIO(image_bytes)) as image:
                image = ImageOps.exif_transpose(image)
                width, height = image.size
                if max(width, height) < 500:
                    raise MercadoLibreError(
                        f"源图片尺寸不足 500px ({width}x{height}): {source_url}"
                    )
                # Mercado's image processing can shave one or two pixels from a
                # boundary-size image. Keep a small margin so valid 500px source
                # pictures are not rejected later as 497-499px.
                needs_resize = max(width, height) < 520
                if needs_resize or content_type == "image/webp":
                    if needs_resize:
                        scale = 520 / max(width, height)
                        image = image.resize(
                            (round(width * scale), round(height * scale)),
                            Image.Resampling.LANCZOS,
                        )
                    converted = io.BytesIO()
                    if content_type == "image/png":
                        image.save(converted, format="PNG", optimize=True)
                    else:
                        image.convert("RGB").save(converted, format="JPEG", quality=95)
                        content_type = "image/jpeg"
                    image_bytes = converted.getvalue()
        except MercadoLibreError:
            raise
        except Exception as exc:
            raise MercadoLibreError(f"无法读取源图片尺寸: {source_url}") from exc
        extension = ".png" if content_type == "image/png" else ".jpg"
        for attempt in range(2):
            response = self.session.post(
                f"{API_BASE_URL}/pictures/items/upload",
                headers={"Authorization": f"Bearer {self.tokens['access_token']}"},
                files={
                    "file": (
                        f"follow-sell{extension}",
                        image_bytes,
                        content_type,
                    )
                },
                timeout=self.timeout,
            )
            if response.status_code == 401 and attempt == 0:
                self._refresh()
                continue
            if not response.ok:
                raise MercadoLibreError(
                    f"上传图片失败 (HTTP {response.status_code}): {_api_message(response)}"
                )
            data = response.json()
            picture_id = data.get("id")
            if not picture_id:
                raise MercadoLibreError("图片上传成功，但响应中没有图片 id")
            metadata_cache = getattr(self, "_uploaded_picture_metadata", None)
            if not isinstance(metadata_cache, dict):
                metadata_cache = {}
                self._uploaded_picture_metadata = metadata_cache
            metadata_cache[str(picture_id)] = dict(data)
            return str(picture_id)
        raise MercadoLibreError("上传图片认证失败")


READ_ONLY_ATTRIBUTE_KEYS = {
    "id",
    "name",
    "value_id",
    "value_name",
    "value_struct",
    "values",
}
SELLER_SPECIFIC_ATTRIBUTES = {"SELLER_SKU", "SKU"}
DEFAULT_BRAND = "Generic"
DEFAULT_REQUIRED_ATTRIBUTES = {
    "MODEL": {"id": "MODEL", "value_name": "Generic"},
    "COLOR": {"id": "COLOR", "value_name": "Multicolor"},
    "SIZE": {"id": "SIZE", "value_name": "One size"},
    "SEASON": {
        "id": "SEASON",
        "value_id": "994284",
        "value_name": "Autumn/Winter",
    },
    "DRESS_TYPE": {
        "id": "DRESS_TYPE",
        "value_id": "1149075",
        "value_name": "A-line/Evase",
    },
}
DEFAULT_SALE_TERMS = [
    {
        "id": "WARRANTY_TYPE",
        "value_id": "6150835",
        "value_name": "No warranty",
    }
]
MAX_PICTURES_PER_LISTING = 12


def _clean_attribute(attribute: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only fields accepted by create/update endpoints."""
    return {
        key: value
        for key, value in attribute.items()
        if key in READ_ONLY_ATTRIBUTE_KEYS and value is not None
    }


def _copy_attributes(
    attributes: Iterable[Mapping[str, Any]],
    *,
    seller_sku: str | None = None,
    allowed_ids: set[str] | None = None,
    schema: Iterable[Mapping[str, Any]] | None = None,
    source_schema: Iterable[Mapping[str, Any]] | None = None,
    ensure_brand: bool = True,
) -> list[dict[str, Any]]:
    source_definitions = {
        str(definition.get("id") or "").upper(): definition
        for definition in source_schema or []
        if definition.get("id")
    }
    copied: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for source_attribute in attributes:
        source_attribute_id = resolve_schema_attribute_id(source_attribute, source_schema)
        source_definition = source_definitions.get(source_attribute_id) or {}
        attribute = _clean_attribute(source_attribute)
        attribute["id"] = source_attribute_id
        _restore_source_enumerated_value_id(attribute, source_definition)
        attribute_id = resolve_schema_attribute_id(attribute, schema)
        if (
            not attribute_id
            or attribute_id in SELLER_SPECIFIC_ATTRIBUTES
            or (allowed_ids is not None and attribute_id not in allowed_ids)
        ):
            continue
        attribute["id"] = attribute_id
        existing_position = positions.get(attribute_id)
        if existing_position is None:
            positions[attribute_id] = len(copied)
            copied.append(attribute)
        elif not copied[existing_position].get("value_name") and attribute.get("value_name"):
            copied[existing_position] = attribute
    if ensure_brand:
        brand_position = positions.get("BRAND")
        if brand_position is None:
            copied.append({"id": "BRAND", "value_name": DEFAULT_BRAND})
        else:
            # Follow-sell products intentionally publish under the generic brand;
            # source-page brand text is often seller-entered or malformed.
            copied[brand_position].pop("value_id", None)
            copied[brand_position]["value_name"] = DEFAULT_BRAND
    if seller_sku:
        copied.append({"id": "SELLER_SKU", "value_name": seller_sku})
    return copied


def _restore_source_enumerated_value_id(
    attribute: dict[str, Any], definition: Mapping[str, Any]
) -> None:
    """Recover stable value IDs from the source category's localized schema."""
    attribute_id = str(definition.get("id") or attribute.get("id") or "").upper()
    allowed_values = [
        value
        for value in definition.get("values") or []
        if isinstance(value, Mapping) and value.get("id")
    ]
    if not allowed_values:
        return
    source_values = attribute.get("values")
    if isinstance(source_values, list) and source_values:
        restored: list[dict[str, Any]] = []
        for source_value in source_values:
            if not isinstance(source_value, Mapping):
                continue
            matched = match_enumerated_value(
                attribute_id,
                source_value.get("id"),
                source_value.get("name"),
                allowed_values,
            )
            restored.append(dict(matched or source_value))
        if restored:
            attribute["values"] = restored
        return
    matched = match_enumerated_value(
        attribute_id,
        attribute.get("value_id"),
        attribute.get("value_name"),
        allowed_values,
    )
    if matched is not None:
        attribute["value_id"] = str(matched["id"])


def _ensure_item_condition(
    attributes: list[dict[str, Any]], condition: str | None
) -> None:
    if any(attribute.get("id") == "ITEM_CONDITION" for attribute in attributes):
        return
    if (condition or "new").lower() == "new":
        attributes.append(
            {
                "id": "ITEM_CONDITION",
                "value_id": "2230284",
                "value_name": "New",
            }
        )


def _ensure_gtin_or_empty_reason(attributes: list[dict[str, Any]]) -> None:
    """Declare the documented no-GTIN reason when the source exposes no code."""
    if any(
        str(attribute.get("id") or "").upper() in {"GTIN", "EMPTY_GTIN_REASON"}
        and _attribute_has_value(attribute)
        for attribute in attributes
    ):
        return
    # Do not let a collected placeholder such as valueid=-1 block the valid
    # EMPTY_GTIN_REASON alternative or leak into the publication request.
    attributes[:] = [
        attribute
        for attribute in attributes
        if str(attribute.get("id") or "").upper() != "GTIN"
    ]
    attributes.append(
        {
            "id": "EMPTY_GTIN_REASON",
            "value_id": "17055160",
            "value_name": "The product does not have registered code",
        }
    )


def _mercado_picture_identity(url: str) -> str:
    """Collapse thumbnail/full-size variants of one Mercado product photo."""
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    match = re.match(
        r"D_(?:NQ|Q)_NP(?:_2X)?_(.+?)-(?:[OFR])(?:-[^.]+)?\.(?:jpe?g|png|webp)$",
        filename,
        flags=re.IGNORECASE,
    )
    return f"meli:{match.group(1).lower()}" if match else url.lower()


def _picture_priority(url: str) -> int:
    filename = urlparse(url).path.rsplit("/", 1)[-1].lower()
    if re.search(r"-f(?:-[^.]+)?\.(?:jpe?g|png|webp)$", filename):
        return 3
    if re.search(r"-o(?:-[^.]+)?\.(?:jpe?g|png|webp)$", filename):
        return 2
    return 1


def _publishable_picture_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url or url.startswith(("data:", "blob:")):
        return ""
    if url.startswith("//"):
        url = f"https:{url}"
    lowered = url.lower()
    if "/storage/logos-api-admin/" in lowered:
        return ""
    if urlparse(lowered).path.endswith(".svg"):
        return ""
    return url.replace("http://", "https://", 1)


def _picture_sources(item: Mapping[str, Any]) -> tuple[list[dict[str, str]], dict[str, str]]:
    selected: dict[str, tuple[str, int]] = {}
    ids_to_urls: dict[str, str] = {}
    for picture in item.get("pictures") or []:
        if not isinstance(picture, Mapping):
            continue
        # API snapshots use secure_url/url; browser-collected snapshots use
        # source because that is also the create-listing payload field.
        url = _publishable_picture_url(
            picture.get("secure_url") or picture.get("url") or picture.get("source")
        )
        if not url:
            continue
        identity = _mercado_picture_identity(url)
        priority = _picture_priority(url)
        previous = selected.get(identity)
        if previous is None or priority > previous[1]:
            selected[identity] = (url, priority)
        if picture.get("id"):
            ids_to_urls[str(picture["id"])] = url
    picture_urls = [url for url, _ in selected.values()][:MAX_PICTURES_PER_LISTING]
    pictures = [{"source": url} for url in picture_urls]
    if not pictures:
        raise MercadoLibreError("源商品没有可复制的图片")
    return pictures, ids_to_urls


def _validate_uploaded_picture_dimensions(
    client: MercadoLibreClient, picture_id: str
) -> tuple[int, int]:
    """Verify dimensions after Mercado has transcoded an uploaded picture."""
    metadata_cache = getattr(client, "_uploaded_picture_metadata", {})
    metadata = metadata_cache.get(picture_id) if isinstance(metadata_cache, Mapping) else None
    if not isinstance(metadata, Mapping):
        metadata = client.request("GET", f"/pictures/{picture_id}")
    max_size = str(metadata.get("max_size") or "") if isinstance(metadata, Mapping) else ""
    match = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", max_size, re.IGNORECASE)
    if not match:
        raise MercadoLibreError(
            f"无法确认 Mercado 上传图片尺寸 ({picture_id}): {max_size or '(empty)'}"
        )
    width, height = int(match.group(1)), int(match.group(2))
    if max(width, height) < 500:
        raise MercadoLibreError(
            f"Mercado 处理后图片尺寸不足 500px ({width}x{height}): {picture_id}"
        )
    return width, height


def _description_text(description: Any) -> str:
    if not isinstance(description, Mapping):
        return ""
    return str(description.get("plain_text") or description.get("text") or "").strip()


def _try_request(client: MercadoLibreClient, method: str, path: str, **kwargs: Any) -> Any:
    try:
        return client.request(method, path, **kwargs)
    except MercadoLibreError:
        return None


_USER_PROFILE_CACHE_LOCK = threading.Lock()
_USER_PROFILE_CACHE: dict[int, tuple[float, Mapping[str, Any]]] = {}
_CATEGORY_CACHE_LOCK = threading.Lock()
_CATEGORY_SCHEMA_CACHE: dict[str, tuple[float, list[Mapping[str, Any]]]] = {}
_CATEGORY_PREDICTION_CACHE: dict[str, tuple[float, list[Mapping[str, Any]]]] = {}
_CATEGORY_KEY_LOCKS: dict[str, threading.Lock] = {}
_PICTURE_CACHE_LOCK = threading.Lock()
_PICTURE_ID_CACHE: dict[tuple[int, str], tuple[float, str]] = {}
_PICTURE_KEY_LOCKS: dict[tuple[int, str], threading.Lock] = {}
_CACHE_TTL_SECONDS = 24 * 60 * 60
_PICTURE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

_CATEGORY_QUERY_PHRASES = {
    "aurora boreal": "northern lights",
    "cielo estrellado": "starry sky",
    "ceu estrelado": "starry sky",
    "luz nocturna": "night light",
    "luz noturna": "night light",
    "onda de agua": "water ripple",
    "onda d agua": "water ripple",
}
_CATEGORY_QUERY_WORDS = {
    "agua": "water",
    "bateria": "battery",
    "decoracion": "decoration",
    "decoracao": "decoration",
    "efecto": "effect",
    "efeito": "effect",
    "estrella": "star",
    "estrellas": "stars",
    "estrela": "star",
    "estrelas": "stars",
    "galactico": "galaxy",
    "galaxia": "galaxy",
    "habitacion": "room",
    "lampada": "lamp",
    "lampara": "lamp",
    "luz": "light",
    "nocturna": "night",
    "noturna": "night",
    "proyector": "projector",
    "projetor": "projector",
}
_CATEGORY_QUERY_STOPWORDS = {
    "a", "as", "con", "da", "das", "de", "del", "do", "dos", "el", "en",
    "la", "las", "o", "os", "para", "por", "um", "uma", "un", "una", "y",
}


def _english_category_prediction_query(value: Any) -> str:
    """Build an English CBT discovery fallback without an external translator."""
    normalized = unicodedata.normalize("NFKD", str(value or "").lower())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    for source_phrase, target_phrase in _CATEGORY_QUERY_PHRASES.items():
        normalized = re.sub(
            rf"\b{re.escape(source_phrase)}\b", target_phrase, normalized
        )
    words = [
        _CATEGORY_QUERY_WORDS.get(word, word)
        for word in normalized.split()
        if word not in _CATEGORY_QUERY_STOPWORDS
    ]
    return " ".join(words)


def _category_prediction_queries(
    source: Mapping[str, Any],
    translator: Callable[..., Any] | None = None,
) -> list[str]:
    """Return safe predictor queries, preferring the API's required English.

    Mercado explicitly documents that CBT category prediction expects a fully
    English product title.  A non-English title can return a plausible but
    unrelated leaf category (for example Spanish ``obsidiana`` has been
    classified as natural grass).  When the offline model is unavailable we
    prefer the much less noisy collected category name before the title.
    """
    category_name = str(source.get("category_name") or "").strip()
    title = str(
        source.get("_category_prediction_title") or source.get("title") or ""
    ).strip()
    source_site = str(
        source.get("site_id") or source.get("id") or source.get("category_id") or ""
    )[:3].upper()
    source_language = marketplace_language(source_site)
    translated_values: list[str] = []
    originals = [value for value in (title, category_name) if value]
    if originals and source_language and source_language != "en":
        try:
            translated_values = translate_texts(
                originals,
                source_language,
                "en",
                translator=translator,
            )
        except ListingTranslationError:
            # Existing deployments may not yet have the new es/pt -> en model.
            # Falling back to the category label is safer than blocking every
            # listing or trusting a long non-English title.
            translated_values = []

    values: list[str] = []
    if translated_values:
        # The official predictor is trained for a full English product title.
        values.extend(translated_values)
    values.extend((category_name, title))
    queries: list[str] = []
    for value in values:
        original = str(value or "").strip()
        if "\ufffd" in original:
            continue
        translated = _english_category_prediction_query(original)
        for query in (original, translated):
            if query and query not in queries:
                queries.append(query)
    return queries


def _category_predictions(
    client: MercadoLibreClient, query: str
) -> list[Mapping[str, Any]]:
    """Fetch one predictor response with a process-wide TTL cache."""
    token_id = int(getattr(client, "token_id", 0) or 0)
    if token_id <= 0:
        suggestions = client.request(
            "GET",
            "/marketplace/domain_discovery/search",
            params={"q": query},
        )
        return (
            [row for row in suggestions if isinstance(row, Mapping)]
            if isinstance(suggestions, list)
            else []
        )
    cache_key = normalize_rule_key(query)
    now = time.monotonic()
    with _CATEGORY_CACHE_LOCK:
        cached = _CATEGORY_PREDICTION_CACHE.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        key_lock = _CATEGORY_KEY_LOCKS.setdefault(
            f"prediction:{cache_key}", threading.Lock()
        )
    with key_lock:
        with _CATEGORY_CACHE_LOCK:
            cached = _CATEGORY_PREDICTION_CACHE.get(cache_key)
            if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
                return cached[1]
        suggestions = client.request(
            "GET",
            "/marketplace/domain_discovery/search",
            params={"q": query},
        )
        normalized = (
            [row for row in suggestions if isinstance(row, Mapping)]
            if isinstance(suggestions, list)
            else []
        )
        with _CATEGORY_CACHE_LOCK:
            _CATEGORY_PREDICTION_CACHE[cache_key] = (time.monotonic(), normalized)
            if len(_CATEGORY_PREDICTION_CACHE) > 5000:
                oldest = min(
                    _CATEGORY_PREDICTION_CACHE,
                    key=lambda key: _CATEGORY_PREDICTION_CACHE[key][0],
                )
                _CATEGORY_PREDICTION_CACHE.pop(oldest, None)
                _CATEGORY_KEY_LOCKS.pop(f"prediction:{oldest}", None)
        return normalized


def _cached_user_profile(client: MercadoLibreClient) -> Mapping[str, Any]:
    instance_profile = getattr(client, "_cached_user_profile_value", None)
    if isinstance(instance_profile, Mapping):
        return instance_profile
    token_id = int(getattr(client, "token_id", 0) or 0)
    if token_id <= 0:
        profile = client.request("GET", "/users/me")
        client._cached_user_profile_value = profile
        return profile
    now = time.monotonic()
    with _USER_PROFILE_CACHE_LOCK:
        cached = _USER_PROFILE_CACHE.get(token_id)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            client._cached_user_profile_value = cached[1]
            return cached[1]
        profile = client.request("GET", "/users/me")
        _USER_PROFILE_CACHE[token_id] = (time.monotonic(), profile)
        client._cached_user_profile_value = profile
        return profile


def _upload_validated_picture(client: MercadoLibreClient, source_url: str) -> str:
    """Upload once per account/source URL and reuse only dimension-checked IDs."""
    token_id = int(getattr(client, "token_id", 0) or 0)
    if token_id <= 0:
        picture_id = client.upload_picture_from_url(source_url)
        _validate_uploaded_picture_dimensions(client, picture_id)
        return picture_id

    key = (token_id, _mercado_picture_identity(source_url))
    now = time.monotonic()
    with _PICTURE_CACHE_LOCK:
        cached = _PICTURE_ID_CACHE.get(key)
        if cached and now - cached[0] < _PICTURE_CACHE_TTL_SECONDS:
            return cached[1]
        key_lock = _PICTURE_KEY_LOCKS.setdefault(key, threading.Lock())
    with key_lock:
        with _PICTURE_CACHE_LOCK:
            cached = _PICTURE_ID_CACHE.get(key)
            if cached and time.monotonic() - cached[0] < _PICTURE_CACHE_TTL_SECONDS:
                return cached[1]
        picture_id = client.upload_picture_from_url(source_url)
        _validate_uploaded_picture_dimensions(client, picture_id)
        with _PICTURE_CACHE_LOCK:
            _PICTURE_ID_CACHE[key] = (time.monotonic(), picture_id)
            if len(_PICTURE_ID_CACHE) > 10000:
                oldest = min(
                    _PICTURE_ID_CACHE,
                    key=lambda cache_key: _PICTURE_ID_CACHE[cache_key][0],
                )
                _PICTURE_ID_CACHE.pop(oldest, None)
                _PICTURE_KEY_LOCKS.pop(oldest, None)
        return picture_id


def fetch_source_listing(
    client: MercadoLibreClient, source: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read a marketplace listing and its description with the target token."""
    item_id = extract_item_id(source)
    params = {"include_attributes": "all", "include_internal_attributes": "true"}
    item = _try_request(
        client, "GET", f"/marketplace/items/{item_id}", params=params
    )
    if not isinstance(item, dict) or not item.get("id"):
        item = client.request("GET", f"/items/{item_id}", params=params)
    description = _try_request(client, "GET", f"/items/{item_id}/description")
    return item, description if isinstance(description, dict) else {}


def _infer_cbt_listing(
    client: MercadoLibreClient,
    source: Mapping[str, Any],
    translator: Callable[..., Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Predict the CBT category and retain attributes inferred by Mercado."""
    category_id = str(source.get("category_id") or "")
    if category_id.startswith("CBT"):
        return category_id, []

    # Site category trees are independent.  A local category number is not a
    # CBT mapping, even if a CBT category with the same numeric suffix happens
    # to exist.  Always use Mercado's supported predictor for cross-site input.
    candidate_order = 0
    for query in _category_prediction_queries(source, translator=translator):
        suggestions = [
            suggestion
            for suggestion in _category_predictions(client, query)[:5]
            if str(suggestion.get("category_id") or "").strip().upper().startswith("CBT")
        ]
        query_best: tuple[int, int, str, list[dict[str, Any]]] | None = None
        for suggestion in suggestions:
            suggested = str(suggestion.get("category_id") or "").strip().upper()
            inferred = [
                dict(attribute)
                for attribute in suggestion.get("attributes") or []
                if isinstance(attribute, Mapping) and attribute.get("id")
            ]
            schema = _category_attribute_schema(client, suggested)
            if schema is None:
                missing_count = 10000
            else:
                candidate_source = _source_with_inferred_attributes(source, inferred)
                present = {
                    resolve_schema_attribute_id(attribute, schema)
                    for attribute in candidate_source.get("attributes") or []
                    if isinstance(attribute, Mapping) and _attribute_has_value(attribute)
                }
                for variation in candidate_source.get("variations") or []:
                    if not isinstance(variation, Mapping):
                        continue
                    for key in ("attribute_combinations", "attributes"):
                        present.update(
                            resolve_schema_attribute_id(attribute, schema)
                            for attribute in variation.get(key) or []
                            if isinstance(attribute, Mapping)
                            and _attribute_has_value(attribute)
                        )
                required = {
                    str(definition.get("id") or "").strip().upper()
                    for definition in schema
                    if definition.get("id")
                    and is_required_attribute(definition)
                    and not is_read_only_attribute(definition)
                }
                satisfiable = set(DEFAULT_REQUIRED_ATTRIBUTES) | {"BRAND"}
                if "EMPTY_GTIN_REASON" in {
                    str(definition.get("id") or "").strip().upper()
                    for definition in schema
                }:
                    satisfiable.add("GTIN")
                missing_count = len(required - present - satisfiable)
            ranked = (missing_count, candidate_order, suggested, inferred)
            candidate_order += 1
            if query_best is None or ranked[:2] < query_best[:2]:
                query_best = ranked
            if missing_count == 0:
                return suggested, inferred
        # Do not jump from a recognized collected category label to a noisy
        # non-English title merely because an unrelated category happens to
        # need fewer attributes.  With no offline English model, failing with
        # an actionable missing field is safer than publishing in a wrong leaf.
        if query_best is not None:
            return query_best[2], query_best[3]
    raise MercadoLibreError(f"无法把源类目 {category_id or '(empty)'} 映射为 CBT 类目")


def infer_cbt_category(
    client: MercadoLibreClient,
    source: Mapping[str, Any],
    translator: Callable[..., Any] | None = None,
) -> str:
    """Map a marketplace category to its CBT counterpart."""
    category_id, _ = _infer_cbt_listing(client, source, translator=translator)
    return category_id


def _source_with_inferred_attributes(
    source: Mapping[str, Any], inferred: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Merge predictor evidence without overwriting collected source values."""
    merged_source = dict(source)
    attributes = [
        dict(attribute)
        for attribute in source.get("attributes") or []
        if isinstance(attribute, Mapping)
    ]
    positions = {
        str(attribute.get("id") or "").strip().upper(): index
        for index, attribute in enumerate(attributes)
        if str(attribute.get("id") or "").strip()
    }
    for candidate in inferred:
        attribute = dict(candidate)
        attribute_id = str(attribute.get("id") or "").strip().upper()
        if not attribute_id:
            continue
        attribute["id"] = attribute_id
        position = positions.get(attribute_id)
        if position is None:
            positions[attribute_id] = len(attributes)
            attributes.append(attribute)
        elif not _attribute_has_value(attributes[position]) and _attribute_has_value(
            attribute
        ):
            attributes[position] = attribute
    merged_source["attributes"] = attributes
    return merged_source


def _category_attribute_schema(
    client: MercadoLibreClient, category_id: str
) -> list[Mapping[str, Any]] | None:
    instance_cache = getattr(client, "_category_schema_cache", None)
    if not isinstance(instance_cache, dict):
        instance_cache = {}
        client._category_schema_cache = instance_cache
    if category_id in instance_cache:
        return instance_cache[category_id]
    token_id = int(getattr(client, "token_id", 0) or 0)
    if token_id > 0:
        now = time.monotonic()
        with _CATEGORY_CACHE_LOCK:
            cached = _CATEGORY_SCHEMA_CACHE.get(category_id)
            if cached and now - cached[0] < _CACHE_TTL_SECONDS:
                instance_cache[category_id] = cached[1]
                return cached[1]
            key_lock = _CATEGORY_KEY_LOCKS.setdefault(
                f"schema:{category_id}", threading.Lock()
            )
        with key_lock:
            with _CATEGORY_CACHE_LOCK:
                cached = _CATEGORY_SCHEMA_CACHE.get(category_id)
                if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
                    instance_cache[category_id] = cached[1]
                    return cached[1]
            schema = _try_request(client, "GET", f"/categories/{category_id}/attributes")
            if isinstance(schema, list):
                normalized = [
                    attribute for attribute in schema if isinstance(attribute, Mapping)
                ]
                with _CATEGORY_CACHE_LOCK:
                    _CATEGORY_SCHEMA_CACHE[category_id] = (time.monotonic(), normalized)
                instance_cache[category_id] = normalized
                return normalized
            return None
    schema = _try_request(client, "GET", f"/categories/{category_id}/attributes")
    if not isinstance(schema, list):
        return None
    normalized = [attribute for attribute in schema if isinstance(attribute, Mapping)]
    instance_cache[category_id] = normalized
    return normalized


def _required_category_attribute_schema(
    client: MercadoLibreClient, category_id: str
) -> list[Mapping[str, Any]]:
    schema = _category_attribute_schema(client, category_id)
    if schema is None:
        raise MercadoLibreError(
            f"无法从 Mercado Libre API 获取类目 {category_id} 的属性规则；"
            "为避免漏传必填属性，本次上架已停止"
        )
    return schema


def _source_category_attribute_schema(
    client: MercadoLibreClient,
    source: Mapping[str, Any],
    target_category_id: str,
    target_schema: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]] | None:
    source_category_id = str(source.get("category_id") or "").strip().upper()
    if not source_category_id:
        return None
    if source_category_id == target_category_id:
        return target_schema
    # Source schemas are used to turn localized browser labels and values back
    # into Mercado's stable IDs. If the source endpoint is temporarily
    # unavailable we can still use explicit source IDs and the target schema.
    return _category_attribute_schema(client, source_category_id)


def _category_attribute_ids(
    schema: Iterable[Mapping[str, Any]] | None,
) -> set[str] | None:
    if schema is None:
        return None
    return {
        str(attribute.get("id") or "").upper()
        for attribute in schema
        if attribute.get("id")
    }


def _validate_required_attributes(
    attributes: Iterable[Mapping[str, Any]],
    schema: Iterable[Mapping[str, Any]] | None,
    variations: Iterable[Mapping[str, Any]] | None = None,
) -> None:
    if schema is None:
        return
    present = {
        str(attribute.get("id") or "").upper()
        for attribute in attributes
        if attribute.get("id")
        and (
            attribute.get("value_id") not in (None, "")
            or str(attribute.get("value_name") or "").strip()
            or attribute.get("values")
        )
    }
    variation_rows = list(variations or [])
    if variation_rows:
        per_variation = []
        for variation in variation_rows:
            per_variation.append({
                str(attribute.get("id") or "").upper()
                for key in ("attribute_combinations", "attributes")
                for attribute in variation.get(key) or []
                if attribute.get("id") and _attribute_has_value(attribute)
            })
        if per_variation:
            present.update(set.intersection(*per_variation))
    required_definitions = {
        str(attribute.get("id") or "").upper(): attribute
        for attribute in schema
        if attribute.get("id")
        and is_required_attribute(attribute)
        and not is_read_only_attribute(attribute)
    }
    required = set(required_definitions)
    # Mercado accepts EMPTY_GTIN_REASON as the documented alternative when a
    # product genuinely has no registered GTIN. Do not reject that valid pair
    # locally merely because the category schema also marks GTIN as required.
    if "EMPTY_GTIN_REASON" in present:
        required.discard("GTIN")
    missing = sorted(required - present)
    if missing:
        details = []
        for attribute_id in missing:
            definition = required_definitions[attribute_id]
            name = str(definition.get("name") or "").strip()
            value_type = str(definition.get("value_type") or "").strip()
            suffix = ", ".join(value for value in (name, value_type) if value)
            details.append(f"{attribute_id} ({suffix})" if suffix else attribute_id)
        raise MercadoLibreError(
            "源商品缺少目标类目必填属性: "
            + ", ".join(details)
            + "；请补全采集数据后再上架"
        )


def _attribute_has_value(attribute: Mapping[str, Any]) -> bool:
    return bool(
        attribute.get("value_id") not in (None, "")
        or str(attribute.get("value_name") or "").strip()
        or attribute.get("value_struct")
        or attribute.get("values")
    )


def _ensure_required_attribute_defaults(
    attributes: list[dict[str, Any]],
    schema: Iterable[Mapping[str, Any]] | None,
    variations: Iterable[Mapping[str, Any]] | None = None,
) -> None:
    """Fill safe publication fallbacks for common required catalog fields."""
    if schema is None:
        return
    present = {
        str(attribute.get("id") or "").upper()
        for attribute in attributes
        if attribute.get("id")
        and (
            attribute.get("value_id") not in (None, "")
            or str(attribute.get("value_name") or "").strip()
            or attribute.get("values")
        )
    }
    variation_rows = list(variations or [])
    if variation_rows:
        per_variation = [
            {
                str(attribute.get("id") or "").upper()
                for key in ("attribute_combinations", "attributes")
                for attribute in variation.get(key) or []
                if attribute.get("id") and _attribute_has_value(attribute)
            }
            for variation in variation_rows
        ]
        if per_variation:
            present.update(set.intersection(*per_variation))
    for definition in schema:
        attribute_id = str(definition.get("id") or "").upper()
        if (
            not attribute_id
            or attribute_id in present
            or is_read_only_attribute(definition)
            or not is_required_attribute(definition)
        ):
            continue
        default = DEFAULT_REQUIRED_ATTRIBUTES.get(attribute_id)
        if default:
            attributes.append(dict(default))
            present.add(attribute_id)


def _ensure_contextual_required_attribute_defaults(
    attributes: list[dict[str, Any]],
    schema: Iterable[Mapping[str, Any]] | None,
    source: Mapping[str, Any],
) -> None:
    """Fill required values only when source evidence supports the inference."""
    if schema is None or any(
        str(attribute.get("id") or "").upper() == "POWER_SUPPLY_TYPE"
        and _attribute_has_value(attribute)
        for attribute in attributes
    ):
        return
    definition = next(
        (
            row for row in schema
            if str(row.get("id") or "").upper() == "POWER_SUPPLY_TYPE"
            and is_required_attribute(row)
            and not is_read_only_attribute(row)
        ),
        None,
    )
    if definition is None:
        return

    usb_value = None
    for attribute in attributes:
        if str(attribute.get("id") or "").upper() != "WITH_USB":
            continue
        usb_value = semantic_value_key(
            "WITH_USB", attribute.get("value_name") or attribute.get("value_id")
        )
        break
    source_attributes = list(source.get("attributes") or [])
    if not usb_value:
        for source_attribute in source_attributes:
            if resolve_schema_attribute_id(source_attribute, None) != "WITH_USB":
                continue
            usb_value = semantic_value_key(
                "WITH_USB",
                source_attribute.get("value_name") or source_attribute.get("value_id"),
            )
            break
    title = str(source.get("title") or "")
    usb_powered = usb_value == "BOOLEAN_TRUE" or (
        not usb_value and bool(re.search(r"\bUSB\b", title, re.IGNORECASE))
    )
    if not usb_powered:
        return

    battery_hint = bool(re.search(r"\b(?:battery|bater[ií]a)\b", title, re.IGNORECASE))
    if not battery_hint:
        source_evidence = " ".join(
            str(attribute.get(key) or "")
            for attribute in source_attributes
            for key in ("id", "name", "value_name")
        )
        normalized_evidence = unicodedata.normalize("NFKD", source_evidence)
        normalized_evidence = "".join(
            character for character in normalized_evidence
            if not unicodedata.combining(character)
        )
        battery_hint = bool(re.search(
            r"(?:BATTER|BATERIA|RECHARG|RECARG|RECARREG)",
            normalized_evidence,
            re.IGNORECASE,
        ))
    desired_name = (
        "Battery/Domestic current" if battery_hint else "Domestic current"
    )
    allowed_values = [
        value for value in definition.get("values") or []
        if isinstance(value, Mapping) and value.get("id")
    ]
    matched = match_enumerated_value(
        "POWER_SUPPLY_TYPE", None, desired_name, allowed_values
    )
    inferred = {"id": "POWER_SUPPLY_TYPE", "value_name": desired_name}
    if matched is not None:
        inferred["value_id"] = str(matched["id"])
        inferred["value_name"] = str(matched.get("name") or desired_name)
    attributes.append(inferred)


def _normalize_enumerated_attributes(
    attributes: list[dict[str, Any]],
    schema: Iterable[Mapping[str, Any]] | None,
) -> None:
    """Map localized source values to IDs accepted by the CBT category."""
    if schema is None:
        return
    definitions = {
        str(definition.get("id") or "").upper(): definition
        for definition in schema
        if definition.get("id")
    }
    discarded: list[dict[str, Any]] = []
    for attribute in list(attributes):
        attribute_id = str(attribute.get("id") or "").upper()
        definition = definitions.get(attribute_id) or {}
        allowed_values = [
            value
            for value in definition.get("values") or []
            if isinstance(value, Mapping) and value.get("id")
        ]
        if not allowed_values:
            continue
        if isinstance(attribute.get("values"), list) and attribute.get("values"):
            mapped_values = []
            for source_value in attribute["values"]:
                if not isinstance(source_value, Mapping):
                    continue
                target_value = match_enumerated_value(
                    attribute_id,
                    source_value.get("id"),
                    source_value.get("name"),
                    allowed_values,
                )
                if target_value is None:
                    mapped_values = []
                    break
                mapped_values.append({
                    "id": str(target_value["id"]),
                    "name": str(target_value.get("name") or ""),
                })
            if mapped_values:
                attribute["values"] = mapped_values
                continue
            target_value = None
        else:
            target_value = match_enumerated_value(
                attribute_id,
                attribute.get("value_id"),
                attribute.get("value_name"),
                allowed_values,
            )
        controlled = str(definition.get("value_type") or "").lower() in {
            "boolean", "list"
        }
        if target_value is None and controlled and is_required_attribute(definition):
            choices = ", ".join(
                str(value.get("name") or value.get("id") or "")
                for value in allowed_values[:8]
            )
            raise MercadoLibreError(
                f"无法用本地规则映射目标类目必填属性 {attribute_id}: "
                f"{attribute.get('value_name') or attribute.get('value_id') or '(empty)'}"
                + (f"；目标可选值: {choices}" if choices else "")
            )
        if target_value is None and controlled:
            # Optional controlled attributes are safer to omit than to send an
            # invalid localized value or guess the nearest enum value.
            discarded.append(attribute)
            continue
        if target_value is None:
            continue
        attribute["value_id"] = str(target_value["id"])
        attribute["value_name"] = str(target_value.get("name") or "")
    for attribute in discarded:
        attributes.remove(attribute)


def _converted_usd_amount(
    client: MercadoLibreClient, amount: float, currency_id: str
) -> float:
    source_currency = currency_id.upper()
    if source_currency == "USD":
        return round(amount, 2)
    from erp.mercadolibre_profitability_cache import DatabaseProfitabilityCache

    cache = DatabaseProfitabilityCache()
    persisted = cache.get_exchange_rate(source_currency, "USD")
    if persisted:
        ratio = persisted.get("rate")
    else:
        conversion = client.request(
            "GET",
            "/currency_conversions/search",
            params={"from": source_currency, "to": "USD"},
        )
        ratio = conversion.get("ratio") if isinstance(conversion, Mapping) else None
        if ratio:
            cache.put_exchange_rate(source_currency, "USD", conversion)
    if not ratio:
        raise MercadoLibreError(f"无法取得 {currency_id} 到 USD 的汇率")
    return max(round(float(amount) * float(ratio), 2), 1.0)


def resolve_net_proceeds(
    client: MercadoLibreClient,
    source: Mapping[str, Any],
    explicit_value: float | None,
) -> float:
    """Use an explicit amount, or convert the source gross price to USD."""
    if explicit_value is not None:
        if explicit_value <= 0:
            raise ValueError("net_proceeds 必须大于 0")
        return round(explicit_value, 2)
    price = source.get("price")
    if price is None:
        raise MercadoLibreError("源商品没有价格；请使用 --net-proceeds 指定美元到手价")
    return _converted_usd_amount(client, float(price), str(source.get("currency_id") or "USD"))


def _copy_sale_terms(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    allowed = {"id", "name", "value_id", "value_name", "value_struct", "values"}
    return [
        {key: value for key, value in term.items() if key in allowed and value is not None}
        for term in source.get("sale_terms") or []
        if term.get("id")
    ]


def _resolve_sale_terms(
    source: Mapping[str, Any], description: Mapping[str, Any]
) -> list[dict[str, Any]]:
    copied = _copy_sale_terms(source)
    if copied:
        return copied
    text = _description_text(description)
    match = re.search(
        r"(?:garant[ií]a\s+del\s+vendedor|garantia\s+do\s+vendedor|seller\s+warranty)"
        r"\s*:\s*(\d+)\s*"
        r"(d[ií]as?|dias?|days?|mes(?:es)?|months?|a(?:ñ|n)os?|anos?|years?)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return [dict(term) for term in DEFAULT_SALE_TERMS]
    amount = int(match.group(1))
    raw_unit = unicodedata.normalize("NFKD", match.group(2).lower())
    unit = "".join(
        character for character in raw_unit if not unicodedata.combining(character)
    )
    if unit.startswith(("dia", "day")):
        normalized_unit = "days"
    elif unit.startswith(("ano", "year")):
        normalized_unit = "years"
    else:
        normalized_unit = "months"
    return [
        {
            "id": "WARRANTY_TYPE",
            "value_id": "2230280",
            "value_name": "Seller warranty",
        },
        {"id": "WARRANTY_TIME", "value_name": f"{amount} {normalized_unit}"},
    ]


def _copy_variations(
    source: Mapping[str, Any],
    ids_to_urls: Mapping[str, str],
    *,
    quantity: int,
    sku_prefix: str,
    allowed_ids: set[str] | None = None,
    schema: Iterable[Mapping[str, Any]] | None = None,
    source_schema: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    variations: list[dict[str, Any]] = []
    for index, variation in enumerate(source.get("variations") or [], start=1):
        copied: dict[str, Any] = {
            "attribute_combinations": _copy_attributes(
                variation.get("attribute_combinations") or [],
                allowed_ids=allowed_ids,
                schema=schema,
                source_schema=source_schema,
                ensure_brand=False,
            ),
            "available_quantity": quantity,
            "attributes": _copy_attributes(
                variation.get("attributes") or [],
                seller_sku=f"{sku_prefix}-V{index}",
                allowed_ids=allowed_ids,
                schema=schema,
                source_schema=source_schema,
                ensure_brand=False,
            ),
        }
        _normalize_enumerated_attributes(copied["attribute_combinations"], schema)
        _normalize_enumerated_attributes(copied["attributes"], schema)
        urls = [
            ids_to_urls[str(picture_id)]
            for picture_id in variation.get("picture_ids") or []
            if str(picture_id) in ids_to_urls
        ]
        if urls:
            copied["picture_ids"] = urls
        variations.append(copied)
    return variations


def build_global_payload(
    client: MercadoLibreClient,
    source: Mapping[str, Any],
    description: Mapping[str, Any],
    *,
    site_id: str = "MLM",
    quantity: int = 1,
    net_proceeds: float | None = None,
    translator: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Build the current Global Selling payload for one destination site."""
    if quantity <= 0:
        raise ValueError("quantity 必须大于 0")
    item_id = extract_item_id(str(source.get("id") or ""))
    pictures, ids_to_urls = _picture_sources(source)
    category_id, inferred_attributes = _infer_cbt_listing(
        client, source, translator=translator
    )
    source = _source_with_inferred_attributes(source, inferred_attributes)
    attribute_schema = _required_category_attribute_schema(client, category_id)
    source_attribute_schema = _source_category_attribute_schema(
        client, source, category_id, attribute_schema
    )
    allowed_attribute_ids = _category_attribute_ids(attribute_schema)
    attributes = _copy_attributes(
        source.get("attributes") or [],
        seller_sku=f"FOLLOW-{item_id}",
        allowed_ids=allowed_attribute_ids,
        schema=attribute_schema,
        source_schema=source_attribute_schema,
    )
    _ensure_item_condition(attributes, str(source.get("condition") or "new"))
    _ensure_gtin_or_empty_reason(attributes)
    variations = _copy_variations(
        source,
        ids_to_urls,
        quantity=quantity,
        sku_prefix=f"FOLLOW-{item_id}",
        allowed_ids=allowed_attribute_ids,
        schema=attribute_schema,
        source_schema=source_attribute_schema,
    )
    _ensure_required_attribute_defaults(attributes, attribute_schema, variations)
    _ensure_contextual_required_attribute_defaults(attributes, attribute_schema, source)
    _normalize_enumerated_attributes(attributes, attribute_schema)
    _validate_required_attributes(attributes, attribute_schema, variations)
    site: dict[str, Any] = {
        "site_id": site_id,
        "logistic_type": "remote",
        "title": str(source.get("title") or "").strip(),
        "net_proceeds": resolve_net_proceeds(client, source, net_proceeds),
        # Since August 2026 Mercado Libre validates pictures/variations inside
        # each sites_to_sell entry for traditional CBT publications.
        "pictures": pictures,
    }
    if variations:
        site["variations"] = variations
    payload: dict[str, Any] = {
        "sites_to_sell": [site],
        "currency_id": "USD",
        "catalog_listing": False,
        "category_id": category_id,
        "title": str(source.get("title") or "").strip(),
        "attributes": attributes,
    }
    if not variations:
        payload["available_quantity"] = quantity
    plain_text = _description_text(description)
    if plain_text:
        payload["description"] = {"plain_text": plain_text}
    sale_terms = _resolve_sale_terms(source, description)
    if sale_terms:
        payload["sale_terms"] = sale_terms
    return payload


def build_user_product_payload(
    client: MercadoLibreClient,
    source: Mapping[str, Any],
    description: Mapping[str, Any],
    *,
    site_id: str = "MLM",
    quantity: int = 1,
    net_proceeds: float | None = None,
    picture_ids: Iterable[str] | None = None,
    translator: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Build a single-variant CBT User Products payload."""
    if source.get("variations"):
        raise MercadoLibreError(
            "源商品包含变体，而目标店铺使用 User Products 模式；"
            "需要按每个变体分别创建并组成 family，当前不会自动合并以免错配库存"
        )
    if quantity <= 0:
        raise ValueError("quantity 必须大于 0")
    item_id = extract_item_id(str(source.get("id") or ""))
    category_id, inferred_attributes = _infer_cbt_listing(
        client, source, translator=translator
    )
    source = _source_with_inferred_attributes(source, inferred_attributes)
    attribute_schema = _required_category_attribute_schema(client, category_id)
    source_attribute_schema = _source_category_attribute_schema(
        client, source, category_id, attribute_schema
    )
    attributes = _copy_attributes(
        source.get("attributes") or [],
        seller_sku=f"FOLLOW-{item_id}",
        allowed_ids=_category_attribute_ids(attribute_schema),
        schema=attribute_schema,
        source_schema=source_attribute_schema,
    )
    _ensure_item_condition(attributes, str(source.get("condition") or "new"))
    _ensure_gtin_or_empty_reason(attributes)
    _ensure_required_attribute_defaults(attributes, attribute_schema)
    _ensure_contextual_required_attribute_defaults(attributes, attribute_schema, source)
    _normalize_enumerated_attributes(attributes, attribute_schema)
    _validate_required_attributes(attributes, attribute_schema)
    pictures: list[dict[str, str]]
    if picture_ids is None:
        # Dry-run placeholder: actual User Products publication uploads each
        # image first and replaces these source entries with picture IDs.
        pictures, _ = _picture_sources(source)
    else:
        pictures = [{"id": str(picture_id)} for picture_id in picture_ids]
        if not pictures:
            raise MercadoLibreError("User Products 刊登缺少已上传的图片 id")
    payload: dict[str, Any] = {
        "sites_to_sell": [{"site_id": site_id, "logistic_type": "remote"}],
        "family_name": str(source.get("title") or "").strip()[:60],
        "category_id": category_id,
        "global_net_proceeds": resolve_net_proceeds(client, source, net_proceeds),
        "available_quantity": quantity,
        "currency_id": "USD",
        "pictures": pictures,
        "attributes": attributes,
    }
    plain_text = _description_text(description)
    if plain_text:
        payload["description"] = {"plain_text": plain_text}
    sale_terms = _resolve_sale_terms(source, description)
    if sale_terms:
        payload["sale_terms"] = sale_terms
    return payload


def build_local_payload(
    source: Mapping[str, Any],
    description: Mapping[str, Any],
    *,
    quantity: int = 1,
    price: float | None = None,
) -> dict[str, Any]:
    """Build a standard local-site ``POST /items`` payload."""
    item_id = extract_item_id(str(source.get("id") or ""))
    pictures, ids_to_urls = _picture_sources(source)
    attributes = _copy_attributes(
        source.get("attributes") or [], seller_sku=f"FOLLOW-{item_id}"
    )
    payload: dict[str, Any] = {
        "site_id": source.get("site_id"),
        "title": source.get("title"),
        "category_id": source.get("category_id"),
        "price": round(float(price if price is not None else source.get("price")), 2),
        "currency_id": source.get("currency_id"),
        "buying_mode": source.get("buying_mode") or "buy_it_now",
        "listing_type_id": source.get("listing_type_id") or "gold_special",
        "condition": source.get("condition") or "new",
        "pictures": pictures,
        "attributes": attributes,
    }
    variations = _copy_variations(
        source, ids_to_urls, quantity=quantity, sku_prefix=f"FOLLOW-{item_id}"
    )
    if variations:
        payload["variations"] = variations
    else:
        payload["available_quantity"] = quantity
    plain_text = _description_text(description)
    if plain_text:
        payload["description"] = {"plain_text": plain_text}
    sale_terms = _resolve_sale_terms(source, description)
    if sale_terms:
        payload["sale_terms"] = sale_terms
    return payload


def _normalized_existing_user_product_id(value: Any) -> str:
    candidate = str(value or "").strip().upper()
    match = re.fullmatch(r"(?:CBT)?U(\d+)", candidate)
    # The mapping resource accepts either CBTU{id} or U{id}, but the
    # add-marketplace resource only accepts the siteless U{id} form.
    return f"U{match.group(1)}" if match else ""


def _conflict_user_product_id(message: str) -> str:
    """Extract a reusable siteless User Product id from a conflict response."""

    match = re.search(r"\b(?:(?:MLM|MLB|CBT))?U(\d+)\b", str(message or ""), re.I)
    return f"U{match.group(1)}" if match else ""


def _mapped_user_product_site_item(mapping: Any, site_id: str) -> Mapping[str, Any] | None:
    rows = mapping if isinstance(mapping, list) else [mapping]
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for site_item in row.get("site_items") or []:
            if (
                isinstance(site_item, Mapping)
                and str(site_item.get("site_id") or "").upper() == site_id
            ):
                return site_item
    return None


def _site_item_error(result: Any, site_id: str) -> Mapping[str, Any] | None:
    """Return a target-site error embedded in an otherwise HTTP-2xx result.

    Global Selling APIs may respond with HTTP 200 while putting the actual
    marketplace rejection inside ``site_items[].error``.  Treating that
    response as a successful publication creates false successes and causes
    the next site/account pass to create duplicate products.
    """

    if not isinstance(result, Mapping):
        return None
    target = str(site_id or "").strip().upper()
    rows = result.get("site_items")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_site = str(row.get("site_id") or "").strip().upper()
        if row_site == target and isinstance(row.get("error"), Mapping):
            return row["error"]
    return None


def follow_sell(
    client: MercadoLibreClient,
    source_url: str,
    *,
    quantity: int = 1,
    net_proceeds: float | None = None,
    local_price: float | None = None,
    destination_site_id: str = "MLM",
    translator: Callable[..., Any] | None = None,
    source_from_database: bool = False,
    prepared_listing: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None,
    existing_user_product_id: str | None = None,
    publish: bool = False,
) -> dict[str, Any]:
    """Build, and optionally publish, a copied listing."""
    total_started = time.perf_counter()
    timings: dict[str, float] = {}
    destination_site_id = normalize_marketplace_site(destination_site_id)
    stage_started = time.perf_counter()
    user = _cached_user_profile(client)
    timings["account"] = time.perf_counter() - stage_started
    stage_started = time.perf_counter()
    if prepared_listing is not None:
        source = dict(prepared_listing[0])
        description = dict(prepared_listing[1])
    elif source_from_database:
        from erp.mercadolibre_source_store import load_listing_for_publish

        source, description = load_listing_for_publish(source_url)
    else:
        try:
            source, description = fetch_source_listing(client, source_url)
        except MercadoLibreError as api_error:
            # Public marketplace item reads may be forbidden for a Global
            # Selling token.  Reuse the browser snapshot when it is available.
            try:
                from erp.mercadolibre_source_store import load_listing_for_publish

                source, description = load_listing_for_publish(source_url)
            except Exception:
                raise api_error
    timings["source"] = time.perf_counter() - stage_started
    target_site = str(user.get("site_id") or "")
    is_global = target_site == "CBT"
    is_user_product = is_global and "user_product_seller" in set(user.get("tags") or [])
    reusable_user_product_id = (
        _normalized_existing_user_product_id(existing_user_product_id)
        if is_user_product
        else ""
    )
    translation = {
        "source_site_id": str(source.get("site_id") or source.get("id") or "")[:3],
        "destination_site_id": destination_site_id,
        "source_language": "",
        "target_language": "",
        "translated": False,
        "translated_field_count": 0,
        "strategy": "deterministic_attribute_rules",
    }
    picture_upload_errors: list[str] = []
    stage_started = time.perf_counter()
    if is_global:
        # Publication deliberately avoids translation/model calls. Stable
        # attribute IDs and target-schema enums are localized by deterministic
        # in-memory rules in the payload builders.
        source["_category_prediction_title"] = str(source.get("title") or "")
    elif target_site != destination_site_id:
        raise MercadoLibreError(
            f"目标店铺只能在 {target_site or '(unknown)'} 站点上架，"
            f"不能选择 {destination_site_id}"
        )
    timings["translation"] = time.perf_counter() - stage_started
    stage_started = time.perf_counter()
    publication_action = "create"
    if is_user_product and reusable_user_product_id:
        payload = {
            "sites_to_sell": [{
                "site_id": destination_site_id,
                "logistic_type": "remote",
                "net_proceeds": resolve_net_proceeds(client, source, net_proceeds),
            }]
        }
        timings["payload"] = time.perf_counter() - stage_started
        timings["pictures"] = 0.0
        endpoint = f"/global/user-products/{reusable_user_product_id}"
        publication_action = "add_marketplace"
    elif is_user_product:
        if source.get("variations"):
            raise MercadoLibreError(
                "源商品包含变体，而目标店铺使用 User Products 模式；"
                "需要按每个变体分别创建并组成 family，当前不会自动合并以免错配库存"
            )
        payload = build_user_product_payload(
            client,
            source,
            description,
            site_id=destination_site_id,
            quantity=quantity,
            net_proceeds=net_proceeds,
            picture_ids=None,
        )
        timings["payload"] = time.perf_counter() - stage_started
        timings["pictures"] = 0.0
        if publish:
            image_started = time.perf_counter()
            source_pictures, _ = _picture_sources(source)
            picture_ids = []
            for picture in source_pictures:
                try:
                    picture_id = _upload_validated_picture(
                        client, picture["source"]
                    )
                    picture_ids.append(picture_id)
                except MercadoLibreError as exc:
                    picture_upload_errors.append(str(exc))
            if not picture_ids:
                details = "; ".join(picture_upload_errors[:3])
                raise MercadoLibreError(
                    "源商品没有符合要求且上传成功的图片"
                    + (f"：{details}" if details else "")
                )
            payload["pictures"] = [{"id": picture_id} for picture_id in picture_ids]
            timings["pictures"] = time.perf_counter() - image_started
        endpoint = str(
            os.environ.get("MERCADO_USER_PRODUCTS_CREATE_ENDPOINT")
            or "/global/user-products"
        ).strip()
    elif is_global:
        payload = build_global_payload(
            client,
            source,
            description,
            site_id=destination_site_id,
            quantity=quantity,
            net_proceeds=net_proceeds,
        )
        timings["payload"] = time.perf_counter() - stage_started
        timings["pictures"] = 0.0
        endpoint = "/global/items"
    else:
        if target_site and target_site != source.get("site_id"):
            raise MercadoLibreError(
                f"目标店铺站点 {target_site} 与源商品站点 {source.get('site_id')} 不一致"
            )
        payload = build_local_payload(
            source, description, quantity=quantity, price=local_price
        )
        timings["payload"] = time.perf_counter() - stage_started
        timings["pictures"] = 0.0
        endpoint = "/items"
    stage_started = time.perf_counter()
    result = None
    if publish:
        try:
            if reusable_user_product_id:
                mapping = _try_request(
                    client,
                    "GET",
                    f"/marketplace/user-products/{reusable_user_product_id}/mapping",
                )
                existing_site_item = _mapped_user_product_site_item(
                    mapping, destination_site_id
                )
                if existing_site_item is not None:
                    publication_action = "already_available"
                    result = {
                        "parent_user_product_id": reusable_user_product_id,
                        "siteless_user_product_id": reusable_user_product_id,
                        "site_items": [dict(existing_site_item)],
                        "already_available": True,
                    }
                else:
                    result = client.request("POST", endpoint, json_body=payload)
            else:
                result = client.request("POST", endpoint, json_body=payload)
        except MercadoLibreError as exc:
            if (
                is_user_product
                and endpoint == "/global/user-products"
                and exc.status_code in {404, 405}
            ):
                # Explicit not-found/method-not-allowed responses are safe to
                # fall back because Mercado confirms no product was created.
                endpoint = "/global/items"
                result = client.request("POST", endpoint, json_body=payload)
            elif (
                is_user_product
                and endpoint == "/global/user-products"
                and exc.status_code == 400
                and (
                    "user_product.repeated.conflict" in str(exc)
                    or "user product already exists" in str(exc).lower()
                )
            ):
                # A prior request may have created the siteless product before
                # the worker lost its response.  Reconcile that resource and
                # add/check the target site instead of creating a duplicate.
                conflict_id = _conflict_user_product_id(str(exc))
                if not conflict_id:
                    raise
                mapping = _try_request(
                    client,
                    "GET",
                    f"/marketplace/user-products/{conflict_id}/mapping",
                )
                existing_site_item = _mapped_user_product_site_item(
                    mapping, destination_site_id
                )
                if existing_site_item is not None:
                    publication_action = "already_available"
                    result = {
                        "parent_user_product_id": f"CBT{conflict_id}",
                        "siteless_user_product_id": conflict_id,
                        "site_items": [dict(existing_site_item)],
                        "already_available": True,
                        "reconciled_conflict": True,
                    }
                else:
                    endpoint = f"/global/user-products/{conflict_id}"
                    publication_action = "add_marketplace"
                    result = client.request(
                        "POST",
                        endpoint,
                        json_body={
                            "sites_to_sell": [{
                                "site_id": destination_site_id,
                                "logistic_type": "remote",
                                "net_proceeds": resolve_net_proceeds(
                                    client, source, net_proceeds
                                ),
                            }]
                        },
                    )
            else:
                raise
        embedded_error = _site_item_error(result, destination_site_id)
        if embedded_error is not None:
            status = embedded_error.get("status")
            try:
                status_code = int(status) if status not in (None, "") else None
            except (TypeError, ValueError):
                status_code = None
            raise MercadoLibreError(
                f"目标站点 {destination_site_id} 刊登失败："
                f"{json.dumps(dict(embedded_error), ensure_ascii=False)}",
                status_code=status_code,
            )
    timings["publish"] = time.perf_counter() - stage_started
    database_publish_recorded = False
    database_publish_error = None
    stage_started = time.perf_counter()
    if publish and result and source_from_database:
        try:
            from erp.mercadolibre_source_store import record_publish_result

            record_publish_result(
                source_url,
                result,
                target_user_id=user.get("id"),
            )
            database_publish_recorded = True
        except Exception as exc:
            # The remote listing already exists at this point.  Surface the
            # local checkpoint failure without misreporting publication as failed.
            database_publish_error = f"{type(exc).__name__}: {exc}"
    timings["checkpoint"] = time.perf_counter() - stage_started
    timings["total"] = time.perf_counter() - total_started
    return {
        "mode": "published" if publish else "dry_run",
        "target_user_id": user.get("id"),
        "destination_site_id": destination_site_id,
        "source_item_id": source.get("id"),
        "endpoint": endpoint,
        "publication_action": publication_action,
        "listing_model": (
            "user_products" if is_user_product else "global" if is_global else "local"
        ),
        "picture_upload_required": bool(is_user_product and not publish),
        "payload": payload,
        "result": result,
        "database_publish_recorded": database_publish_recorded,
        "database_publish_error": database_publish_error,
        "picture_upload_errors": picture_upload_errors,
        "translation": translation,
        "timings": {
            key: round(value, 4) for key, value in timings.items()
        },
    }


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise MercadoLibreError(f"缺少环境变量 {name}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="跟卖 Mercado Libre 商品")
    parser.add_argument("source", nargs="?", help="源商品链接或商品编号")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    parser.add_argument("--exchange-code", help="TG code 或完整授权回调链接")
    parser.add_argument("--quantity", type=int, default=1, help="库存，默认 1")
    parser.add_argument(
        "--site-id",
        default="MLM",
        choices=("MLM", "MLB", "MLA", "MLC", "MCO", "MLU"),
        help="目标站点，默认 MLM（墨西哥）",
    )
    parser.add_argument("--net-proceeds", type=float, help="Global Selling 美元到手价")
    parser.add_argument("--price", type=float, help="本地店铺售价")
    parser.add_argument(
        "--source-from-db",
        action="store_true",
        help="只使用已保存的网页/智赢插件快照，不调用源商品 API",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="实际创建商品；不加时只生成并输出 payload",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        client_id = _required_env("MELI_CLIENT_ID")
        client_secret = _required_env("MELI_CLIENT_SECRET")
        if args.exchange_code:
            data = exchange_authorization_code(
                args.exchange_code,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=args.redirect_uri,
                token_file=args.token_file,
            )
            print(f"授权成功，店铺用户 ID: {data.get('user_id', 'unknown')}")
            return 0
        if not args.source:
            raise MercadoLibreError("请提供源商品链接，或使用 --exchange-code")
        client = MercadoLibreClient(
            args.token_file, client_id=client_id, client_secret=client_secret
        )
        output = follow_sell(
            client,
            args.source,
            quantity=args.quantity,
            net_proceeds=args.net_proceeds,
            local_price=args.price,
            destination_site_id=args.site_id,
            source_from_database=args.source_from_db,
            publish=args.publish,
        )
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (MercadoLibreError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
