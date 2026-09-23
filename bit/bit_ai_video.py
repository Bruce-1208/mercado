"""AI product-video jobs optimized for Mercado Libre Clips.

New jobs use Volcengine Seedance 2.5 first and fail over to Alibaba Cloud Wan
3.0 when the primary provider rejects or terminally fails a task. Generation
state and finished media stay on the server so temporary provider URLs never
leak into the product workflow and existing Wan jobs can safely resume.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from werkzeug.datastructures import FileStorage


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
AI_VIDEO_EXTENSIONS = {".mp4", ".mov"}
VIDEO_EXTENSIONS = AI_VIDEO_EXTENSIONS | {".avi", ".mpeg", ".mpg", ".mkv", ".webm"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_OUTPUT_BYTES = 280 * 1024 * 1024
MAX_IMAGES = 10
MAX_VIDEOS = 5
MAX_ASSETS = 20
ACTIVE_STATUSES = {"queued", "submitting", "generating"}
TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}
SITE_LANGUAGES = {"MLB": "巴西葡萄牙语", "MLM": "拉美西班牙语"}
SEEDANCE_PROVIDER = "volcengine-seedance"
WAN_PROVIDER = "dashscope-wan3"
LOCAL_PROVIDER = "local-ffmpeg"
PROVIDER_ORDER = (SEEDANCE_PROVIDER, WAN_PROVIDER)
LOCAL_FIT_MODES = {"crop", "pad"}
LOCAL_FIT_MODE_LABELS = {
    "crop": "居中裁剪铺满 9:16",
    "pad": "完整保留画面并补黑边",
}
LOCAL_OUTPUT_WIDTH = 720
LOCAL_OUTPUT_HEIGHT = 1280
SEEDANCE_MODEL = "doubao-seedance-2-5-260628"
WAN_MODELS = {"wan3.0-video-prime", "wan3.0-video"}
WAN_REGIONS = {
    "cn-beijing", "ap-southeast-1", "ap-northeast-1", "eu-central-1",
    "us-east-1", "cn-hongkong",
}

_manifest_lock = threading.RLock()
_worker_lock = threading.Lock()
_active_workers: set[str] = set()
_settings_lock = threading.RLock()
_account_credential_resolver = None


def set_account_credential_resolver(resolver) -> None:
    """Inject account credential lookup without coupling this module to Flask."""
    global _account_credential_resolver
    _account_credential_resolver = resolver


def _account_credentials(user_id) -> dict:
    if not user_id or not callable(_account_credential_resolver):
        return {}
    value = _account_credential_resolver(int(user_id))
    return value if isinstance(value, dict) else {}


def _job_owner(job: dict | None) -> int:
    try:
        return int((job or {}).get("credential_owner_id") or 0)
    except (TypeError, ValueError):
        return 0


class _ProviderRejectedError(RuntimeError):
    """The provider definitively rejected a request before returning a task."""


class _ProviderTerminalError(RuntimeError):
    """The provider task reached a failed/cancelled terminal state."""


class _ProviderStateUncertainError(RuntimeError):
    """The remote task may still exist; submitting a fallback could double bill."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_storage_root() -> Path:
    configured = str(os.environ.get("AI_VIDEO_STORAGE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(
            os.environ.get("XDG_DATA_HOME")
            or os.environ.get("XDG_STATE_HOME")
            or (Path.home() / ".local" / "share")
        )
    return (base / "Zeshun" / "MercadoWorkbench" / "ai-videos").resolve()


def storage_root() -> Path:
    root = _default_storage_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _settings_path() -> Path:
    return storage_root() / "settings.json"


def _load_saved_settings() -> dict:
    path = _settings_path()
    if not path.is_file():
        return {}
    with _settings_lock:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return value if isinstance(value, dict) else {}


def _common_config() -> dict:
    saved = _load_saved_settings()
    return {
        "public_base_url": str(
            saved.get("public_base_url")
            or os.environ.get("AI_VIDEO_PUBLIC_BASE_URL")
            or os.environ.get("BIT_PUBLIC_WORKBENCH_URL")
            or ""
        ).strip().rstrip("/"),
    }


def _provider_config(provider: str, user_id=None) -> dict:
    saved = _load_saved_settings()
    account = _account_credentials(user_id)
    if provider == SEEDANCE_PROVIDER:
        return {
            "api_key": str(
                account.get("seedance_api_key")
                or saved.get("seedance_api_key")
                or os.environ.get("AI_VIDEO_SEEDANCE_API_KEY")
                or os.environ.get("ARK_API_KEY")
                or ""
            ).strip(),
            "region": str(
                saved.get("seedance_region")
                or os.environ.get("AI_VIDEO_SEEDANCE_REGION")
                or "cn-beijing"
            ).strip(),
            "model": str(
                saved.get("seedance_model")
                or os.environ.get("AI_VIDEO_SEEDANCE_MODEL")
                or SEEDANCE_MODEL
            ).strip(),
            "endpoint": str(
                saved.get("seedance_endpoint")
                or os.environ.get("AI_VIDEO_SEEDANCE_ENDPOINT")
                or ""
            ).strip().rstrip("/"),
        }
    if provider == WAN_PROVIDER:
        return {
            "api_key": str(
                account.get("wan_api_key")
                or saved.get("wan_api_key")
                or saved.get("api_key")
                or os.environ.get("AI_VIDEO_WAN_API_KEY")
                or os.environ.get("AI_VIDEO_API_KEY")
                or os.environ.get("DASHSCOPE_API_KEY")
                or ""
            ).strip(),
            "workspace_id": str(
                account.get("wan_workspace_id")
                or saved.get("wan_workspace_id")
                or saved.get("workspace_id")
                or os.environ.get("AI_VIDEO_WAN_WORKSPACE_ID")
                or os.environ.get("AI_VIDEO_WORKSPACE_ID")
                or ""
            ).strip(),
            "region": str(
                saved.get("wan_region")
                or saved.get("region")
                or os.environ.get("AI_VIDEO_WAN_REGION")
                or os.environ.get("AI_VIDEO_REGION")
                or "cn-beijing"
            ).strip(),
            "model": str(
                saved.get("wan_model")
                or saved.get("model")
                or os.environ.get("AI_VIDEO_WAN_MODEL")
                or os.environ.get("AI_VIDEO_MODEL")
                or "wan3.0-video-prime"
            ).strip(),
            "endpoint": str(
                saved.get("wan_endpoint")
                or saved.get("endpoint")
                or os.environ.get("AI_VIDEO_WAN_ENDPOINT")
                or os.environ.get("AI_VIDEO_ENDPOINT")
                or ""
            ).strip().rstrip("/"),
        }
    raise ValueError("不支持的视频生成供应商")


def _mask_key(key: str) -> str:
    if len(key) >= 10:
        return f"{key[:4]}{'*' * 8}{key[-4:]}"
    return "已配置" if key else ""


def _provider_is_configured(provider: str, user_id=None) -> bool:
    config = _provider_config(provider, user_id)
    if provider == SEEDANCE_PROVIDER:
        return bool(config["api_key"])
    return bool(config["api_key"] and config["workspace_id"])


def _local_transcode_tools() -> tuple[str, str] | None:
    ffmpeg_name = str(os.environ.get("AI_VIDEO_FFMPEG_BIN") or "ffmpeg").strip()
    ffprobe_name = str(os.environ.get("AI_VIDEO_FFPROBE_BIN") or "ffprobe").strip()
    ffmpeg = shutil.which(ffmpeg_name)
    ffprobe = shutil.which(ffprobe_name)
    if ffmpeg:
        return ffmpeg, ffprobe or ""
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        return None
    return (str(bundled), "") if bundled else None


def save_provider_settings(changes: dict) -> dict:
    current = _load_saved_settings()
    key_aliases = {
        "seedance_api_key": "seedance_api_key",
        "wan_api_key": "wan_api_key",
        # Old clients configured Wan through the generic field.
        "api_key": "wan_api_key",
    }
    for incoming, stored in key_aliases.items():
        if incoming not in changes:
            continue
        api_key = str(changes.get(incoming) or "").strip()
        if api_key and "*" not in api_key:
            current[stored] = api_key
    fields = {
        "seedance_region": 40,
        "seedance_model": 100,
        "seedance_endpoint": 500,
        "wan_workspace_id": 100,
        "wan_region": 40,
        "wan_model": 100,
        "wan_endpoint": 500,
        "public_base_url": 500,
    }
    legacy_aliases = {
        "workspace_id": "wan_workspace_id",
        "region": "wan_region",
        "model": "wan_model",
        "endpoint": "wan_endpoint",
    }
    for incoming, stored in legacy_aliases.items():
        if incoming in changes and stored not in changes:
            current[stored] = str(changes.get(incoming) or "").strip()[:fields[stored]]
    for key, maximum in fields.items():
        if key in changes:
            current[key] = str(changes.get(key) or "").strip()[:maximum]
    current.setdefault("seedance_region", "cn-beijing")
    current.setdefault("seedance_model", SEEDANCE_MODEL)
    current.setdefault("wan_region", str(current.get("region") or "cn-beijing"))
    current.setdefault("wan_model", str(current.get("model") or "wan3.0-video-prime"))
    if current["seedance_region"] != "cn-beijing":
        raise ValueError("Seedance 2.5 当前仅支持北京地域")
    if current["seedance_model"] != SEEDANCE_MODEL:
        raise ValueError("Seedance 主模型必须使用 doubao-seedance-2-5-260628")
    if current["wan_region"] not in WAN_REGIONS:
        raise ValueError("不支持的万相模型地域")
    if current["wan_model"] not in WAN_MODELS:
        raise ValueError("不支持的万相备用模型")
    public_base = str(current.get("public_base_url") or "")
    if public_base and not public_base.startswith("https://"):
        raise ValueError("公网素材地址必须使用 HTTPS")
    for key in ("seedance_endpoint", "wan_endpoint"):
        endpoint = str(current.get(key) or "")
        if endpoint and not endpoint.startswith("https://"):
            raise ValueError("自定义模型地址必须使用 HTTPS")
    path = _settings_path()
    temporary = path.with_suffix(".json.tmp")
    with _settings_lock:
        temporary.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    return provider_settings()


def _safe_id(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text or any(character not in "0123456789abcdef-" for character in text):
        raise ValueError("视频任务编号无效")
    return text


def _job_dir(job_id: str) -> Path:
    safe = _safe_id(job_id)
    path = (storage_root() / safe).resolve()
    if path.parent != storage_root():
        raise ValueError("视频任务路径无效")
    return path


def _manifest_path(job_id: str) -> Path:
    return _job_dir(job_id) / "job.json"


def _write_manifest(job: dict) -> dict:
    job = dict(job)
    job["updated_at"] = _now_iso()
    path = _manifest_path(job["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    with _manifest_lock:
        temporary.write_text(
            json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    return job


def get_job(job_id: str) -> dict:
    path = _manifest_path(job_id)
    if not path.is_file():
        raise ValueError("找不到该视频任务")
    with _manifest_lock:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("视频任务记录损坏") from exc
    if not isinstance(value, dict):
        raise ValueError("视频任务记录无效")
    return value


def _public_job(job: dict) -> dict:
    assets = [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "kind": row.get("kind"),
            "size": row.get("size"),
            "source": "url" if row.get("source_url") else "upload",
        }
        for row in job.get("assets") or []
    ]
    result = {
        key: value
        for key, value in job.items()
        if key not in {
            "provider_task_id", "output_filename", "fingerprint",
            "credential_owner_id",
        }
    }
    result["assets"] = assets
    result["provider_attempts"] = [
        {key: value for key, value in row.items() if key != "task_id"}
        for row in job.get("provider_attempts") or []
        if isinstance(row, dict)
    ]
    result["content_url"] = (
        f"/api/ai-videos/jobs/{quote(str(job['id']))}/content"
        if job.get("status") == "succeeded" and job.get("output_filename")
        else ""
    )
    first_image = next(
        (row for row in job.get("assets") or [] if row.get("kind") == "image"), None
    )
    result["cover_url"] = (
        f"/api/ai-videos/jobs/{quote(str(job['id']))}/cover" if first_image else ""
    )
    return result


def provider_settings(user_id=None) -> dict:
    seedance = _provider_config(SEEDANCE_PROVIDER, user_id)
    wan = _provider_config(WAN_PROVIDER, user_id)
    public_base = _common_config()["public_base_url"]
    seedance_configured = _provider_is_configured(SEEDANCE_PROVIDER, user_id)
    wan_configured = _provider_is_configured(WAN_PROVIDER, user_id)
    public_base_configured = public_base.startswith(("https://", "http://"))
    available_providers = []
    if seedance_configured:
        available_providers.append("Seedance 2.5")
    if wan_configured:
        available_providers.append("Wan 3.0")
    return {
        "configured": seedance_configured or wan_configured,
        "fully_configured": seedance_configured and wan_configured and public_base_configured,
        "provider": "Seedance 2.5 → Wan 3.0",
        "available_providers": available_providers,
        "local_transcode_available": _local_transcode_tools() is not None,
        "seedance_configured": seedance_configured,
        "seedance_api_key_masked": _mask_key(seedance["api_key"]),
        "seedance_region": seedance["region"],
        "seedance_model": seedance["model"],
        "seedance_endpoint": seedance["endpoint"],
        "wan_configured": wan_configured,
        "wan_api_key_masked": _mask_key(wan["api_key"]),
        "wan_workspace_id": wan["workspace_id"],
        "wan_region": wan["region"],
        "wan_model": wan["model"],
        "wan_endpoint": wan["endpoint"],
        # Legacy response keys keep older desktop clients functional.
        "api_key_masked": _mask_key(wan["api_key"]),
        "workspace_id": wan["workspace_id"],
        "region": wan["region"],
        "model": wan["model"],
        "public_base_url": public_base,
        "endpoint": wan["endpoint"],
        "public_base_configured": public_base_configured,
        "default_duration": 10,
        "default_resolution": "720P",
        "requirements": {
            "ratio": "9:16",
            "duration": "10–60 秒",
            "min_resolution": "360×640",
            "max_size": "280 MB",
            "formats": "MP4、MOV、MPEG、AVI（成品统一 MP4）",
        },
    }


def _clean_name(filename: object, extension: str) -> str:
    stem = Path(str(filename or "素材")).stem
    stem = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in stem
    ).strip("._")[:70]
    return f"{stem or 'asset'}{extension}"


def _copy_upload(upload: FileStorage, destination: Path, maximum: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    upload.stream.seek(0)
    with destination.open("wb") as target:
        while True:
            chunk = upload.stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ValueError(
                    "图片素材不能超过 20 MB，视频素材不能超过 100 MB"
                )
            digest.update(chunk)
            target.write(chunk)
    upload.stream.seek(0)
    if total <= 0:
        raise ValueError("素材文件不能为空")
    return total, digest.hexdigest()


def _validate_remote_asset_url(value: object) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("素材链接格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError("素材链接必须使用 HTTP 或 HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("素材链接不能包含账号或密码")
    if len(url) > 4000:
        raise ValueError("素材链接过长")
    try:
        addresses = {
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(hostname, port or (443 if parsed.scheme.lower() == "https" else 80))
        }
    except (OSError, ValueError) as exc:
        raise ValueError("素材链接域名无法解析") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("素材链接不能指向本机或内网地址")
    return url


def _parse_url_asset_specs(value: object) -> list[dict]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("链接素材参数无效") from exc
    if not isinstance(value, list):
        raise ValueError("链接素材参数无效")
    specs = []
    for row in value:
        if isinstance(row, str):
            row = {"url": row}
        if not isinstance(row, dict):
            raise ValueError("链接素材参数无效")
        url = _validate_remote_asset_url(row.get("url"))
        kind = str(row.get("kind") or "").strip().lower()
        if kind not in {"image", "video"}:
            raise ValueError("请为链接素材选择图片或视频类型")
        name = " ".join(str(row.get("name") or "").split())[:120]
        specs.append({"url": url, "kind": kind, "name": name})
    return specs


def _asset_extension(kind: str, content_type: str, url: str) -> str:
    allowed = IMAGE_EXTENSIONS if kind == "image" else VIDEO_EXTENSIONS
    extension = Path(urlsplit(url).path).suffix.lower()
    if extension == ".jpe":
        extension = ".jpg"
    if extension in allowed:
        return extension
    guessed = mimetypes.guess_extension(content_type or "") or ""
    if guessed == ".jpe":
        guessed = ".jpg"
    if guessed in allowed:
        return guessed
    return ".jpg" if kind == "image" else ".mp4" if kind == "video" else ""


def _download_url_asset(
    job_id: str,
    spec: dict,
    asset_id: str,
) -> dict:
    url = _validate_remote_asset_url(spec.get("url"))
    kind = str(spec.get("kind") or "").strip().lower()
    maximum = MAX_IMAGE_BYTES if kind == "image" else MAX_VIDEO_BYTES
    response = None
    destination = None
    completed = False
    try:
        response = requests.get(
            url,
            stream=True,
            allow_redirects=True,
            headers={"User-Agent": "MercadoWorkbench AI Video/1.0"},
            timeout=(10, 120),
        )
        response.raise_for_status()
        final_url = _validate_remote_asset_url(getattr(response, "url", None) or url)
        headers = getattr(response, "headers", {}) or {}
        content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        generic_types = {"", "application/octet-stream", "binary/octet-stream"}
        if content_type not in generic_types:
            if kind == "image" and not content_type.startswith("image/"):
                raise ValueError("图片链接返回的不是图片文件")
            if kind == "video" and not content_type.startswith("video/"):
                raise ValueError("视频链接返回的不是视频文件")
        extension = _asset_extension(kind, content_type, final_url)
        if kind == "image" and extension not in IMAGE_EXTENSIONS:
            raise ValueError("链接素材格式仅支持 JPG、PNG、BMP、WEBP")
        if kind == "video" and extension not in VIDEO_EXTENSIONS:
            raise ValueError("链接视频格式仅支持 MP4、MOV、AVI、MPEG、MKV、WEBM")
        try:
            content_length = int(str(headers.get("Content-Length") or "0"))
        except ValueError:
            content_length = 0
        if content_length > maximum:
            raise ValueError("图片素材不能超过 20 MB，视频素材不能超过 100 MB")
        assets_dir = _job_dir(job_id) / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{asset_id}{extension}"
        destination = assets_dir / stored_name
        digest = hashlib.sha256()
        total = 0
        with destination.open("wb") as target:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > maximum:
                    raise ValueError("图片素材不能超过 20 MB，视频素材不能超过 100 MB")
                digest.update(chunk)
                target.write(chunk)
        if total <= 0:
            raise ValueError("链接素材不能为空")
        name = str(spec.get("name") or "").strip()
        if not name:
            name = Path(urlsplit(final_url).path).name or f"链接素材-{asset_id}"
        completed = True
        return {
            "id": asset_id,
            "name": _clean_name(name, extension),
            "stored_name": stored_name,
            "kind": kind,
            "size": total,
            "sha256": digest.hexdigest(),
            "mime_type": content_type or mimetypes.guess_type(stored_name)[0]
            or ("image/jpeg" if kind == "image" else "video/mp4"),
            "source_url": url,
        }
    except requests.RequestException as exc:
        raise ValueError("素材链接下载失败，请检查链接是否可公开访问") from exc
    finally:
        if destination is not None and destination.exists() and not completed:
            destination.unlink(missing_ok=True)
        if response is not None:
            response.close()


def _parse_positive_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalize_local_fit_mode(value: object) -> str:
    mode = str(value or "crop").strip().lower()
    return mode if mode in LOCAL_FIT_MODES else "crop"


def _save_assets(job_id: str, uploads, *, require_any: bool = True, start_index: int = 1) -> list[dict]:
    uploads = [upload for upload in uploads or [] if upload and upload.filename]
    if not uploads:
        if require_any:
            raise ValueError("请至少添加一张图片或一段视频")
        return []
    if len(uploads) > MAX_ASSETS:
        raise ValueError(f"每个任务最多使用 {MAX_ASSETS} 个素材")
    assets_dir = _job_dir(job_id) / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    assets: list[dict] = []
    image_count = 0
    video_count = 0
    for index, upload in enumerate(uploads, start=start_index):
        extension = Path(str(upload.filename)).suffix.lower()
        if extension in IMAGE_EXTENSIONS:
            kind = "image"
            maximum = MAX_IMAGE_BYTES
            image_count += 1
            if image_count > MAX_IMAGES:
                raise ValueError(f"参考图片最多 {MAX_IMAGES} 张")
        elif extension in VIDEO_EXTENSIONS:
            kind = "video"
            maximum = MAX_VIDEO_BYTES
            video_count += 1
            if video_count > MAX_VIDEOS:
                raise ValueError(f"参考视频最多 {MAX_VIDEOS} 段")
        else:
            raise ValueError("素材仅支持 JPG、PNG、BMP、WEBP、MP4、MOV、AVI、MPEG、MKV、WEBM")
        asset_id = f"{index:02d}-{uuid.uuid4().hex[:10]}"
        stored_name = f"{asset_id}{extension}"
        destination = assets_dir / stored_name
        try:
            size, sha256 = _copy_upload(upload, destination, maximum)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        assets.append(
            {
                "id": asset_id,
                "name": _clean_name(upload.filename, extension),
                "stored_name": stored_name,
                "kind": kind,
                "size": size,
                "sha256": sha256,
                "mime_type": mimetypes.guess_type(stored_name)[0]
                or ("image/jpeg" if kind == "image" else "video/mp4"),
            }
        )
    return assets


def _save_url_assets(job_id: str, specs: list[dict], *, start_index: int, existing_assets: list[dict]) -> list[dict]:
    if not specs:
        return []
    image_count = sum(row.get("kind") == "image" for row in existing_assets)
    video_count = sum(row.get("kind") == "video" for row in existing_assets)
    assets = []
    for index, spec in enumerate(specs, start=start_index):
        if spec["kind"] == "image":
            image_count += 1
            if image_count > MAX_IMAGES:
                raise ValueError(f"参考图片最多 {MAX_IMAGES} 张")
        else:
            video_count += 1
            if video_count > MAX_VIDEOS:
                raise ValueError(f"参考视频最多 {MAX_VIDEOS} 段")
        assets.append(_download_url_asset(job_id, spec, f"{index:02d}-{uuid.uuid4().hex[:10]}"))
    return assets


def _order_assets(file_assets: list[dict], url_assets: list[dict], value: object) -> list[dict]:
    assets = file_assets + url_assets
    if not value:
        return assets
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("素材顺序参数无效") from exc
    if not isinstance(value, list) or len(value) != len(assets):
        raise ValueError("素材顺序参数无效")
    ordered = []
    used = set()
    for row in value:
        if not isinstance(row, dict) or row.get("source") not in {"file", "url"}:
            raise ValueError("素材顺序参数无效")
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError) as exc:
            raise ValueError("素材顺序参数无效") from exc
        key = (row["source"], index)
        source = file_assets if row["source"] == "file" else url_assets
        if index < 0 or index >= len(source) or key in used:
            raise ValueError("素材顺序参数无效")
        used.add(key)
        ordered.append(source[index])
    if len(used) != len(assets):
        raise ValueError("素材顺序参数无效")
    return ordered


def _normalize_target(form: dict) -> dict:
    link_id = _parse_positive_int(form.get("link_id"), 0, 0, 2_147_483_647)
    thumbnail_url = str(form.get("thumbnail_url") or "").strip()[:1000]
    if not thumbnail_url.startswith(("https://", "http://")):
        thumbnail_url = ""
    return {
        "link_id": link_id,
        "item_id": str(form.get("item_id") or "").strip()[:80],
        "title": str(form.get("title") or "").strip()[:300],
        "store_name": str(form.get("store_name") or "").strip()[:120],
        "site_id": str(form.get("site_id") or "").strip().upper()[:8],
        "thumbnail_url": thumbnail_url,
    }


def _fingerprint(job: dict) -> str:
    value = {
        "assets": [row.get("sha256") for row in job.get("assets") or []],
        "prompt": job.get("user_prompt"),
        "duration": job.get("duration"),
        "audio": job.get("audio"),
        "target": job.get("target"),
        "provider_order": job.get("provider_order") or list(PROVIDER_ORDER),
        "models": job.get("models") or {},
        "local_transcode": bool(job.get("local_transcode")),
        "local_fit_mode": _normalize_local_fit_mode(job.get("local_fit_mode")),
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _find_reusable_job(fingerprint: str) -> dict | None:
    for path in sorted(storage_root().glob("*/job.json"), reverse=True):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("fingerprint") != fingerprint:
            continue
        if job.get("status") in ACTIVE_STATUSES:
            return job
        if job.get("status") == "succeeded":
            output = _job_dir(job["id"]) / str(job.get("output_filename") or "")
            if output.is_file():
                return job
    return None


def create_job(uploads, form: dict) -> dict:
    job_id = str(uuid.uuid4())
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=False)
    try:
        local_transcode = str(form.get("local_transcode") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        local_fit_mode = _normalize_local_fit_mode(form.get("local_fit_mode"))
        file_assets = _save_assets(job_id, uploads, require_any=False)
        url_assets = _save_url_assets(
            job_id,
            _parse_url_asset_specs(form.get("asset_urls")),
            start_index=len(file_assets) + 1,
            existing_assets=file_assets,
        )
        if len(file_assets) + len(url_assets) > MAX_ASSETS:
            raise ValueError(f"每个任务最多使用 {MAX_ASSETS} 个素材")
        assets = _order_assets(file_assets, url_assets, form.get("asset_order"))
        if not assets:
            raise ValueError("请至少添加一张图片或一段视频")
        duration = _parse_positive_int(form.get("duration"), 10, 10, 30)
        if local_transcode:
            if len(assets) != 1 or assets[0].get("kind") != "video":
                raise ValueError("仅转换格式模式只能添加一个视频，不能同时添加图片或多个视频")
            if not _local_transcode_tools():
                raise RuntimeError("服务器未安装可用的 FFmpeg，暂时无法进行本地视频格式转换")
        elif any(
            asset.get("kind") == "video"
            and Path(str(asset.get("stored_name") or "")).suffix.lower() not in AI_VIDEO_EXTENSIONS
            for asset in assets
        ):
            raise ValueError("AI 视频参考素材仅支持 MP4、MOV；其他格式请勾选“仅转换视频格式”")
        target = _normalize_target(form)
        user_prompt = " ".join(str(form.get("prompt") or "").split())[:1200]
        try:
            credential_owner_id = int(form.get("credential_owner_id") or 0)
        except (TypeError, ValueError):
            credential_owner_id = 0
        seedance = _provider_config(SEEDANCE_PROVIDER, credential_owner_id)
        wan = _provider_config(WAN_PROVIDER, credential_owner_id)
        job = {
            "id": job_id,
            "name": str(form.get("name") or "").strip()[:120]
            or target.get("title")
            or f"备选视频 {datetime.now().strftime('%m-%d %H:%M')}",
            "status": "queued",
            "message": "已进入生成队列",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "duration": duration,
            "resolution": "720P",
            "ratio": "9:16",
            "audio": True,
            "prompt_extend": False,
            "user_prompt": user_prompt,
            "target": target,
            "assets": assets,
            "provider": LOCAL_PROVIDER if local_transcode else SEEDANCE_PROVIDER,
            "provider_order": [] if local_transcode else list(PROVIDER_ORDER),
            "provider_index": 0,
            "models": {
                SEEDANCE_PROVIDER: seedance["model"],
                WAN_PROVIDER: wan["model"],
            },
            "model": "ffmpeg-h264-aac" if local_transcode else seedance["model"],
            "local_transcode": local_transcode,
            "local_fit_mode": local_fit_mode,
            "provider_task_id": "",
            "provider_attempts": [],
            "credential_owner_id": credential_owner_id,
            "output_filename": "",
            "published": [],
            "publish_attempts": [],
        }
        job["fingerprint"] = _fingerprint(job)
        reusable = _find_reusable_job(job["fingerprint"])
        if reusable:
            requested_name = " ".join(str(form.get("name") or "").split())[:120]
            if requested_name and requested_name != reusable.get("name"):
                reusable["name"] = requested_name
                _write_manifest(reusable)
            shutil.rmtree(job_dir)
            result = _public_job(reusable)
            result["reused"] = True
            return result
        _write_manifest(job)
    except Exception:
        if job_dir.exists():
            shutil.rmtree(job_dir)
        raise
    _start_worker(job_id)
    return _public_job(job)


def list_jobs(limit: int = 30, user_id=None) -> dict:
    rows: list[dict] = []
    try:
        requested_user_id = int(user_id or 0)
    except (TypeError, ValueError):
        requested_user_id = 0
    for path in sorted(
        storage_root().glob("*/job.json"),
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    ):
        if len(rows) >= max(1, min(100, int(limit or 30))):
            break
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        owner_id = _job_owner(job)
        if requested_user_id and owner_id not in {0, requested_user_id}:
            continue
        if job.get("status") in ACTIVE_STATUSES:
            _start_worker(str(job.get("id") or ""))
        rows.append(_public_job(job))
    return {"rows": rows, "settings": provider_settings(user_id)}


def public_job(job_id: str) -> dict:
    job = get_job(job_id)
    if job.get("status") in ACTIVE_STATUSES:
        _start_worker(job["id"])
    return _public_job(job)


def update_job(job_id: str, changes: dict) -> dict:
    job = get_job(job_id)
    if "name" in changes:
        name = " ".join(str(changes.get("name") or "").split())[:120]
        if not name:
            raise ValueError("备选视频名称不能为空")
        job["name"] = name
    _write_manifest(job)
    return _public_job(job)


def delete_job(job_id: str) -> dict:
    job = get_job(job_id)
    if job.get("status") in ACTIVE_STATUSES:
        raise ValueError("视频生成尚未完成，不能删除")
    job_dir = _job_dir(job_id)
    shutil.rmtree(job_dir)
    return {
        "id": str(job_id),
        "name": str(job.get("name") or ""),
        "deleted": True,
    }


def _signing_secret(job=None) -> bytes:
    user_id = _job_owner(job)
    seedance = _provider_config(SEEDANCE_PROVIDER, user_id)
    wan = _provider_config(WAN_PROVIDER, user_id)
    secret = str(
        os.environ.get("AI_VIDEO_ASSET_SIGNING_KEY")
        or os.environ.get("BIT_DB_API_TOKEN")
        or seedance["api_key"]
        or wan["api_key"]
        or ""
    ).strip()
    if not secret:
        raise RuntimeError("未配置视频素材签名密钥")
    return secret.encode("utf-8")


def _asset_signature(job_id: str, asset_id: str, expires: int) -> str:
    message = f"{job_id}:{asset_id}:{expires}".encode("utf-8")
    return hmac.new(
        _signing_secret(get_job(job_id)), message, hashlib.sha256
    ).hexdigest()


def signed_asset_url(job_id: str, asset: dict, expires: int | None = None) -> str:
    base_url = _common_config()["public_base_url"]
    if not base_url.startswith(("https://", "http://")):
        raise RuntimeError("请配置 AI_VIDEO_PUBLIC_BASE_URL，使模型能够读取视频素材")
    expires = int(expires or time.time() + 2 * 60 * 60)
    signature = _asset_signature(job_id, str(asset["id"]), expires)
    return (
        f"{base_url}/api/ai-videos/assets/{quote(job_id)}/{quote(str(asset['id']))}"
        f"?expires={expires}&signature={signature}"
    )


def resolve_signed_asset(job_id: str, asset_id: str, expires: object, signature: str) -> Path:
    try:
        expires_int = int(str(expires))
    except (TypeError, ValueError) as exc:
        raise ValueError("素材链接无效") from exc
    if expires_int < int(time.time()) or expires_int > int(time.time()) + 24 * 60 * 60:
        raise ValueError("素材链接已过期")
    expected = _asset_signature(job_id, asset_id, expires_int)
    if not signature or not hmac.compare_digest(expected, str(signature)):
        raise ValueError("素材链接签名无效")
    job = get_job(job_id)
    asset = next(
        (row for row in job.get("assets") or [] if str(row.get("id")) == asset_id),
        None,
    )
    if not asset:
        raise ValueError("找不到素材")
    path = (_job_dir(job_id) / "assets" / str(asset["stored_name"])).resolve()
    if path.parent != (_job_dir(job_id) / "assets").resolve() or not path.is_file():
        raise ValueError("找不到素材文件")
    return path


def _compact_prompt(job: dict, provider: str = WAN_PROVIDER) -> str:
    target = job.get("target") or {}
    site_id = str(target.get("site_id") or "").upper()
    language = SITE_LANGUAGES.get(site_id, "对应站点的本地语言")
    image_number = 0
    video_number = 0
    references: list[str] = []
    for asset in job.get("assets") or []:
        if asset.get("kind") == "image":
            image_number += 1
            references.append(
                f"@image{image_number}" if provider == SEEDANCE_PROVIDER else f"图{image_number}"
            )
        else:
            video_number += 1
            references.append(
                f"@video{video_number}" if provider == SEEDANCE_PROVIDER else f"视频{video_number}"
            )
    user_prompt = str(job.get("user_prompt") or "").strip()
    product = str(target.get("title") or target.get("item_id") or "该商品").strip()
    parts = [
        f"生成一条{job['duration']}秒、9:16竖屏、真实自然的美客多商品短视频，商品是“{product}”。",
        f"忠实保留{'、'.join(references)}中的商品外观、颜色、结构与包装，只展示同一商品；持续有自然运动、细节特写和使用场景，不能做静态幻灯片。",
        "画面清晰、光线良好，主体避开上下及右侧按钮安全区；不要黑边、平台页面截图、价格、折扣、优惠券、联系方式、二维码、竞品、夸大功效、大水印或第三方商标。",
        f"配原创免版权轻音乐或自然环境音；如有人声只用{language}，不要说价格和促销。",
    ]
    if user_prompt:
        parts.append(f"补充要求：{user_prompt}")
    return "".join(parts)[:2400]


def _wan_endpoint_base(job=None) -> str:
    config = _provider_config(WAN_PROVIDER, _job_owner(job))
    custom = config["endpoint"]
    if custom:
        return custom
    region = config["region"]
    hosts = {
        "cn-beijing": "cn-beijing.maas.aliyuncs.com",
        "ap-southeast-1": "ap-southeast-1.maas.aliyuncs.com",
        "ap-northeast-1": "ap-northeast-1.maas.aliyuncs.com",
        "eu-central-1": "eu-central-1.maas.aliyuncs.com",
        "us-east-1": "us-east-1.maas.aliyuncs.com",
        "cn-hongkong": "cn-hongkong.maas.aliyuncs.com",
    }
    host = hosts.get(region)
    if not host:
        raise RuntimeError("AI_VIDEO_WAN_REGION 不受支持")
    workspace_id = config["workspace_id"]
    if not workspace_id:
        raise RuntimeError("未配置 AI_VIDEO_WAN_WORKSPACE_ID")
    return f"https://{workspace_id}.{host}/api/v1"


def _seedance_endpoint_base(job=None) -> str:
    config = _provider_config(SEEDANCE_PROVIDER, _job_owner(job))
    if config["endpoint"]:
        return config["endpoint"]
    if config["region"] != "cn-beijing":
        raise RuntimeError("AI_VIDEO_SEEDANCE_REGION 不受支持")
    return "https://ark.cn-beijing.volces.com/api/v3"


def _api_key(provider: str, job=None) -> str:
    key = _provider_config(provider, _job_owner(job))["api_key"]
    if not key:
        if provider == SEEDANCE_PROVIDER:
            raise RuntimeError("未配置 AI_VIDEO_SEEDANCE_API_KEY 或 ARK_API_KEY")
        raise RuntimeError("未配置 AI_VIDEO_WAN_API_KEY 或 DASHSCOPE_API_KEY")
    return key


def _provider_headers(provider: str, async_request: bool = False, job=None) -> dict:
    headers = {"Authorization": f"Bearer {_api_key(provider, job)}"}
    if provider == SEEDANCE_PROVIDER or async_request:
        headers["Content-Type"] = "application/json"
    if provider == WAN_PROVIDER and async_request:
        headers["X-DashScope-Async"] = "enable"
    return headers


def _asset_provider_url(job: dict, asset: dict, *, inline_images: bool) -> str:
    public_base = _common_config()["public_base_url"]
    if asset.get("kind") == "image" and inline_images and not public_base:
        path = _job_dir(job["id"]) / "assets" / asset["stored_name"]
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{asset['mime_type']};base64,{encoded}"
    return signed_asset_url(job["id"], asset)


def _build_wan_payload(job: dict) -> dict:
    media = []
    for asset in job.get("assets") or []:
        media.append(
            {
                "type": "reference_image" if asset.get("kind") == "image" else "reference_video",
                "url": _asset_provider_url(job, asset, inline_images=True),
            }
        )
    model = (job.get("models") or {}).get(WAN_PROVIDER) or _provider_config(WAN_PROVIDER, _job_owner(job))["model"]
    return {
        "model": model,
        "input": {"prompt": _compact_prompt(job, WAN_PROVIDER), "media": media},
        "parameters": {
            "resolution": "720P",
            "ratio": "9:16",
            "duration": int(job.get("duration") or 10),
            "audio": True,
            "prompt_extend": False,
            "watermark": False,
        },
    }


def _build_seedance_payload(job: dict) -> dict:
    content = [{"type": "text", "text": _compact_prompt(job, SEEDANCE_PROVIDER)}]
    for asset in job.get("assets") or []:
        kind = str(asset.get("kind") or "")
        content_type = "image_url" if kind == "image" else "video_url"
        content.append(
            {
                "type": content_type,
                content_type: {"url": _asset_provider_url(job, asset, inline_images=False)},
                "role": "reference_image" if kind == "image" else "reference_video",
            }
        )
    model = ((job.get("models") or {}).get(SEEDANCE_PROVIDER)
             or _provider_config(SEEDANCE_PROVIDER, _job_owner(job))["model"])
    return {
        "model": model,
        "content": content,
        "generate_audio": True,
        "ratio": "9:16",
        "resolution": "720p",
        "duration": int(job.get("duration") or 10),
        "watermark": False,
    }


def build_provider_payload(job: dict, provider: str | None = None) -> dict:
    provider = provider or str(job.get("provider") or SEEDANCE_PROVIDER)
    if provider == SEEDANCE_PROVIDER:
        return _build_seedance_payload(job)
    if provider == WAN_PROVIDER:
        return _build_wan_payload(job)
    raise ValueError("不支持的视频生成供应商")


def _response_json(response, context: str) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        error_type = (
            _ProviderStateUncertainError
            if response.status_code == 408 or response.status_code >= 500
            else _ProviderRejectedError
        )
        raise error_type(f"{context}返回非 JSON（HTTP {response.status_code}）") from exc
    return payload if isinstance(payload, dict) else {}


def _provider_error(payload: dict, default: str) -> str:
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        output = {}
    error = payload.get("error") or {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    return str(
        payload.get("message")
        or output.get("message")
        or error.get("message")
        or error.get("code")
        or default
    )


def _submit_provider(job: dict, provider: str) -> str:
    if provider == SEEDANCE_PROVIDER:
        url = f"{_seedance_endpoint_base(job)}/content_generation/tasks"
    elif provider == WAN_PROVIDER:
        url = f"{_wan_endpoint_base(job)}/services/aigc/video-generation/video-synthesis"
    else:
        raise ValueError("不支持的视频生成供应商")
    try:
        response = requests.post(
            url,
            headers=_provider_headers(provider, async_request=True, job=job),
            json=build_provider_payload(job, provider),
            timeout=(30, 180),
        )
    except requests.RequestException as exc:
        raise _ProviderStateUncertainError(
            "提交视频任务时连接中断，远端结果未知；为避免重复计费，未自动切换备用模型"
        ) from exc
    payload = _response_json(response, "视频模型")
    if response.status_code == 408 or response.status_code >= 500:
        raise _ProviderStateUncertainError(
            f"{_provider_error(payload, f'视频模型提交状态未知（HTTP {response.status_code}）')}；"
            "远端结果未知，为避免重复计费，未自动切换备用模型"
        )
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        output = {}
    task_id = str(
        (payload.get("id") if provider == SEEDANCE_PROVIDER else output.get("task_id"))
        or ""
    ).strip()
    if not response.ok or not task_id:
        raise _ProviderRejectedError(
            _provider_error(payload, "视频模型提交失败")
        )
    return task_id


def _query_provider(task_id: str, provider: str, job=None) -> dict:
    if provider == SEEDANCE_PROVIDER:
        url = f"{_seedance_endpoint_base(job)}/content_generation/tasks/{quote(task_id)}"
    elif provider == WAN_PROVIDER:
        url = f"{_wan_endpoint_base(job)}/tasks/{quote(task_id)}"
    else:
        raise ValueError("不支持的视频生成供应商")
    try:
        response = requests.get(
            url,
            headers=_provider_headers(provider, job=job),
            timeout=(20, 60),
        )
    except requests.RequestException as exc:
        raise _ProviderStateUncertainError("视频任务状态查询连接中断") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise _ProviderStateUncertainError(
            f"视频任务查询返回非 JSON（HTTP {response.status_code}）"
        ) from exc
    if not isinstance(payload, dict):
        raise _ProviderStateUncertainError("视频任务查询返回了无效数据")
    if not response.ok:
        raise _ProviderStateUncertainError(_provider_error(payload, "视频任务查询失败"))
    return payload


def _normalized_provider_result(provider: str, payload: dict) -> dict:
    if provider == SEEDANCE_PROVIDER:
        content = payload.get("content") or {}
        if not isinstance(content, dict):
            content = {}
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        return {
            "status": str(payload.get("status") or "").upper(),
            "video_url": str(content.get("video_url") or content.get("file_url") or ""),
            "message": _provider_error(payload, "Seedance 视频任务失败"),
            "usage": usage,
            "duration": payload.get("duration"),
            "ratio": payload.get("ratio"),
            "resolution": payload.get("resolution"),
        }
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        output = {}
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return {
        "status": str(output.get("task_status") or "").upper(),
        "video_url": str(output.get("video_url") or ""),
        "message": str(output.get("message") or output.get("code") or "万相视频任务失败"),
        "usage": usage,
        "duration": None,
        "ratio": None,
        "resolution": None,
    }


def _download_output(job: dict, url: str) -> tuple[str, int]:
    if not str(url).startswith("https://"):
        raise RuntimeError("视频模型返回了不安全的下载地址")
    response = requests.get(url, stream=True, timeout=(30, 300))
    response.raise_for_status()
    destination = _job_dir(job["id"]) / "mercado-video.mp4"
    temporary = destination.with_suffix(".mp4.part")
    total = 0
    with temporary.open("wb") as output:
        for chunk in response.iter_content(1024 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_OUTPUT_BYTES:
                raise RuntimeError("生成的视频超过美客多 280 MB 限制")
            output.write(chunk)
    if total <= 0:
        raise RuntimeError("视频模型返回了空文件")
    with temporary.open("rb") as generated:
        if b"ftyp" not in generated.read(32):
            temporary.unlink(missing_ok=True)
            raise RuntimeError("视频模型返回的文件不是有效 MP4")
    os.replace(temporary, destination)
    return destination.name, total


def _validate_provider_result(result: dict) -> None:
    usage = result.get("usage") or {}
    try:
        duration = float(
            result.get("duration")
            or usage.get("output_video_duration")
            or usage.get("duration")
            or 0
        )
    except (TypeError, ValueError):
        duration = 0
    if duration and not 9.5 <= duration <= 60.5:
        raise RuntimeError(f"生成视频时长 {duration:g} 秒，不符合美客多 10–60 秒要求")
    ratio = str(result.get("ratio") or usage.get("ratio") or "").strip()
    if ratio and ratio != "9:16":
        raise RuntimeError(f"生成视频比例 {ratio}，不符合美客多 9:16 竖屏要求")
    try:
        resolution_text = str(result.get("resolution") or usage.get("SR") or "0")
        resolution = int("".join(character for character in resolution_text if character.isdigit()) or 0)
    except (TypeError, ValueError):
        resolution = 0
    if resolution and resolution < 640:
        raise RuntimeError("生成视频分辨率低于美客多 360×640 最低要求")


def _provider_unavailable_reason(provider: str, job: dict) -> str:
    if not _provider_is_configured(provider, _job_owner(job)):
        return "未配置供应商凭证"
    public_base = _common_config()["public_base_url"]
    if provider == SEEDANCE_PROVIDER and not public_base:
        return "Seedance 参考素材需要 AI_VIDEO_PUBLIC_BASE_URL"
    if provider == WAN_PROVIDER and not public_base and any(
        asset.get("kind") == "video" for asset in job.get("assets") or []
    ):
        return "万相读取参考视频需要 AI_VIDEO_PUBLIC_BASE_URL"
    return ""


def _provider_display_name(provider: str) -> str:
    return "Seedance 2.5" if provider == SEEDANCE_PROVIDER else "Wan 3.0"


def _record_provider_attempt(
    job: dict,
    provider: str,
    *,
    status: str,
    message: str,
) -> None:
    attempts = list(job.get("provider_attempts") or [])
    attempts.append(
        {
            "provider": provider,
            "model": (job.get("models") or {}).get(provider) or job.get("model"),
            "task_id": str(job.get("provider_task_id") or ""),
            "status": status,
            "message": str(message)[:500],
            "at": _now_iso(),
        }
    )
    job["provider_attempts"] = attempts[-10:]


def _job_provider_order(job: dict) -> tuple[list[str], int]:
    configured_order = job.get("provider_order")
    if not isinstance(configured_order, list):
        # Jobs created by the old Wan-only implementation must never be
        # resubmitted to Seedance while they are being resumed.
        return [WAN_PROVIDER], 0
    order = [provider for provider in configured_order if provider in PROVIDER_ORDER]
    if not order:
        order = list(PROVIDER_ORDER)
    try:
        index = int(job.get("provider_index") or 0)
    except (TypeError, ValueError):
        index = 0
    return order, max(0, min(index, len(order) - 1))


def _probe_local_video(path: Path, ffmpeg: str, ffprobe: str) -> dict:
    if not ffprobe:
        try:
            import imageio_ffmpeg

            reader = imageio_ffmpeg.read_frames(str(path))
            try:
                metadata = next(reader)
            finally:
                reader.close()
        except (ImportError, OSError, RuntimeError, StopIteration) as exc:
            raise RuntimeError("无法读取源视频规格，请检查 FFmpeg 安装") from exc
        size = metadata.get("size") or metadata.get("source_size") or (0, 0)
        try:
            width, height = int(size[0]), int(size[1])
            rotation = int(float(metadata.get("rotate") or 0))
            duration = float(metadata.get("duration") or 0)
        except (TypeError, ValueError, IndexError):
            width = height = rotation = 0
            duration = 0
        if abs(rotation) % 180 == 90:
            width, height = height, width
        try:
            inspection = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            has_audio = "Audio:" in (inspection.stderr or "")
        except (OSError, subprocess.TimeoutExpired):
            has_audio = False
        return _validate_local_video_info(width, height, duration, has_audio)

    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("无法读取源视频规格，请检查 FFprobe 安装") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[-400:]
        raise RuntimeError(f"无法读取源视频规格：{detail or '文件格式无效'}")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFprobe 返回了无效的视频信息") from exc
    streams = payload.get("streams") or []
    video_stream = next(
        (row for row in streams if isinstance(row, dict) and row.get("codec_type") == "video"),
        None,
    )
    if not video_stream:
        raise RuntimeError("源文件不包含视频画面")
    try:
        width = int(video_stream.get("width") or 0)
        height = int(video_stream.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0
    rotation = 0
    for side_data in video_stream.get("side_data_list") or []:
        if not isinstance(side_data, dict):
            continue
        try:
            rotation = int(side_data.get("rotation") or 0)
        except (TypeError, ValueError):
            rotation = 0
        if rotation:
            break
    if abs(rotation) % 180 == 90:
        width, height = height, width
    format_info = payload.get("format") or {}
    duration_value = format_info.get("duration") if isinstance(format_info, dict) else None
    if not duration_value:
        duration_value = video_stream.get("duration")
    try:
        duration = float(duration_value or 0)
    except (TypeError, ValueError):
        duration = 0
    return _validate_local_video_info(
        width,
        height,
        duration,
        any(isinstance(row, dict) and row.get("codec_type") == "audio" for row in streams),
    )


def _validate_local_video_info(
    width: int,
    height: int,
    duration: float,
    has_audio: bool,
) -> dict:
    if width <= 0 or height <= 0:
        raise RuntimeError("无法识别源视频分辨率")
    if not 9.5 <= duration <= 60.5:
        raise RuntimeError(
            f"仅转换格式要求源视频为 10–60 秒，当前识别为 {duration:g} 秒"
        )
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "source_ratio": width / height,
        "has_audio": bool(has_audio),
    }


def _local_video_filter(fit_mode: str) -> str:
    """Build a non-AI 9:16 conversion filter without distorting the source."""
    mode = _normalize_local_fit_mode(fit_mode)
    if mode == "pad":
        return (
            f"scale={LOCAL_OUTPUT_WIDTH}:{LOCAL_OUTPUT_HEIGHT}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={LOCAL_OUTPUT_WIDTH}:{LOCAL_OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
            "setsar=1"
        )
    return (
        f"scale={LOCAL_OUTPUT_WIDTH}:{LOCAL_OUTPUT_HEIGHT}:"
        "force_original_aspect_ratio=increase,"
        f"crop={LOCAL_OUTPUT_WIDTH}:{LOCAL_OUTPUT_HEIGHT},"
        "setsar=1"
    )


def _run_local_transcode(job: dict) -> None:
    tools = _local_transcode_tools()
    if not tools:
        raise RuntimeError("服务器未安装可用的 FFmpeg，无法进行本地视频格式转换")
    ffmpeg, ffprobe = tools
    assets = job.get("assets") or []
    if len(assets) != 1 or assets[0].get("kind") != "video":
        raise RuntimeError("仅转换格式模式需要且只能使用一个视频")
    source = (_job_dir(job["id"]) / "assets" / str(assets[0]["stored_name"])).resolve()
    if source.parent != (_job_dir(job["id"]) / "assets").resolve() or not source.is_file():
        raise RuntimeError("找不到待转换的源视频")
    info = _probe_local_video(source, ffmpeg, ffprobe)
    fit_mode = _normalize_local_fit_mode(job.get("local_fit_mode"))
    fit_label = LOCAL_FIT_MODE_LABELS[fit_mode]
    destination = _job_dir(job["id"]) / "mercado-video.mp4"
    temporary = destination.with_suffix(".part.mp4")
    command = [ffmpeg, "-y", "-i", str(source)]
    if not info["has_audio"]:
        command.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"])
    command.extend(["-map", "0:v:0", "-map", "0:a:0" if info["has_audio"] else "1:a:0"])
    command.extend(
        [
            "-vf", _local_video_filter(fit_mode),
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "48000",
            "-ac", "2",
            "-map_metadata", "-1",
            "-movflags", "+faststart",
            "-max_muxing_queue_size", "1024",
        ]
    )
    if not info["has_audio"]:
        command.append("-shortest")
    command.append(str(temporary))
    job.update(
        status="generating",
        message=f"正在本地转换为美客多兼容 MP4（{fit_label}），不调用 AI",
    )
    _write_manifest(job)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1200,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("本地视频格式转换超时") from exc
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("无法启动 FFmpeg 视频格式转换") from exc
    if completed.returncode != 0:
        temporary.unlink(missing_ok=True)
        detail = (completed.stderr or "").strip()[-500:]
        raise RuntimeError(f"本地视频格式转换失败：{detail or 'FFmpeg 执行失败'}")
    output_size = temporary.stat().st_size if temporary.is_file() else 0
    if output_size <= 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("本地视频格式转换产生了空文件")
    if output_size > MAX_OUTPUT_BYTES:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("转换后的视频超过美客多 280 MB 限制")
    with temporary.open("rb") as generated:
        if b"ftyp" not in generated.read(32):
            temporary.unlink(missing_ok=True)
            raise RuntimeError("转换结果不是有效 MP4")
    os.replace(temporary, destination)
    job.update(
        status="succeeded",
        message=f"本地格式转换完成，未调用 AI 模型（{fit_label}）",
        duration=max(10, min(60, int(round(info["duration"])))),
        output_filename=destination.name,
        output_size=output_size,
        completed_at=_now_iso(),
        provider_usage={
            "local_transcode": True,
            "fit_mode": fit_mode,
            "output_width": LOCAL_OUTPUT_WIDTH,
            "output_height": LOCAL_OUTPUT_HEIGHT,
            **info,
        },
    )
    _write_manifest(job)


def _run_worker(job_id: str) -> None:
    try:
        job = get_job(job_id)
        if job.get("local_transcode"):
            _run_local_transcode(job)
            return
        order, start_index = _job_provider_order(job)
        interval = _parse_positive_int(
            os.environ.get("AI_VIDEO_POLL_INTERVAL_SECONDS"), 15, 3, 60
        )
        for index in range(start_index, len(order)):
            provider = order[index]
            provider_name = _provider_display_name(provider)
            unavailable = _provider_unavailable_reason(provider, job)
            if unavailable:
                _record_provider_attempt(
                    job, provider, status="skipped", message=unavailable
                )
                job.update(provider_index=index + 1, provider_task_id="")
                _write_manifest(job)
                continue

            job.update(
                provider=provider,
                provider_index=index,
                model=(job.get("models") or {}).get(provider)
                or _provider_config(provider, _job_owner(job))["model"],
            )
            task_id = str(job.get("provider_task_id") or "").strip()
            try:
                if not task_id:
                    job.update(
                        status="submitting",
                        message=f"正在向 {provider_name} 提交视频生成任务",
                    )
                    _write_manifest(job)
                    task_id = _submit_provider(job, provider)
                    job.update(
                        provider_task_id=task_id,
                        status="generating",
                        message=f"{provider_name} 正在生成视频，通常需要数分钟",
                    )
                    _write_manifest(job)

                deadline = time.monotonic() + _parse_positive_int(
                    os.environ.get("AI_VIDEO_POLL_TIMEOUT_SECONDS"), 2400, 300, 7200
                )
                consecutive_query_errors = 0
                while time.monotonic() < deadline:
                    try:
                        payload = _query_provider(task_id, provider, job)
                        consecutive_query_errors = 0
                    except _ProviderStateUncertainError as exc:
                        consecutive_query_errors += 1
                        if consecutive_query_errors >= 4:
                            raise _ProviderStateUncertainError(
                                f"{provider_name} 任务连续查询失败；任务可能仍在运行，未自动切换备用模型"
                            ) from exc
                        time.sleep(interval)
                        continue
                    result = _normalized_provider_result(provider, payload)
                    status = result["status"]
                    if status == "SUCCEEDED":
                        _validate_provider_result(result)
                        filename, output_size = _download_output(job, result["video_url"])
                        job.update(
                            status="succeeded",
                            message=f"{provider_name} 生成完成，已通过美客多基础规格预检",
                            output_filename=filename,
                            output_size=output_size,
                            completed_at=_now_iso(),
                            provider_usage=result["usage"],
                        )
                        _write_manifest(job)
                        return
                    if status in {"FAILED", "CANCELED", "CANCELLED", "UNKNOWN"}:
                        raise _ProviderTerminalError(result["message"])
                    time.sleep(interval)
                raise _ProviderStateUncertainError(
                    f"{provider_name} 生成超时；任务可能仍在运行，未自动切换备用模型"
                )
            except (_ProviderRejectedError, _ProviderTerminalError) as exc:
                _record_provider_attempt(job, provider, status="failed", message=str(exc))
                job.update(provider_task_id="", provider_index=index + 1)
                if index + 1 < len(order):
                    next_name = _provider_display_name(order[index + 1])
                    job.update(
                        status="submitting",
                        message=f"{provider_name} 失败，正在自动切换到 {next_name}",
                    )
                    _write_manifest(job)
                    continue
                _write_manifest(job)
                raise
        reasons = "; ".join(
            str(row.get("message") or "") for row in job.get("provider_attempts") or []
        )
        raise RuntimeError(f"没有可用的视频生成供应商：{reasons}"[:500])
    except Exception as exc:
        try:
            job = get_job(job_id)
            job.update(status="failed", message=str(exc)[:500], failed_at=_now_iso())
            _write_manifest(job)
        except Exception:
            pass
    finally:
        with _worker_lock:
            _active_workers.discard(job_id)


def _start_worker(job_id: str) -> bool:
    job_id = _safe_id(job_id)
    with _worker_lock:
        if job_id in _active_workers:
            return False
        _active_workers.add(job_id)
    thread = threading.Thread(
        target=_run_worker,
        args=(job_id,),
        name=f"ai-video-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return True


def output_path(job_id: str) -> Path:
    job = get_job(job_id)
    if job.get("status") != "succeeded" or not job.get("output_filename"):
        raise ValueError("视频尚未生成完成")
    path = (_job_dir(job_id) / str(job["output_filename"])).resolve()
    if path.parent != _job_dir(job_id) or not path.is_file():
        raise ValueError("成品视频文件不存在")
    return path


def cover_path(job_id: str) -> Path:
    job = get_job(job_id)
    asset = next(
        (row for row in job.get("assets") or [] if row.get("kind") == "image"), None
    )
    if not asset:
        raise ValueError("该备选视频没有图片封面")
    path = (_job_dir(job_id) / "assets" / str(asset.get("stored_name") or "")).resolve()
    assets_dir = (_job_dir(job_id) / "assets").resolve()
    if path.parent != assets_dir or not path.is_file():
        raise ValueError("封面图片不存在")
    return path


def publish_job(job_id: str, link_id: int) -> dict:
    from bit.bit_store_link_video import upload_store_link_video

    job = get_job(job_id)
    attempt = {
        "link_id": int(link_id),
        "status": "uploading",
        "started_at": _now_iso(),
        "message": "正在提交美客多",
    }
    attempts = list(job.get("publish_attempts") or [])
    attempts.append(attempt)
    job["publish_attempts"] = attempts[-20:]
    _write_manifest(job)
    try:
        path = output_path(job_id)
        with path.open("rb") as stream:
            upload = FileStorage(stream=stream, filename=path.name, content_type="video/mp4")
            result = upload_store_link_video(int(link_id), upload)
    except Exception as exc:
        attempt.update(
            status="failed",
            finished_at=_now_iso(),
            message=str(exc)[:500] or type(exc).__name__,
        )
        job["publish_attempts"] = attempts[-20:]
        _write_manifest(job)
        raise
    attempt.update(
        status="accepted",
        finished_at=_now_iso(),
        item_id=result.get("item_id"),
        clip_uuid=result.get("clip_uuid"),
        message="美客多已受理，等待平台审核",
    )
    published = list(job.get("published") or [])
    published.append(
        {
            "link_id": int(link_id),
            "item_id": result.get("item_id"),
            "clip_uuid": result.get("clip_uuid"),
            "published_at": _now_iso(),
        }
    )
    job["published"] = published[-20:]
    job["publish_attempts"] = attempts[-20:]
    job["message"] = "视频已提交美客多，等待平台审核"
    _write_manifest(job)
    return result


__all__ = (
    "build_provider_payload",
    "cover_path",
    "create_job",
    "delete_job",
    "get_job",
    "list_jobs",
    "output_path",
    "provider_settings",
    "public_job",
    "publish_job",
    "resolve_signed_asset",
    "save_provider_settings",
    "update_job",
)
