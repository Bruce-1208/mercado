import io
import json
from unittest.mock import Mock

import pytest
from werkzeug.datastructures import FileStorage

from bit import bit_store_link_video as video
from mercado_api.client import MercadoAPIError, MercadoLibreClient


def upload(name="clip.mp4", content=b"sample video"):
    return FileStorage(stream=io.BytesIO(content), filename=name)


@pytest.mark.parametrize("name,content", [("file.txt", b"x"), ("clip.mp4", b""), ("", b"x")])
def test_reject_invalid_video(name, content):
    with pytest.raises(ValueError):
        video.validate_video(upload(name, content))


def test_reject_large_video(monkeypatch):
    monkeypatch.setattr(video, "MAX_VIDEO_BYTES", 4)
    with pytest.raises(ValueError, match="280"):
        video.validate_video(upload(content=b"12345"))


def test_clip_upload_uses_explicit_site_and_multipart():
    session = Mock()
    session.post.return_value.ok = True
    session.post.return_value.json.return_value = {"status": "accepted", "clip_uuid": "clip-1"}
    client = MercadoLibreClient("token", session=session)
    sites = [{"site_id": "MLM", "logistic_type": "remote"}]
    result = client.upload_item_clip("CBT123", io.BytesIO(b"video"), "video.mp4", sites)
    assert result["clip_uuid"] == "clip-1"
    args, kwargs = session.post.call_args
    assert args[0].endswith("/marketplace/items/CBT123/clips/upload")
    assert json.loads(kwargs["data"]["sites"]) == sites
    assert kwargs["files"]["file"][0] == "video.mp4"
    assert "Content-Type" not in kwargs["headers"]


def test_clip_timeout_is_not_retried():
    import requests
    session = Mock()
    session.post.side_effect = requests.Timeout()
    client = MercadoLibreClient("token", session=session)
    with pytest.raises(MercadoAPIError, match="结果未知"):
        client.upload_item_clip("CBT123", io.BytesIO(b"video"), "video.mp4", [{"site_id": "MLM"}])
    assert session.post.call_count == 1


@pytest.mark.parametrize("overrides", [{}, {"status": "paused"}, {"cbt_item_id": ""}, {"site_id": "MLB"}, {"shipping": {}}])
def test_resolve_only_selected_listing(monkeypatch, overrides):
    from bit import bit_mysql, bit_store_link_sync
    from erp import mercadolibre_store_link_store
    row = {"id": 1, "token_id": 7, "item_id": "MLM123", "site_id": "MLM"}
    monkeypatch.setattr(mercadolibre_store_link_store, "get_store_links_by_ids", lambda ids: [row])
    monkeypatch.setattr(bit_mysql, "get_mercado_store_token", lambda token_id: {"id": token_id})
    client = Mock()
    client.get_marketplace_item.return_value = {
        "status": "active", "cbt_item_id": "CBT123", "site_id": "MLM",
        "shipping": {"logistic_type": "remote"}, **overrides,
    }
    client.upload_item_clip.return_value = {"status": "accepted", "clip_uuid": "clip-1"}
    monkeypatch.setattr(bit_store_link_sync, "_client_and_token", lambda token: (client, token))
    if overrides:
        with pytest.raises(ValueError):
            video.upload_store_link_video(1, upload())
        client.upload_item_clip.assert_not_called()
    else:
        assert video.upload_store_link_video(1, upload())["item_id"] == "MLM123"
        assert client.upload_item_clip.call_args.args[3] == [{"site_id": "MLM", "logistic_type": "remote"}]


def test_upload_route_and_missing_file(monkeypatch):
    from bit import bit_interface as app_module
    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = {"id": 1, "username": "tester"}
    submit = Mock(return_value={"clip_uuid": "clip-1"})
    monkeypatch.setattr(app_module.bit_db_api, "upload_mercado_store_link_video", submit)
    assert client.post("/api/store-links/1/video").status_code == 400
    submit.assert_not_called()
    response = client.post("/api/store-links/1/video", data={"file": (io.BytesIO(b"video"), "clip.mp4")})
    assert response.status_code == 200
    assert "审核" in response.json["message"]
    assert submit.call_args.args[0] == 1


def test_proxy_forwards_multipart_without_json_header(monkeypatch):
    from bit import bit_db_api
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    session = Mock()
    session.request.return_value.ok = True
    session.request.return_value.json.return_value = {"status": "success", "data": {"clip_uuid": "clip-1"}}
    monkeypatch.setattr(bit_db_api, "DB_API_SESSION", session)
    assert bit_db_api.upload_mercado_store_link_video(1, upload())["clip_uuid"] == "clip-1"
    args, kwargs = session.request.call_args
    assert args[1].endswith("/api/db/store-links/1/video")
    assert "Content-Type" not in kwargs["headers"]
    assert kwargs["files"]["file"][1].read() == b"sample video"
