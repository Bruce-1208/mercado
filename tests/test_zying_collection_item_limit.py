"""Collection count limits do not inherit the paid review task's cap."""

import json
import zipfile
from pathlib import Path

import pytest

from bit import bit_interface, bit_zying_caiji


@pytest.mark.parametrize("limit", [10001, 1000000])
def test_collection_accepts_large_positive_count_without_default_page_cutoff(limit):
    params = bit_interface.build_zying_collection_params({"max_items": limit})

    assert params["max_items"] == limit
    assert params["number"] is None
    assert params["start_page"] == 1


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "10001", None])
def test_collection_rejects_nonpositive_or_noninteger_count(limit):
    with pytest.raises(ValueError, match="正整数"):
        bit_interface.build_zying_collection_params({"max_items": limit})


def test_count_collection_can_start_beyond_old_page_cutoff():
    params = bit_interface.build_zying_collection_params({
        "max_items": 10001, "start_page": 10001,
    })

    assert params["start_page"] == 10001
    assert params["number"] is None


def test_collection_retains_explicit_page_range():
    params = bit_interface.build_zying_collection_params({
        "max_items": 10001, "start_page": 2, "end_page": 5,
    })

    assert params["start_page"] == 2
    assert params["number"] == 5


def test_infringement_retains_item_and_page_limits():
    with pytest.raises(ValueError, match="1–10000"):
        bit_interface.build_zying_infringement_params({"max_items": 10001})

    params = bit_interface.build_zying_infringement_params({"max_items": 10000})
    assert params["max_items"] == 10000
    assert params["end_page"] == 10000


