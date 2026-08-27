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
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlparse

import requests

from erp.mercadolibre_translation import (
    BatchTranslator,
    normalize_marketplace_site,
    translate_listing_content,
)


API_BASE_URL = "https://api.mercadolibre.com"
DEFAULT_REDIRECT_URI = "https://zeshun.nat100.top/zs"
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
        source_response = self.session.get(source_url, timeout=self.timeout)
        if not source_response.ok:
            raise MercadoLibreError(
                f"下载源图片失败 (HTTP {source_response.status_code}): {source_url}"
            )
        if len(source_response.content) > 10 * 1024 * 1024:
            raise MercadoLibreError(f"源图片超过 10 MB: {source_url}")
        content_type = source_response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
        image_bytes = source_response.content
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
GENDER_VALUE_IDS = {
    "WOMAN": "339665",
    "WOMEN": "339665",
    "MUJER": "339665",
    "MUJERES": "339665",
    "FEMENINO": "339665",
    "FEMININO": "339665",
    "MAN": "339666",
    "MEN": "339666",
    "HOMBRE": "339666",
    "HOMBRES": "339666",
    "MASCULINO": "339666",
    "GIRL": "339668",
    "GIRLS": "339668",
    "NINA": "339668",
    "NINAS": "339668",
    "MENINA": "339668",
    "MENINAS": "339668",
    "BOY": "339667",
    "BOYS": "339667",
    "NINO": "339667",
    "NINOS": "339667",
    "MENINO": "339667",
    "MENINOS": "339667",
    "BABY": "371795",
    "BABIES": "371795",
    "BEBE": "371795",
    "BEBES": "371795",
    "GENDER_NEUTRAL": "110461",
    "SIN_GENERO": "110461",
    "SEM_GENERO": "110461",
    "UNISEX": "110461",
}
ATTRIBUTE_ID_ALIASES = {
    "MARCA": "BRAND",
    "MODELO": "MODEL",
    "GENERO": "GENDER",
    "PERSONAJE": "CHARACTER",
    "NOMBRE_DEL_JUEGO_DE_MESA": "BOARD_GAME_NAME",
    "TIPO_DE_PRODUCTO": "PRODUCT_TYPE",
    "TIPO_DE_CARTAS": "PLAYING_CARDS_TYPE",
    "ES_SET": "IS_SET",
    "TIPO_DE_CAMARA_DE_VIGILANCIA": "SURVEILLANCE_CAMERA_TYPE",
    "LOCACIONES_DE_LA_CAMARA": "CAMERA_LOCATIONS",
    "ES_INALAMBRICO": "IS_WIRELESS",
    "TALLA": "SIZE",
    "MATERIAL_PRINCIPAL": "MAIN_MATERIAL",
    "COMPOSICION": "COMPOSITION",
    "CANTIDAD_DE_DISFRACES": "COSTUMES_NUMBER",
    "INCLUYE_ACCESORIOS": "INCLUDES_ACCESSORIES",
    "ACCESORIOS_INCLUIDOS": "ACCESSORIES_INCLUDED",
    "ES_KIT": "IS_KIT",
    "TALLA_DEL_DISFRAZ": "COSTUME_SIZE",
    "CONTORNO_DEL_PECHO": "CHEST_CIRCUMFERENCE",
    "CONTORNO_DE_LA_CINTURA": "WAIST_CIRCUMFERENCE",
    "CONTORNO_DE_LA_CADERA": "HIP_CIRCUMFERENCE",
    "ESTILOS": "NECKLACE_STYLES",
    "MATERIAL_DEL_COLLAR": "NECKLACE_MATERIAL",
}
MAX_PICTURES_PER_LISTING = 12


