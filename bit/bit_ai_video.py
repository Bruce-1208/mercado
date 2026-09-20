"""AI product-video jobs optimized for Mercado Libre Clips.

The module keeps generation state on the server, submits compact reference jobs
to Alibaba Cloud Model Studio, and stores the finished MP4 before the provider's
temporary URL expires.  It deliberately avoids database coupling so the large
media files stay outside MySQL and survive application upgrades.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from werkzeug.datastructures import FileStorage


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_OUTPUT_BYTES = 280 * 1024 * 1024
MAX_IMAGES = 10
MAX_VIDEOS = 5
MAX_ASSETS = 20
ACTIVE_STATUSES = {"queued", "submitting", "generating"}
TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}
SITE_LANGUAGES = {"MLB": "巴西葡萄牙语", "MLM": "拉美西班牙语"}

_manifest_lock = threading.RLock()
_worker_lock = threading.Lock()
_active_workers: set[str] = set()
_settings_lock = threading.RLock()


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


def _provider_config() -> dict:
    saved = _load_saved_settings()
    return {
        "api_key": str(
            saved.get("api_key")
            or os.environ.get("AI_VIDEO_API_KEY")
            or os.environ.get("DASHSCOPE_API_KEY")
            or ""
        ).strip(),
        "workspace_id": str(
            saved.get("workspace_id")
            or os.environ.get("AI_VIDEO_WORKSPACE_ID")
            or ""
        ).strip(),
        "region": str(
            saved.get("region") or os.environ.get("AI_VIDEO_REGION") or "cn-beijing"
        ).strip(),
        "model": str(
            saved.get("model")
            or os.environ.get("AI_VIDEO_MODEL")
            or "wan3.0-video-prime"
        ).strip(),
        "public_base_url": str(
            saved.get("public_base_url")
            or os.environ.get("AI_VIDEO_PUBLIC_BASE_URL")
            or os.environ.get("BIT_PUBLIC_WORKBENCH_URL")
            or ""
        ).strip().rstrip("/"),
        "endpoint": str(
            saved.get("endpoint") or os.environ.get("AI_VIDEO_ENDPOINT") or ""
        ).strip().rstrip("/"),
    }


def save_provider_settings(changes: dict) -> dict:
    current = _load_saved_settings()
    api_key = str(changes.get("api_key") or "").strip()
    if api_key and "*" not in api_key:
        current["api_key"] = api_key
    fields = {
        "workspace_id": 100,
        "region": 40,
        "model": 100,
        "public_base_url": 500,
        "endpoint": 500,
    }
    for key, maximum in fields.items():
        if key in changes:
            current[key] = str(changes.get(key) or "").strip()[:maximum]
    if current.get("region") not in {
        "cn-beijing", "ap-southeast-1", "ap-northeast-1", "eu-central-1",
        "us-east-1", "cn-hongkong",
    }:
        raise ValueError("不支持的视频模型地域")
    if current.get("model") not in {"wan3.0-video-prime", "wan3.0-video"}:
        raise ValueError("不支持的视频生成模型")
    public_base = str(current.get("public_base_url") or "")
    if public_base and not public_base.startswith("https://"):
        raise ValueError("公网素材地址必须使用 HTTPS")
    endpoint = str(current.get("endpoint") or "")
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
        }
        for row in job.get("assets") or []
    ]
    result = {
        key: value
        for key, value in job.items()
        if key not in {"provider_task_id", "output_filename", "fingerprint"}
    }
    result["assets"] = assets
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


def provider_settings() -> dict:
    config = _provider_config()
    key = config["api_key"]
    workspace_id = config["workspace_id"]
    public_base = config["public_base_url"]
    return {
        "configured": bool(key and workspace_id),
        "provider": "阿里云百炼 · 万相 3.0",
        "api_key_masked": f"{key[:4]}{'*' * 8}{key[-4:]}" if len(key) >= 10 else ("已配置" if key else ""),
        "workspace_id": workspace_id,
        "region": config["region"],
        "model": config["model"],
        "public_base_url": public_base,
        "endpoint": config["endpoint"],
        "public_base_configured": public_base.startswith(("https://", "http://")),
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


def _parse_positive_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _save_assets(job_id: str, uploads) -> list[dict]:
    uploads = [upload for upload in uploads or [] if upload and upload.filename]
    if not uploads:
        raise ValueError("请至少添加一张图片或一段视频")
    if len(uploads) > MAX_ASSETS:
        raise ValueError(f"每个任务最多使用 {MAX_ASSETS} 个素材")
    assets_dir = _job_dir(job_id) / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    assets: list[dict] = []
    image_count = 0
    video_count = 0
    for index, upload in enumerate(uploads, start=1):
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
            raise ValueError("素材仅支持 JPG、PNG、BMP、WEBP、MP4、MOV")
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
        assets = _save_assets(job_id, uploads)
        duration = _parse_positive_int(form.get("duration"), 10, 10, 30)
        target = _normalize_target(form)
        user_prompt = " ".join(str(form.get("prompt") or "").split())[:1200]
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
            "provider": "dashscope-wan3",
            "model": _provider_config()["model"],
            "provider_task_id": "",
            "output_filename": "",
            "published": [],
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


def list_jobs(limit: int = 30) -> dict:
    rows: list[dict] = []
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
        if job.get("status") in ACTIVE_STATUSES:
            _start_worker(str(job.get("id") or ""))
        rows.append(_public_job(job))
    return {"rows": rows, "settings": provider_settings()}


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


def _signing_secret() -> bytes:
    config = _provider_config()
    secret = str(
        os.environ.get("AI_VIDEO_ASSET_SIGNING_KEY")
        or os.environ.get("BIT_DB_API_TOKEN")
        or config["api_key"]
        or ""
    ).strip()
    if not secret:
        raise RuntimeError("未配置视频素材签名密钥")
    return secret.encode("utf-8")


def _asset_signature(job_id: str, asset_id: str, expires: int) -> str:
    message = f"{job_id}:{asset_id}:{expires}".encode("utf-8")
    return hmac.new(_signing_secret(), message, hashlib.sha256).hexdigest()


def signed_asset_url(job_id: str, asset: dict, expires: int | None = None) -> str:
    base_url = _provider_config()["public_base_url"]
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


def _compact_prompt(job: dict) -> str:
    target = job.get("target") or {}
    site_id = str(target.get("site_id") or "").upper()
    language = SITE_LANGUAGES.get(site_id, "对应站点的本地语言")
    image_number = 0
    video_number = 0
    references: list[str] = []
    for asset in job.get("assets") or []:
        if asset.get("kind") == "image":
            image_number += 1
            references.append(f"图{image_number}")
        else:
            video_number += 1
            references.append(f"视频{video_number}")
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


def _endpoint_base() -> str:
    config = _provider_config()
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
        raise RuntimeError("AI_VIDEO_REGION 不受支持")
    workspace_id = config["workspace_id"]
    if not workspace_id:
        raise RuntimeError("未配置 AI_VIDEO_WORKSPACE_ID")
    return f"https://{workspace_id}.{host}/api/v1"


def _api_key() -> str:
    key = _provider_config()["api_key"]
    if not key:
        raise RuntimeError("未配置 AI_VIDEO_API_KEY 或 DASHSCOPE_API_KEY")
    return key


def _provider_headers(async_request: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {_api_key()}"}
    if async_request:
        headers["X-DashScope-Async"] = "enable"
        headers["Content-Type"] = "application/json"
    return headers


def build_provider_payload(job: dict) -> dict:
    media = []
    public_base = _provider_config()["public_base_url"]
    for asset in job.get("assets") or []:
        if asset.get("kind") == "image" and not public_base:
            path = _job_dir(job["id"]) / "assets" / asset["stored_name"]
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            url = f"data:{asset['mime_type']};base64,{encoded}"
        else:
            url = signed_asset_url(job["id"], asset)
        media.append(
            {
                "type": "reference_image" if asset.get("kind") == "image" else "reference_video",
                "url": url,
            }
        )
    return {
        "model": job.get("model") or "wan3.0-video-prime",
        "input": {"prompt": _compact_prompt(job), "media": media},
        "parameters": {
            "resolution": "720P",
            "ratio": "9:16",
            "duration": int(job.get("duration") or 10),
            "audio": True,
            "prompt_extend": False,
            "watermark": False,
        },
    }


def _submit_provider(job: dict) -> str:
    response = requests.post(
        f"{_endpoint_base()}/services/aigc/video-generation/video-synthesis",
        headers=_provider_headers(async_request=True),
        json=build_provider_payload(job),
        timeout=(30, 180),
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"视频模型返回非 JSON（HTTP {response.status_code}）") from exc
    output = payload.get("output") or {}
    task_id = str(output.get("task_id") or "").strip()
    if not response.ok or not task_id:
        raise RuntimeError(
            str(payload.get("message") or output.get("message") or "视频模型提交失败")
        )
    return task_id


def _query_provider(task_id: str) -> dict:
    response = requests.get(
        f"{_endpoint_base()}/tasks/{quote(task_id)}",
        headers=_provider_headers(),
        timeout=(20, 60),
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"视频任务查询返回非 JSON（HTTP {response.status_code}）") from exc
    if not response.ok:
        raise RuntimeError(str(payload.get("message") or "视频任务查询失败"))
    return payload


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


def _validate_provider_usage(payload: dict) -> None:
    usage = payload.get("usage") or {}
    try:
        duration = float(usage.get("output_video_duration") or usage.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration and not 9.5 <= duration <= 60.5:
        raise RuntimeError(f"生成视频时长 {duration:g} 秒，不符合美客多 10–60 秒要求")
    ratio = str(usage.get("ratio") or "").strip()
    if ratio and ratio != "9:16":
        raise RuntimeError(f"生成视频比例 {ratio}，不符合美客多 9:16 竖屏要求")
    try:
        resolution = int(usage.get("SR") or 0)
    except (TypeError, ValueError):
        resolution = 0
    if resolution and resolution < 640:
        raise RuntimeError("生成视频分辨率低于美客多 360×640 最低要求")


def _run_worker(job_id: str) -> None:
    try:
        job = get_job(job_id)
        task_id = str(job.get("provider_task_id") or "").strip()
        if not task_id:
            job.update(status="submitting", message="正在提交素材并创建 AI 视频任务")
            _write_manifest(job)
            task_id = _submit_provider(job)
            job.update(
                provider_task_id=task_id,
                status="generating",
                message="AI 正在生成视频，通常需要数分钟",
            )
            _write_manifest(job)
        deadline = time.monotonic() + _parse_positive_int(
            os.environ.get("AI_VIDEO_POLL_TIMEOUT_SECONDS"), 2400, 300, 7200
        )
        interval = _parse_positive_int(
            os.environ.get("AI_VIDEO_POLL_INTERVAL_SECONDS"), 15, 3, 60
        )
        while time.monotonic() < deadline:
            payload = _query_provider(task_id)
            output = payload.get("output") or {}
            status = str(output.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                _validate_provider_usage(payload)
                filename, output_size = _download_output(job, str(output.get("video_url") or ""))
                job.update(
                    status="succeeded",
                    message="生成完成，已通过美客多基础规格预检",
                    output_filename=filename,
                    output_size=output_size,
                    completed_at=_now_iso(),
                    provider_usage=payload.get("usage") or {},
                )
                _write_manifest(job)
                return
            if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                raise RuntimeError(
                    str(output.get("message") or output.get("code") or "AI 视频任务失败")
                )
            time.sleep(interval)
        raise RuntimeError("AI 视频生成超时，请稍后刷新任务状态")
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
    path = output_path(job_id)
    with path.open("rb") as stream:
        upload = FileStorage(stream=stream, filename=path.name, content_type="video/mp4")
        result = upload_store_link_video(int(link_id), upload)
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
    job["message"] = "视频已提交美客多，等待平台审核"
    _write_manifest(job)
    return result


__all__ = (
    "build_provider_payload",
    "cover_path",
    "create_job",
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
