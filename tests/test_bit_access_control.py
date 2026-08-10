import inspect
from io import BytesIO
from pathlib import Path

from bit import bit_db_api, bit_interface


def _access_user(*permissions):
    return {
        "id": 9,
        "username": "permission-tester",
        "display_name": "权限测试员",
        "role_key": "custom",
        "role_name": "测试角色",
        "permissions": list(permissions),
        "access_version": 1,
    }


def test_permission_catalog_and_role_dependencies():
    catalog = bit_interface.workbench_permission_catalog()
    permission_keys = {
        permission["key"]
        for group in catalog
        for permission in group["permissions"]
    }

    assert "appeal.view" in permission_keys
    assert "appeal.execute" in permission_keys
    assert "order_analysis.view" in permission_keys
    assert "order_analysis.execute" in permission_keys
    assert "access.manage" in permission_keys
    assert bit_interface._validate_workbench_permissions(["appeal.execute"]) == [
        "appeal.execute",
        "appeal.view",
    ]
    assert bit_interface._validate_workbench_permissions(["access.manage"]) == [
        "access.manage",
        "access.view",
    ]


def test_session_user_contains_role_and_permissions():
    user = bit_interface.build_workbench_session_user(
        {
            "id": 1,
            "username": "viewer",
            "display_name": "只读账号",
            "email": "viewer@example.com",
            "department": "运营部",
            "role_key": "viewer",
            "role_name": "只读人员",
            "permissions_json": '["reputation.view"]',
        }
    )

    assert user["role_key"] == "viewer"
    assert user["role_name"] == "只读人员"
    assert user["permissions"] == ["reputation.view"]
    assert user["access_version"] == 1


def test_super_admin_session_always_gets_all_permissions():
    user = bit_interface.build_workbench_session_user(
        {
            "id": 1,
            "username": "admin",
            "role_key": "super_admin",
            "role_name": "超级管理员",
            "permissions_json": "[]",
        }
    )

    assert user["permissions"] == ["*"]
    assert bit_interface.workbench_user_has_permission(user, "access.manage") is True


def test_business_write_api_is_denied_for_view_only_role(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _access_user("reputation.view"),
    )
    client = bit_interface.app.test_client()

    response = client.post("/api/reputation/collect", json={})

    assert response.status_code == 403
    assert "没有执行该操作的权限" in response.get_json()["message"]
    assert response.get_json()["required_permissions"] == ["reputation.execute"]