def test_authenticated_extension_endpoint_accepts_more_than_10000_products(monkeypatch):
    user = {
        "id": 7, "username": "collection-user", "access_version": 1,
        "role_key": "operator", "permissions": ["zying_collection.execute"],
    }
    session_user_ids = []
    billing_requests = []
    started = {}

    def session_user(user_id):
        session_user_ids.append(user_id)
        return dict(user)

    def billing(payload):
        billing_requests.append(payload)
        return {"user_id": 7, "status": "active"}

    def start_task(params, source_label):
        started.update(params=params, source_label=source_label)
        return {"running": True}, "", 200

    monkeypatch.setitem(bit_interface.app.config, "TESTING", True)
    monkeypatch.setattr(bit_interface.app, "secret_key", "count-endpoint-test-secret")
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    monkeypatch.setattr(bit_interface.bit_db_api, "get_workbench_session_user", session_user)
    monkeypatch.setattr(bit_interface.bit_db_api, "account_billing", billing)
    monkeypatch.setattr(bit_interface, "list_zying_collection_categories", lambda: [])
    monkeypatch.setattr(bit_interface, "_start_zying_collection_task", start_task)

    token = bit_interface.create_browser_extension_token(user)
    response = bit_interface.app.test_client().post(
        "/api/browser-extension/zying/start",
        headers={"Authorization": f"Bearer {token}"},
        json={"credential": "offline-browser-credential", "max_items": 25001},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "success"
    assert session_user_ids == [7]
    assert billing_requests == [{"action": "get", "actor_id": 7}]
    assert started["params"]["max_items"] == 25001
    assert started["params"]["number"] is None
    assert started["params"]["auth_token"] == "offline-browser-credential"


def test_extension_package_contains_current_count_limit_sources():
    extension_dir = Path(bit_interface.BROWSER_EXTENSION_DIR)
    package = bit_interface.build_browser_extension_package()

    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == json.loads((extension_dir / "manifest.json").read_text())["version"]
        for filename in ("manifest.json", "background.js", "popup.js", "popup.html", "launcher.js"):
            assert archive.read(filename) == (extension_dir / filename).read_bytes()
        assert b"const DOWNLOADED_ACCOUNT_CONFIG = null;" in archive.read("background.js")
        assert "最多产品数必须是正整数" in archive.read("popup.js").decode("utf-8")


def _prepare_offline_collector(monkeypatch, api_post):
    monkeypatch.setattr(bit_zying_caiji, "list_zying_product_developers", lambda _token: [])
    monkeypatch.setattr(bit_zying_caiji, "_load_zying_category_name_lookup", lambda _reader: {})
    monkeypatch.setattr(bit_zying_caiji, "_zying_api_post", api_post)
    monkeypatch.setattr(bit_zying_caiji, "_enrich_product_records", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bit_zying_caiji, "_attach_zying_category", lambda *_args: None)
    monkeypatch.setattr(bit_zying_caiji, "_attach_product_developer", lambda *_args: None)
    monkeypatch.setattr(bit_zying_caiji, "_finalize_zying_listing_snapshot", lambda *_args: None)


def test_api_collection_crosses_10000_products_and_obeys_requested_count(monkeypatch):
    pages_requested = []
    written = []
    page_size = 500
    monkeypatch.setattr(bit_zying_caiji, "ZYING_API_PAGE_SIZE", page_size)
    monkeypatch.setattr(bit_zying_caiji, "DEFAULT_ZYING_PAGE_COUNT", 1)

    def api_post(_session, _token, command, payload):
        assert command == "sale.stat"
        pages_requested.append(payload["page"])
        start_id = (payload["page"] - 1) * page_size + 1
        return {"list": {"data": [
            {"id": product_id, "title": f"Product {product_id}"}
            for product_id in range(start_id, start_id + page_size)
        ]}}

    _prepare_offline_collector(monkeypatch, api_post)
    result = bit_zying_caiji.collect_zying_products_api(
        auth_token="offline-count-token", max_items=10001,
        existing_product_id_reader=lambda _ids: set(),
        product_writer=lambda rows: written.extend(rows) or len(rows),
        product_mirror_writer=lambda _rows: {}, return_summary=True,
    )

    assert pages_requested == list(range(1, 22))
    assert len(written) == 10001
    assert written[-1]["product_id"] == "10001"
    assert result["selected_count"] == result["inserted_count"] == 10001


def test_api_count_collection_scans_beyond_old_cutoff_for_cursor(monkeypatch):
    pages_requested = []
    written = []

    def api_post(_session, _token, command, payload):
        assert command == "sale.stat"
        page = payload["page"]
        pages_requested.append(page)
        return {"list": {"data": [{"id": page, "title": f"Product {page}"}]}}

    _prepare_offline_collector(monkeypatch, api_post)
    result = bit_zying_caiji.collect_zying_products_api(
        auth_token="offline-cursor-token", start_page=10000,
        start_product_id="10001", max_items=2,
        existing_product_id_reader=lambda _ids: set(),
        product_writer=lambda rows: written.extend(rows) or len(rows),
        product_mirror_writer=lambda _rows: {}, return_summary=True,
    )

    assert pages_requested == [10000, 10001, 10002]
    assert [row["product_id"] for row in written] == ["10001", "10002"]
    assert result["selected_count"] == 2


def test_api_count_collection_stops_when_products_are_exhausted(monkeypatch):
    pages_requested = []

    def api_post(_session, _token, _command, payload):
        page = payload["page"]
        pages_requested.append(page)
        return {"list": {"data": [{"id": 1}] if page == 1 else []}}

    _prepare_offline_collector(monkeypatch, api_post)
    result = bit_zying_caiji.collect_zying_products_api(
        auth_token="offline-empty-token", max_items=10001,
        existing_product_id_reader=lambda _ids: set(),
        product_writer=lambda rows: len(rows),
        product_mirror_writer=lambda _rows: {}, return_summary=True,
    )

    assert pages_requested == [1, 2]
    assert result["selected_count"] == result["inserted_count"] == 1


def test_api_count_collection_stops_at_reported_end_when_cursor_is_missing(monkeypatch):
    pages_requested = []
    monkeypatch.setattr(bit_zying_caiji, "ZYING_API_PAGE_SIZE", 1)

    def api_post(_session, _token, _command, payload):
        page = payload["page"]
        pages_requested.append(page)
        assert page <= 2, "Do not request beyond the reported final page"
        return {"list": {"data": [{"id": page}], "maxcount": "2"}}

    _prepare_offline_collector(monkeypatch, api_post)
    with pytest.raises(ValueError, match="未找到起始产品编号 999"):
        bit_zying_caiji.collect_zying_products_api(
            auth_token="offline-missing-cursor-token", start_product_id="999",
            max_items=10001, existing_product_id_reader=lambda _ids: set(),
            product_writer=lambda rows: len(rows),
            product_mirror_writer=lambda _rows: {},
        )

    assert pages_requested == [1, 2]


def test_api_count_collection_ignores_zero_total_with_nonempty_products(monkeypatch):
    pages_requested = []
    monkeypatch.setattr(bit_zying_caiji, "ZYING_API_PAGE_SIZE", 1)

    def api_post(_session, _token, _command, payload):
        page = payload["page"]
        pages_requested.append(page)
        return {"list": {"data": [{"id": page}], "maxcount": 0}}

    _prepare_offline_collector(monkeypatch, api_post)
    result = bit_zying_caiji.collect_zying_products_api(
        auth_token="offline-zero-total-token", max_items=2,
        existing_product_id_reader=lambda _ids: set(),
        product_writer=lambda rows: len(rows),
        product_mirror_writer=lambda _rows: {}, return_summary=True,
    )

    assert pages_requested == [1, 2]
    assert result["inserted_count"] == 2


def test_api_count_collection_rejects_repeated_pages_with_missing_cursor(monkeypatch):
    pages_requested = []

    def api_post(_session, _token, _command, payload):
        page = payload["page"]
        pages_requested.append(page)
        assert page <= 2, "Repeated list pages must end the task"
        return {"list": {"data": [{"id": 1}, {"id": 2}]}}

    _prepare_offline_collector(monkeypatch, api_post)
    with pytest.raises(RuntimeError, match="分页异常.*重复返回"):
        bit_zying_caiji.collect_zying_products_api(
            auth_token="offline-repeated-page-token", start_product_id="999",
            max_items=10001, existing_product_id_reader=lambda _ids: set(),
            product_writer=lambda rows: len(rows),
            product_mirror_writer=lambda _rows: {},
        )

    assert pages_requested == [1, 2]
