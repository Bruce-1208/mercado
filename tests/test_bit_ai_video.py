import io
import json
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from werkzeug.datastructures import FileStorage

from bit import bit_ai_video as video


def upload(name="product.png", content=b"\x89PNG\r\n\x1a\nproduct"):
    return FileStorage(stream=io.BytesIO(content), filename=name)


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_VIDEO_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("AI_VIDEO_SEEDANCE_API_KEY", "seedance-test-key")
    monkeypatch.setenv("AI_VIDEO_API_KEY", "test-key")
    monkeypatch.setenv("AI_VIDEO_WORKSPACE_ID", "ws-test")
    monkeypatch.setenv("AI_VIDEO_PUBLIC_BASE_URL", "https://workbench.example")
    monkeypatch.setenv("AI_VIDEO_ASSET_SIGNING_KEY", "signing-test")
    monkeypatch.setattr(video, "_start_worker", lambda _job_id: True)


def test_create_job_builds_compact_mercado_payload(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    created = video.create_job(
        [upload()],
        {
            "duration": "10",
            "prompt": "重点展示折叠方式",
            "link_id": "7",
            "item_id": "MLM123",
            "site_id": "MLM",
            "title": "折叠收纳盒",
        },
    )
    job = video.get_job(created["id"])
    payload = video.build_provider_payload(job)

    assert payload["model"] == "doubao-seedance-2-5-260628"
    assert payload["generate_audio"] is True
    assert payload["resolution"] == "720p"
    assert payload["ratio"] == "9:16"
    assert payload["duration"] == 10
    assert payload["watermark"] is False
    assert payload["content"][1]["type"] == "image_url"
    assert payload["content"][1]["role"] == "reference_image"
    assert payload["content"][1]["image_url"]["url"].startswith(
        "https://workbench.example/api/ai-videos/assets/"
    )
    assert job["provider"] == video.SEEDANCE_PROVIDER

    wan_payload = video.build_provider_payload(job, video.WAN_PROVIDER)
    assert wan_payload["model"] == "wan3.0-video-prime"
    assert wan_payload["parameters"] == {
        "resolution": "720P",
        "ratio": "9:16",
        "duration": 10,
        "audio": True,
        "prompt_extend": False,
        "watermark": False,
    }
    assert wan_payload["input"]["media"][0]["type"] == "reference_image"
    assert wan_payload["input"]["media"][0]["url"].startswith(
        "https://workbench.example/api/ai-videos/assets/"
    )
    assert created["name"] == "折叠收纳盒"
    assert created["cover_url"].endswith("/cover")
    assert "价格" in payload["content"][0]["text"]
    assert "拉美西班牙语" in payload["content"][0]["text"]
    assert "@image1" in payload["content"][0]["text"]
    assert len(payload["content"][0]["text"]) < 2400


def test_identical_job_is_reused_without_new_generation(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    first = video.create_job([upload()], {"duration": "10", "prompt": "自然展示"})
    second = video.create_job([upload()], {"duration": "10", "prompt": "自然展示"})

    assert second["id"] == first["id"]
    assert second["reused"] is True
    assert len(list(tmp_path.glob("*/job.json"))) == 1

    renamed = video.update_job(first["id"], {"name": "墨西哥折叠演示 A"})
    assert renamed["name"] == "墨西哥折叠演示 A"


def test_asset_limits_and_signature(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    created = video.create_job([upload()], {})
    job = video.get_job(created["id"])
    url = video.signed_asset_url(job["id"], job["assets"][0])
    query = url.split("?", 1)[1]
    values = dict(part.split("=", 1) for part in query.split("&"))
    path = video.resolve_signed_asset(
        job["id"], job["assets"][0]["id"], values["expires"], values["signature"]
    )

    assert path.read_bytes().endswith(b"product")


def test_create_job_downloads_image_link_and_preserves_mixed_order(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        video.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )

    response = Mock()
    response.url = "https://example.com/product.png"
    response.headers = {"Content-Type": "image/png", "Content-Length": "15"}
    response.iter_content.return_value = [b"\x89PNG\r\n\x1a\nlinked"]
    monkeypatch.setattr(video.requests, "get", Mock(return_value=response))

    created = video.create_job(
        [upload("local.png", b"local")],
        {
            "asset_urls": json.dumps([{
                "url": "https://example.com/product.png",
                "kind": "image",
                "name": "远程主图",
            }]),
            "asset_order": json.dumps([
                {"source": "url", "index": 0},
                {"source": "file", "index": 0},
            ]),
        },
    )

    job = video.get_job(created["id"])
    assert [asset["kind"] for asset in job["assets"]] == ["image", "image"]
    assert job["assets"][0]["source_url"] == "https://example.com/product.png"
    assert job["assets"][0]["name"] == "远程主图.png"
    assert (tmp_path / created["id"] / "assets" / job["assets"][0]["stored_name"]).read_bytes().endswith(b"linked")


def test_create_job_rejects_private_asset_link(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="本机或内网"):
        video.create_job(
            [],
            {
                "asset_urls": json.dumps([{
                    "url": "http://127.0.0.1/image.png",
                    "kind": "image",
                }])
            },
        )


def test_settings_are_saved_server_side_and_key_is_masked(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    settings = video.save_provider_settings({
        "api_key": "sk-1234567890abcdef",
        "workspace_id": "ws-video",
        "region": "ap-southeast-1",
        "model": "wan3.0-video",
        "public_base_url": "https://video.example",
    })

    assert settings["configured"] is True
    assert settings["api_key_masked"].startswith("sk-1")
    assert "1234567890abcdef" not in settings["api_key_masked"]
    assert settings["workspace_id"] == "ws-video"
    assert settings["public_base_url"] == "https://video.example"
    assert "sk-1234567890abcdef" in (tmp_path / "settings.json").read_text(encoding="utf-8")


def test_dual_provider_settings_and_masking(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    settings = video.save_provider_settings({
        "seedance_api_key": "ark-1234567890abcdef",
        "seedance_region": "cn-beijing",
        "seedance_model": "doubao-seedance-2-5-260628",
        "wan_api_key": "sk-1234567890abcdef",
        "wan_workspace_id": "ws-video",
        "wan_region": "cn-beijing",
        "wan_model": "wan3.0-video-prime",
        "public_base_url": "https://video.example",
    })

    assert settings["fully_configured"] is True
    assert settings["seedance_configured"] is True
    assert settings["wan_configured"] is True
    assert "1234567890abcdef" not in settings["seedance_api_key_masked"]
    assert "1234567890abcdef" not in settings["wan_api_key_masked"]
    assert settings["provider"] == "Seedance 2.5 → Wan 3.0"


def test_either_single_provider_is_enough(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_VIDEO_STORAGE_PATH", str(tmp_path))
    for name in (
        "AI_VIDEO_WAN_API_KEY", "AI_VIDEO_API_KEY", "DASHSCOPE_API_KEY",
        "AI_VIDEO_WAN_WORKSPACE_ID", "AI_VIDEO_WORKSPACE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_VIDEO_SEEDANCE_API_KEY", "seedance-only")
    monkeypatch.delenv("ARK_API_KEY", raising=False)

    seedance_only = video.provider_settings()
    assert seedance_only["configured"] is True
    assert seedance_only["seedance_configured"] is True
    assert seedance_only["wan_configured"] is False

    monkeypatch.delenv("AI_VIDEO_SEEDANCE_API_KEY", raising=False)
    monkeypatch.setenv("AI_VIDEO_WAN_API_KEY", "wan-only")
    monkeypatch.setenv("AI_VIDEO_WAN_WORKSPACE_ID", "ws-only")
    wan_only = video.provider_settings()
    assert wan_only["configured"] is True
    assert wan_only["seedance_configured"] is False
    assert wan_only["wan_configured"] is True


def test_local_transcode_creates_mp4_without_model_call(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(video, "_local_transcode_tools", lambda: ("ffmpeg", "ffprobe"))
    created = video.create_job(
        [upload("ready.avi", b"fake-video")],
        {"local_transcode": "1", "name": "本地转换"},
    )
    assert created["provider"] == video.LOCAL_PROVIDER
    assert created["local_transcode"] is True

    probe_payload = {
        "streams": [
            {"codec_type": "video", "width": 1080, "height": 1920},
            {"codec_type": "audio"},
        ],
        "format": {"duration": "12.4"},
    }

    def run(command, **_kwargs):
        if command[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout=json.dumps(probe_payload), stderr="")
        Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisomconverted")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video.subprocess, "run", run)
    submitting = Mock(side_effect=AssertionError("本地转换不应调用模型"))
    monkeypatch.setattr(video, "_submit_provider", submitting)

    video._run_worker(created["id"])

    job = video.get_job(created["id"])
    assert job["status"] == "succeeded"
    assert job["duration"] == 12
    assert job["message"] == "本地格式转换完成，未调用 AI 模型"
    assert (tmp_path / created["id"] / "mercado-video.mp4").is_file()
    submitting.assert_not_called()


def test_local_transcode_requires_one_vertical_video(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(video, "_local_transcode_tools", lambda: ("ffmpeg", "ffprobe"))
    with pytest.raises(ValueError, match="只能添加一个视频"):
        video.create_job([upload("product.png")], {"local_transcode": "1"})


def test_publish_job_persists_success_and_failure_attempts(monkeypatch, tmp_path):
    from bit import bit_store_link_video

    configure(monkeypatch, tmp_path)
    created = video.create_job([upload()], {"duration": "10"})
    job = video.get_job(created["id"])
    output = tmp_path / created["id"] / "mercado-video.mp4"
    output.write_bytes(b"video")
    job.update(status="succeeded", output_filename=output.name, output_size=output.stat().st_size)
    video._write_manifest(job)

    monkeypatch.setattr(
        bit_store_link_video,
        "upload_store_link_video",
        lambda link_id, _upload: {"link_id": link_id, "item_id": "MLM123", "clip_uuid": "clip-1"},
    )
    result = video.publish_job(created["id"], 42)
    saved = video.get_job(created["id"])
    assert result["clip_uuid"] == "clip-1"
    assert saved["publish_attempts"][-1]["status"] == "accepted"
    assert saved["publish_attempts"][-1]["clip_uuid"] == "clip-1"
    assert saved["published"][-1]["link_id"] == 42

    def reject(_link_id, _upload):
        raise ValueError("商品物流类型不完整")

    monkeypatch.setattr(bit_store_link_video, "upload_store_link_video", reject)
    with pytest.raises(ValueError, match="物流类型"):
        video.publish_job(created["id"], 43)
    failed = video.get_job(created["id"])["publish_attempts"][-1]
    assert failed["status"] == "failed"
    assert failed["link_id"] == 43
    assert "物流类型" in failed["message"]


def test_non_mp4_reference_requires_local_transcode_mode(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="仅支持 MP4、MOV"):
        video.create_job([upload("source.avi", b"fake-video")], {})


def test_worker_falls_back_to_wan_after_seedance_rejection(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    created = video.create_job([upload()], {"duration": "10"})
    submitted = []

    def submit(_job, provider):
        submitted.append(provider)
        if provider == video.SEEDANCE_PROVIDER:
            raise video._ProviderRejectedError("Seedance rejected")
        return "wan-task-1"

    monkeypatch.setattr(video, "_submit_provider", submit)
    monkeypatch.setattr(
        video,
        "_query_provider",
        lambda _task_id, provider, _job: {
            "output": {"task_status": "SUCCEEDED", "video_url": "https://example.com/out.mp4"},
            "usage": {"duration": 10, "ratio": "9:16", "SR": 720},
        } if provider == video.WAN_PROVIDER else {},
    )
    monkeypatch.setattr(video, "_download_output", lambda _job, _url: ("mercado-video.mp4", 123))

    video._run_worker(created["id"])

    job = video.get_job(created["id"])
    assert submitted == [video.SEEDANCE_PROVIDER, video.WAN_PROVIDER]
    assert job["status"] == "succeeded"
    assert job["provider"] == video.WAN_PROVIDER
    assert job["provider_attempts"][0]["provider"] == video.SEEDANCE_PROVIDER
    assert "Seedance rejected" in job["provider_attempts"][0]["message"]


def test_worker_does_not_fallback_when_primary_task_state_is_uncertain(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    created = video.create_job([upload()], {})
    submitted = []
    monkeypatch.setattr(
        video,
        "_submit_provider",
        lambda _job, provider: submitted.append(provider) or "seedance-task-1",
    )
    monkeypatch.setattr(
        video,
        "_query_provider",
        lambda *_args: (_ for _ in ()).throw(video._ProviderStateUncertainError("network")),
    )
    monkeypatch.setattr(video.time, "sleep", lambda _seconds: None)

    video._run_worker(created["id"])

    job = video.get_job(created["id"])
    assert submitted == [video.SEEDANCE_PROVIDER]
    assert job["status"] == "failed"
    assert "未自动切换备用模型" in job["message"]


def test_worker_does_not_fallback_after_uncertain_submit_response(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    created = video.create_job([upload()], {})
    response = Mock()
    response.status_code = 503
    response.ok = False
    response.json.return_value = {"message": "provider temporarily unavailable"}
    posting = Mock(return_value=response)
    monkeypatch.setattr(video.requests, "post", posting)

    video._run_worker(created["id"])

    job = video.get_job(created["id"])
    assert posting.call_count == 1
    assert posting.call_args.args[0].endswith("/api/v3/content_generation/tasks")
    assert posting.call_args.kwargs["json"]["model"] == "doubao-seedance-2-5-260628"
    assert job["status"] == "failed"
    assert "未自动切换备用模型" in job["message"]


def test_workbench_contains_ai_video_module():
    from bit import bit_interface as app_module

    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = {"id": 1, "username": "tester"}

    response = client.get("/")
    assert response.status_code == 200
    assert b'data-tab="ai-video"' in response.data
    assert b'id="ai-video-dropzone"' in response.data
    assert b'id="ai-video-asset-url"' in response.data
    assert b'asset_urls' in response.data
    assert b'id="ai-video-jobs"' in response.data
    assert b'id="ai-video-settings-dialog"' in response.data
    assert b'id="ai-video-seedance-api-key"' in response.data
    assert b'id="ai-video-wan-api-key"' in response.data
    assert b'id="ai-video-local-transcode"' in response.data
    assert "复制粘贴".encode("utf-8") in response.data
    assert "AI生成视频".encode("utf-8") in response.data
    assert "选择AI备选".encode("utf-8") in response.data


def test_ai_weight_price_page_redirects_to_login_when_session_expires():
    from bit import bit_interface as app_module

    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = app_module.app.test_client()

    response = client.get("/ai-weight-price")

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login?next=/ai-weight-price")


def test_ai_video_routes_use_central_api(monkeypatch):
    from bit import bit_interface as app_module

    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = {"id": 1, "username": "tester"}

    listing = Mock(return_value={"rows": [], "settings": {"configured": True}})
    creating = Mock(return_value={"id": "job-1", "status": "queued"})
    settings = Mock(return_value={"configured": True, "api_key_masked": "sk-a********bcde"})
    monkeypatch.setattr(app_module.bit_db_api, "list_ai_video_jobs", listing)
    monkeypatch.setattr(app_module.bit_db_api, "create_ai_video_job", creating)
    monkeypatch.setattr(app_module.bit_db_api, "get_ai_video_settings", settings)

    response = client.get("/api/ai-videos/jobs?limit=12")
    assert response.status_code == 200
    assert response.json["data"]["settings"]["configured"] is True
    listing.assert_called_once_with(12, user_id=1)

    response = client.post(
        "/api/ai-videos/jobs",
        data={"duration": "10", "files": (io.BytesIO(b"image"), "product.png")},
    )
    assert response.status_code == 200
    assert response.json["data"]["id"] == "job-1"
    assert creating.call_args.args[0][0].filename == "product.png"

    response = client.get("/api/ai-videos/settings")
    assert response.status_code == 200
    assert response.json["data"]["api_key_masked"].startswith("sk-")


def test_video_provider_settings_use_account_bound_credentials(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        video,
        "_account_credential_resolver",
        lambda user_id: {
            "seedance_api_key": "account-seedance" if user_id == 7 else "",
            "wan_api_key": "account-wan" if user_id == 7 else "",
            "wan_workspace_id": "ws-account" if user_id == 7 else "",
        },
    )

    settings = video.provider_settings(7)
    assert settings["seedance_configured"] is True
    assert settings["wan_configured"] is True

    created = video.create_job(
        [upload()], {"duration": "10", "credential_owner_id": "7"}
    )
    private = video.get_job(created["id"])
    assert private["credential_owner_id"] == 7
    assert "credential_owner_id" not in created
