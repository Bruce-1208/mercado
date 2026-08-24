from pathlib import Path

from openpyxl import Workbook

from bit import bit_config
from bit import bit_db_api
from bit import bit_interface
from bit import bit_mysql


def test_split_config_sites_supports_all_existing_separators():
    assert bit_config.split_config_sites("墨西哥，巴西/智利；阿根廷|乌拉圭") == [
        "墨西哥",
        "巴西",
        "智利",
        "阿根廷",
        "乌拉圭",
    ]


def test_config_rows_are_read_from_database_api(monkeypatch):
    monkeypatch.setattr(
        bit_config.bit_db_api,
        "list_bit_browser_configs",
        lambda include_ignored=True: [
            {
                "window_id": "window-1",
                "shop_name": "测试店铺",
                "status": "",
                "sites": "墨西哥，巴西",
                "sequence_no": 8,
                "salesperson": "测试业务员",
                "email": "shop@example.com",
            },
            {
                "window_id": "window-2",
                "shop_name": "忽略店铺",
                "status": "忽略",
                "sites": "墨西哥",
            },
        ],
    )

    rows = bit_config.list_config_rows(include_ignored=False)

    assert rows == [
        (
            "window-1",
            "测试店铺",
            "",
            "墨西哥，巴西",
            "8",
            "测试业务员",
            "shop@example.com",
        )
    ]


def test_single_shop_lookup_falls_back_to_database_list(monkeypatch):
    monkeypatch.setattr(
        bit_config.bit_db_api,
        "get_bit_browser_config",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("数据库接口返回非 JSON，状态码：404")
        ),
    )
    monkeypatch.setattr(
        bit_config.bit_db_api,
        "list_bit_browser_configs",
        lambda include_ignored=True: [
            {
                "window_id": "window-1",
                "shop_name": "四季如春",
                "email": "shop@example.com",
            }
        ],
    )

    assert bit_config.require_shop_config(shop_name="四季如春") == {
        "window_id": "window-1",
        "shop_name": "四季如春",
        "status": "",
        "sites": "",
        "sequence_no": "",
        "salesperson": "",
        "email": "shop@example.com",
    }


def test_config_list_falls_back_to_direct_database_when_cloud_route_is_missing(
    monkeypatch,
):
    monkeypatch.setattr(bit_config.bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_config.bit_db_api,
        "list_bit_browser_configs",
        lambda include_ignored=True: (_ for _ in ()).throw(
            RuntimeError(
                "数据库接口返回非 JSON：http://host/api/db/browser-configs，状态码：404"
            )
        ),
    )
    monkeypatch.setattr(
        bit_mysql,
        "list_bit_browser_configs",
        lambda include_ignored=True: [
            {
                "window_id": "window-1",
                "shop_name": "四季如春",
                "email": "shop@example.com",
            }
        ],
    )

    assert bit_config.list_shop_configs(include_ignored=False)[0]["shop_name"] == "四季如春"


def test_excel_migration_uses_headers_and_writes_database(monkeypatch, tmp_path):
    path = tmp_path / "比特配置文件.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["站点", "邮箱", "账号名", "窗口ID", "状态", "业务员", "序号"])
    sheet.append(["巴西，墨西哥", "shop@example.com", "测试店铺", "window-1", "", "Lucy", 12])
    workbook.save(path)
    captured = {}

    def fake_upsert(records, replace=False):
        captured["records"] = records
        captured["replace"] = replace
        return {"count": len(records), "replaced": replace}

    monkeypatch.setattr(bit_config.bit_db_api, "upsert_bit_browser_configs", fake_upsert)

    result = bit_config.import_config_excel(path, replace=True)

    assert result["count"] == 1
    assert captured["replace"] is True
    assert captured["records"] == [
        {
            "window_id": "window-1",
            "shop_name": "测试店铺",
            "status": "",
            "sites": "巴西，墨西哥",
            "sequence_no": "12",
            "salesperson": "Lucy",
            "email": "shop@example.com",
        }
    ]