def _clean_attribute(attribute: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only fields accepted by create/update endpoints."""
    return {
        key: value
        for key, value in attribute.items()
        if key in READ_ONLY_ATTRIBUTE_KEYS and value is not None
    }


def _normalized_attribute_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(
        character for character in text if not unicodedata.combining(character)
    )
    return re.sub(r"[^A-Z0-9]+", "_", ascii_text.upper()).strip("_")


def _canonical_attribute_id(attribute: Mapping[str, Any]) -> str:
    raw_id = _normalized_attribute_key(attribute.get("id"))
    name_id = _normalized_attribute_key(attribute.get("name"))
    return ATTRIBUTE_ID_ALIASES.get(raw_id) or ATTRIBUTE_ID_ALIASES.get(name_id) or raw_id


def _copy_attributes(
    attributes: Iterable[Mapping[str, Any]],
    *,
    seller_sku: str | None = None,
    allowed_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for source_attribute in attributes:
        attribute_id = _canonical_attribute_id(source_attribute)
        if (
            not attribute_id
            or attribute_id in SELLER_SPECIFIC_ATTRIBUTES
            or (allowed_ids is not None and attribute_id not in allowed_ids)
        ):
            continue
        attribute = _clean_attribute(source_attribute)
        attribute["id"] = attribute_id
        existing_position = positions.get(attribute_id)
        if existing_position is None:
            positions[attribute_id] = len(copied)
            copied.append(attribute)
        elif not copied[existing_position].get("value_name") and attribute.get("value_name"):
            copied[existing_position] = attribute
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
    ids = {str(attribute.get("id") or "").upper() for attribute in attributes}
    if "GTIN" in ids or "EMPTY_GTIN_REASON" in ids:
        return
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
_DIRECT_CBT_CATEGORY_CACHE: dict[str, float] = {}
_CATEGORY_KEY_LOCKS: dict[str, threading.Lock] = {}
_PICTURE_CACHE_LOCK = threading.Lock()
_PICTURE_ID_CACHE: dict[tuple[int, str], tuple[float, str]] = {}
_PICTURE_KEY_LOCKS: dict[tuple[int, str], threading.Lock] = {}
_CACHE_TTL_SECONDS = 24 * 60 * 60
_PICTURE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


def _cached_user_profile(client: MercadoLibreClient) -> Mapping[str, Any]:
    instance_profile = getattr(client, "_cached_user_profile_value", None)
    if isinstance(instance_profile, Mapping):
        return instance_profile
    token_id = int(getattr(client, "token_id", 0) or 0)
    if token_id <= 0:
        profile = client.request("GET", "/users/me")
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


def _direct_cbt_category_exists(
    client: MercadoLibreClient, candidate: str
) -> bool:
    token_id = int(getattr(client, "token_id", 0) or 0)
    if token_id <= 0:
        return bool(_try_request(client, "GET", f"/categories/{candidate}"))
    now = time.monotonic()
    with _CATEGORY_CACHE_LOCK:
        cached_at = _DIRECT_CBT_CATEGORY_CACHE.get(candidate)
        if cached_at is not None and now - cached_at < _CACHE_TTL_SECONDS:
            return True
        key_lock = _CATEGORY_KEY_LOCKS.setdefault(f"category:{candidate}", threading.Lock())
    with key_lock:
        with _CATEGORY_CACHE_LOCK:
            cached_at = _DIRECT_CBT_CATEGORY_CACHE.get(candidate)
            if cached_at is not None and time.monotonic() - cached_at < _CACHE_TTL_SECONDS:
                return True
        exists = bool(_try_request(client, "GET", f"/categories/{candidate}"))
        if exists:
            with _CATEGORY_CACHE_LOCK:
                _DIRECT_CBT_CATEGORY_CACHE[candidate] = time.monotonic()
        return exists


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


def infer_cbt_category(client: MercadoLibreClient, source: Mapping[str, Any]) -> str:
    """Map a marketplace category to its CBT counterpart and validate it."""
    category_id = str(source.get("category_id") or "")
    if category_id.startswith("CBT"):
        return category_id
    numeric = re.sub(r"^[A-Z]+", "", category_id)
    candidate = f"CBT{numeric}" if numeric else ""
    if candidate and _direct_cbt_category_exists(client, candidate):
        return candidate
    suggestions = client.request(
        "GET",
        "/sites/CBT/domain_discovery/search",
        params={
            "q": source.get("_category_prediction_title") or source.get("title") or ""
        },
    )
    if isinstance(suggestions, list):
        for suggestion in suggestions:
            suggested = str(suggestion.get("category_id") or "")
            if suggested.startswith("CBT"):
                return suggested
    raise MercadoLibreError(f"无法把源类目 {category_id or '(empty)'} 映射为 CBT 类目")


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
    required = {
        str(attribute.get("id") or "").upper()
        for attribute in schema
        if attribute.get("id")
        and (
            bool((attribute.get("tags") or {}).get("required"))
            or bool((attribute.get("tags") or {}).get("catalog_required"))
        )
        and not bool((attribute.get("tags") or {}).get("read_only"))
    }
    missing = sorted(required - present)
    if missing:
        raise MercadoLibreError(
            "源商品缺少目标类目必填属性: "
            + ", ".join(missing)
            + "；请补全采集数据后再上架"
        )


def _ensure_required_attribute_defaults(
    attributes: list[dict[str, Any]],
    schema: Iterable[Mapping[str, Any]] | None,
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
    for definition in schema:
        attribute_id = str(definition.get("id") or "").upper()
        tags = definition.get("tags") or {}
        if (
            not attribute_id
            or attribute_id in present
            or bool(tags.get("read_only"))
            or not (bool(tags.get("required")) or bool(tags.get("catalog_required")))
        ):
            continue
        default = DEFAULT_REQUIRED_ATTRIBUTES.get(attribute_id)
        if default:
            attributes.append(dict(default))
            present.add(attribute_id)


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
    for attribute in attributes:
        if str(attribute.get("id") or "").upper() != "GENDER":
            continue
        definition = definitions.get("GENDER") or {}
        allowed_values = {
            str(value.get("id") or ""): value
            for value in definition.get("values") or []
            if isinstance(value, Mapping) and value.get("id")
        }
        if not allowed_values:
            continue
        existing_value_id = str(attribute.get("value_id") or "")
        target_value_id = (
            existing_value_id
            if existing_value_id in allowed_values
            else GENDER_VALUE_IDS.get(
                _normalized_attribute_key(attribute.get("value_name"))
            )
        )
        target_value = allowed_values.get(str(target_value_id or ""))
        if not target_value:
            raise MercadoLibreError(
                "无法把源商品 GENDER 值映射到目标类目: "
                f"{attribute.get('value_name') or attribute.get('value_id') or '(empty)'}"
            )
        attribute["value_id"] = str(target_value["id"])
        attribute["value_name"] = str(target_value.get("name") or "")


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
) -> list[dict[str, Any]]:
    variations: list[dict[str, Any]] = []
    for index, variation in enumerate(source.get("variations") or [], start=1):
        copied: dict[str, Any] = {
            "attribute_combinations": [
                _clean_attribute(attribute)
                for attribute in variation.get("attribute_combinations") or []
            ],
            "available_quantity": quantity,
            "attributes": _copy_attributes(
                variation.get("attributes") or [],
                seller_sku=f"{sku_prefix}-V{index}",
            ),
        }
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
) -> dict[str, Any]:
    """Build the current Global Selling payload for one destination site."""
    if quantity <= 0:
        raise ValueError("quantity 必须大于 0")
    item_id = extract_item_id(str(source.get("id") or ""))
    pictures, ids_to_urls = _picture_sources(source)
    category_id = infer_cbt_category(client, source)
    attribute_schema = _category_attribute_schema(client, category_id)
    attributes = _copy_attributes(
        source.get("attributes") or [],
        seller_sku=f"FOLLOW-{item_id}",
        allowed_ids=_category_attribute_ids(attribute_schema),
    )
    _ensure_item_condition(attributes, str(source.get("condition") or "new"))
    _ensure_gtin_or_empty_reason(attributes)
    _normalize_enumerated_attributes(attributes, attribute_schema)
    _ensure_required_attribute_defaults(attributes, attribute_schema)
    _validate_required_attributes(attributes, attribute_schema)
    site: dict[str, Any] = {
        "site_id": site_id,
        "logistic_type": "remote",
        "title": str(source.get("title") or "").strip(),
        "net_proceeds": resolve_net_proceeds(client, source, net_proceeds),
        # Since August 2026 Mercado Libre validates pictures/variations inside
        # each sites_to_sell entry for traditional CBT publications.
        "pictures": pictures,
    }
    variations = _copy_variations(
        source, ids_to_urls, quantity=quantity, sku_prefix=f"FOLLOW-{item_id}"
    )
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
    category_id = infer_cbt_category(client, source)
    attribute_schema = _category_attribute_schema(client, category_id)
    attributes = _copy_attributes(
        source.get("attributes") or [],
        seller_sku=f"FOLLOW-{item_id}",
        allowed_ids=_category_attribute_ids(attribute_schema),
    )
    _ensure_item_condition(attributes, str(source.get("condition") or "new"))
    _ensure_gtin_or_empty_reason(attributes)
    _normalize_enumerated_attributes(attributes, attribute_schema)
    _ensure_required_attribute_defaults(attributes, attribute_schema)
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


def follow_sell(
    client: MercadoLibreClient,
    source_url: str,
    *,
    quantity: int = 1,
    net_proceeds: float | None = None,
    local_price: float | None = None,
    destination_site_id: str = "MLM",
    translator: BatchTranslator | None = None,
    source_from_database: bool = False,
    prepared_listing: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None,
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
    translation = {
        "source_site_id": str(source.get("site_id") or source.get("id") or "")[:3],
        "destination_site_id": destination_site_id,
        "source_language": "",
        "target_language": "",
        "translated": False,
        "translated_field_count": 0,
    }
    picture_upload_errors: list[str] = []
    stage_started = time.perf_counter()
    if is_global:
        category_prediction_title = str(source.get("title") or "")
        source, description, translation = translate_listing_content(
            source,
            description,
            destination_site_id=destination_site_id,
            translator=translator,
        )
        source["_category_prediction_title"] = category_prediction_title
    elif target_site != destination_site_id:
        raise MercadoLibreError(
            f"目标店铺只能在 {target_site or '(unknown)'} 站点上架，"
            f"不能选择 {destination_site_id}"
        )
    timings["translation"] = time.perf_counter() - stage_started
    stage_started = time.perf_counter()
    if is_user_product:
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
            else:
                raise
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