def test_order_analysis_import_requires_execute_permission(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _access_user("order_analysis.view"),
    )
    client = bit_interface.app.test_client()

    response = client.post(
        "/api/order-analysis/import",
        data={"files": (BytesIO(b"excel"), "orders.xlsx")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 403
    assert response.get_json()["required_permissions"] == ["order_analysis.execute"]


def test_order_analysis_import_uses_update_orders_module(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _access_user("order_analysis.view", "order_analysis.execute"),
    )
    monkeypatch.setattr(
        bit_interface.bit_update_orders,
        "update_order_sources",
        lambda sources, insert_func: calls.append((list(sources), insert_func)) or {
            "files_discovered": 1,
            "files_processed": 1,
            "raw_rows": 3,
            "unique_orders": 2,
            "duplicate_rows": 1,
            "blank_order_ids": 0,
            "errors": [],
            "imported_orders": 2,
        },
    )
    client = bit_interface.app.test_client()

    response = client.post(
        "/api/order-analysis/import",
        data={"files": (BytesIO(b"excel"), "folder/orders.xlsx")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["imported_orders"] == 2
    assert calls[0][0][0][0] == "folder/orders.xlsx"
    assert calls[0][1] is bit_interface.db_insert_orders


def test_access_view_can_list_but_cannot_modify_roles(monkeypatch):
    backend_calls = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _access_user("access.view"),
    )
    monkeypatch.setattr(
        bit_interface,
        "_workbench_backend",
        lambda name, *args: backend_calls.append((name, args)) or [],
    )
    client = bit_interface.app.test_client()

    list_response = client.get("/api/access/roles")
    create_response = client.post(
        "/api/access/roles",
        json={"role_name": "运营", "permissions": []},
    )

    assert list_response.status_code == 200
    assert backend_calls == [("list_workbench_roles", ())]
    assert create_response.status_code == 403


def test_access_manager_can_create_role(monkeypatch):
    backend_calls = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _access_user("access.view", "access.manage"),
    )
    monkeypatch.setattr(
        bit_interface,
        "_workbench_backend",
        lambda name, *args: backend_calls.append((name, args)) or "role-new",
    )
    client = bit_interface.app.test_client()

    response = client.post(
        "/api/access/roles",
        json={"role_name": "运营", "permissions": ["appeal.execute"]},
    )

    assert response.status_code == 200
    assert response.get_json()["data"] == "role-new"
    assert backend_calls == [
        (
            "create_workbench_role",
            ({"role_name": "运营", "permissions": ["appeal.execute"]},),
        )
    ]


def test_access_view_can_list_but_cannot_modify_shop_configs(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _access_user("access.view"),
    )
    monkeypatch.setattr(
        bit_interface,
        "db_list_bit_browser_configs",
        lambda include_ignored=True: calls.append(include_ignored) or [{"id": 1}],
    )
    client = bit_interface.app.test_client()

    list_response = client.get("/api/access/browser-configs")
    create_response = client.post(
        "/api/access/browser-configs",
        json={"window_id": "window-1", "shop_name": "店铺"},
    )

    assert list_response.status_code == 200
    assert list_response.get_json()["data"] == [{"id": 1}]
    assert calls == [True]
    assert create_response.status_code == 403


def test_access_manager_can_create_update_and_delete_shop_configs(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _access_user("access.view", "access.manage"),
    )
    monkeypatch.setattr(
        bit_interface,
        "db_create_bit_browser_config",
        lambda data: calls.append(("create", data)) or {"id": 5},
    )
    monkeypatch.setattr(
        bit_interface,
        "db_update_bit_browser_config",
        lambda config_id, data: calls.append(("update", config_id, data)) or {"id": config_id},
    )
    monkeypatch.setattr(
        bit_interface,
        "db_delete_bit_browser_config",
        lambda config_id: calls.append(("delete", config_id)) or {"id": config_id},
    )
    client = bit_interface.app.test_client()
    record = {"window_id": "window-1", "shop_name": "店铺"}

    create_response = client.post("/api/access/browser-configs", json=record)
    update_response = client.put("/api/access/browser-configs/5", json=record)
    delete_response = client.delete("/api/access/browser-configs/5")

    assert create_response.get_json()["data"] == {"id": 5}
    assert update_response.status_code == 200
    assert delete_response.status_code == 200
    assert calls == [
        ("create", record),
        ("update", 5, record),
        ("delete", 5),
    ]


def test_workbench_schema_migrates_roles_and_existing_users():
    source = inspect.getsource(bit_interface.ensure_workbench_user_table)

    assert "CREATE TABLE IF NOT EXISTS `workbench_roles`" in source
    assert "`role_key` VARCHAR(64)" in source
    assert "SET `role_key` = 'super_admin'" in source
    assert "WORKBENCH_DEFAULT_ROLES" in source


def test_access_management_page_contains_role_user_and_permission_controls():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'data-tab="access" data-permission="access.view"' in template
    assert 'id="tab-access"' in template
    assert 'id="access-role-form"' in template
    assert 'id="access-user-form"' in template
    assert 'id="access-permission-grid"' in template
    assert 'id="access-shop-form"' in template
    assert 'id="access-shop-body"' in template
    assert 'id="access-shop-search"' in template
    assert 'requestAccessApi("/api/access/browser-configs")' in template
    assert "currentWorkbenchUser" in template
    assert "applyWorkbenchPermissions" in template
    assert '"reputation.execute"' in template


def test_db_api_proxies_access_management_requests(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)) or [],
    )

    assert bit_db_api.list_workbench_roles() == []
    bit_db_api.update_workbench_user(12, {"role_key": "viewer"})

    assert calls[0][:2] == ("GET", "/api/db/workbench/roles")
    assert calls[1][:2] == ("PUT", "/api/db/workbench/users/12")
    assert calls[1][2]["json"] == {"role_key": "viewer"}