def test_db_api_browser_config_uses_remote_endpoints(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)) or [],
    )

    bit_db_api.list_bit_browser_configs(include_ignored=False)
    bit_db_api.get_bit_browser_config(shop_name="测试店铺")
    bit_db_api.upsert_bit_browser_configs([{"window_id": "1", "shop_name": "店铺"}], replace=True)
    bit_db_api.create_bit_browser_config({"window_id": "2", "shop_name": "新增店铺"})
    bit_db_api.update_bit_browser_config(7, {"window_id": "3", "shop_name": "修改店铺"})
    bit_db_api.delete_bit_browser_config(7)

    assert calls[0][0:2] == ("GET", "/api/db/browser-configs")
    assert calls[0][2]["params"]["include_ignored"] == "0"
    assert calls[1][0:2] == ("GET", "/api/db/browser-configs/lookup")
    assert calls[2][0:2] == ("POST", "/api/db/browser-configs/bulk")
    assert calls[2][2]["json"]["replace"] is True
    assert calls[3][0:2] == ("POST", "/api/db/browser-configs")
    assert calls[4][0:2] == ("PUT", "/api/db/browser-configs/7")
    assert calls[5][0:2] == ("DELETE", "/api/db/browser-configs/7")


def test_browser_config_database_routes(monkeypatch):
    monkeypatch.setattr(bit_interface, "reject_db_api_client_mode", lambda: None)
    monkeypatch.setattr(
        bit_interface,
        "db_list_bit_browser_configs",
        lambda include_ignored=True: [{"window_id": "window-1", "shop_name": "测试店铺"}],
    )
    monkeypatch.setattr(
        bit_interface,
        "db_get_bit_browser_config",
        lambda shop_name, window_id, include_ignored=True: {
            "window_id": window_id or "window-1",
            "shop_name": shop_name or "测试店铺",
        },
    )
    captured = {}
    monkeypatch.setattr(
        bit_interface,
        "db_upsert_bit_browser_configs",
        lambda records, replace=False: captured.update(records=records, replace=replace) or {
            "count": len(records)
        },
    )
    monkeypatch.setattr(
        bit_interface,
        "db_create_bit_browser_config",
        lambda record: captured.update(created=record) or {"id": 7},
    )
    monkeypatch.setattr(
        bit_interface,
        "db_update_bit_browser_config",
        lambda config_id, record: captured.update(updated=(config_id, record)) or {"id": config_id},
    )
    monkeypatch.setattr(
        bit_interface,
        "db_delete_bit_browser_config",
        lambda config_id: captured.update(deleted=config_id) or {"id": config_id},
    )
    client = bit_interface.app.test_client()

    list_response = client.get("/api/db/browser-configs?include_ignored=0")
    lookup_response = client.get("/api/db/browser-configs/lookup?shop_name=测试店铺")
    write_response = client.post(
        "/api/db/browser-configs/bulk",
        json={
            "records": [{"window_id": "window-1", "shop_name": "测试店铺"}],
            "replace": True,
        },
    )
    create_response = client.post(
        "/api/db/browser-configs",
        json={"window_id": "window-2", "shop_name": "新增店铺"},
    )
    update_response = client.put(
        "/api/db/browser-configs/7",
        json={"window_id": "window-3", "shop_name": "修改店铺"},
    )
    delete_response = client.delete("/api/db/browser-configs/7")

    assert list_response.status_code == 200
    assert lookup_response.get_json()["data"]["shop_name"] == "测试店铺"
    assert write_response.status_code == 200
    assert captured["replace"] is True
    assert create_response.get_json()["data"] == {"id": 7}
    assert captured["created"]["shop_name"] == "新增店铺"
    assert update_response.status_code == 200
    assert captured["updated"][0] == 7
    assert delete_response.status_code == 200
    assert captured["deleted"] == 7


def test_runtime_modules_do_not_read_bit_config_excel_directly():
    project_root = Path(bit_config.__file__).resolve().parent.parent
    runtime_files = list((project_root / "bit").glob("*.py")) + list(
        (project_root / "bit_playwright").glob("*.py")
    )
    offenders = []
    for path in runtime_files:
        if path.name == "bit_config.py":
            continue
        if "比特配置文件.xlsx" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(project_root)))

    assert offenders == []


def test_existing_zying_mark_function_still_updates_rows(monkeypatch):
    calls = []

    class Cursor:
        rowcount = 2

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def executemany(self, sql, params):
            calls.append((sql, params))

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: Connection())
    monkeypatch.setattr(bit_mysql, "_ensure_zying_product_table", lambda cursor: None)

    assert bit_mysql.mark_zying_products_suspected([3, "4", 3]) == 2
    assert calls[0][1] == [(3,), (4,)]
    assert "commit" in calls
