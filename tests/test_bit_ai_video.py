import io
from unittest.mock import Mock

from werkzeug.datastructures import FileStorage

from bit import bit_ai_video as video


def upload(name="product.png", content=b"\x89PNG\r\n\x1a\nproduct"):
    return FileStorage(stream=io.BytesIO(content), filename=name)


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_VIDEO_STORAGE_PATH", str(tmp_path))
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

    assert payload["model"] == "wan3.0-video-prime"
    assert payload["parameters"] == {
        "resolution": "720P",
        "ratio": "9:16",
        "duration": 10,
        "audio": True,
        "prompt_extend": False,
        "watermark": False,
    }
    assert payload["input"]["media"][0]["type"] == "reference_image"
    assert payload["input"]["media"][0]["url"].startswith(
        "https://workbench.example/api/ai-videos/assets/"
    )
    assert created["name"] == "折叠收纳盒"
    assert created["cover_url"].endswith("/cover")
    assert "价格" in payload["input"]["prompt"]
    assert "拉美西班牙语" in payload["input"]["prompt"]
    assert len(payload["input"]["prompt"]) < 2400


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
    assert b'id="ai-video-jobs"' in response.data
    assert b'id="ai-video-settings-dialog"' in response.data
    assert "复制粘贴".encode("utf-8") in response.data
    assert "AI生成视频".encode("utf-8") in response.data
    assert "选择AI备选".encode("utf-8") in response.data


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
    listing.assert_called_once_with(12)

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
