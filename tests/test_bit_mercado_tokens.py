from urllib.parse import parse_qs, urlparse

import pytest

from bit import bit_db_api, bit_interface, bit_mysql, mercado_tokens


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = str(payload)

    def json(self):
        return self.payload


class OAuthSession:
    def __init__(self, token_payload=None, profile_payload=None):
        self.token_payload = token_payload or {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "token_type": "Bearer",
            "expires_in": 21600,
            "scope": "offline_access read write",
            "user_id": 123456,
        }
        self.profile_payload = profile_payload or {
            "id": 123456,
            "nickname": "SELLER_TEST",
            "site_id": "CBT",
            "email": "seller@example.com",
        }
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(dict(self.token_payload))

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return FakeResponse(dict(self.profile_payload))


@pytest.fixture
def oauth_env(monkeypatch):
    monkeypatch.setenv("MELI_CLIENT_ID", "app-123")
    monkeypatch.setenv("MELI_CLIENT_SECRET", "app-secret")
    monkeypatch.setenv("MELI_REDIRECT_URI", "https://console.test/zs")
    monkeypatch.setenv(
        "MELI_AUTHORIZATION_URL",
        "https://global-selling.mercadolibre.com/authorization",
    )


def test_authorization_link_uses_server_configuration_without_secret(oauth_env):
    info = mercado_tokens.authorization_info()
    query = parse_qs(urlparse(info["authorization_url"]).query)

    assert info["configured"] is True
    assert query == {
        "response_type": ["code"],
        "client_id": ["app-123"],
        "redirect_uri": ["https://console.test/zs"],
    }
    assert "app-secret" not in str(info)


def test_authorization_link_only_requires_public_client_id(monkeypatch):
    monkeypatch.setenv("MELI_CLIENT_ID", "app-123")
    monkeypatch.delenv("MELI_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("MERCADO_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(mercado_tokens, "_legacy_oauth_credentials", lambda: {})

    info = mercado_tokens.authorization_info()

    assert info["configured"] is True
    assert "client_id=app-123" in info["authorization_url"]


def test_token_summary_defensively_removes_both_secrets():
    summary = bit_mysql._mercado_token_summary(
        {
            "id": 1,
            "display_name": "店铺",
            "access_token": "must-not-leak",
            "refresh_token": "must-not-leak-either",
            "email": "seller@example.com",
            "enabled": 0,
            "has_refresh_token": 1,
        }
    )

    assert "access_token" not in summary
    assert "refresh_token" not in summary
    assert summary["has_refresh_token"] is True
    assert summary["email"] == "seller@example.com"
    assert summary["enabled"] is False


def test_server_side_token_access_rejects_disabled_store(monkeypatch):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _params=None):
            self.sql = sql

        def fetchone(self):
            if "SHOW COLUMNS" in self.sql:
                return {"Field": "enabled"}
            if "SELECT *" in self.sql:
                return {"id": 8, "enabled": 0, "access_token": "secret"}
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            return None

    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: Connection())

    with pytest.raises(ValueError, match="店铺已关闭"):
        bit_mysql.get_mercado_store_token(8)


def test_site_setting_rows_expose_appeal_and_visit_switches():
    class Cursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            return [
                {
                    "token_id": 9,
                    "site_id": "MLM",
                    "appeal_enabled": 1,
                    "visit_stats_enabled": 0,
                }
            ]

    rows = bit_mysql._mercado_store_site_setting_rows(Cursor(), 9)
    mexico = next(row for row in rows if row["site_id"] == "MLM")
    brazil = next(row for row in rows if row["site_id"] == "MLB")

    assert mexico["appeal_enabled"] is True
    assert mexico["visit_stats_enabled"] is False
    assert brazil["appeal_enabled"] is False
    assert brazil["visit_stats_enabled"] is False


def test_default_oauth_client_does_not_inherit_system_proxy(monkeypatch):
    http = OAuthSession()
    http.trust_env = True
    monkeypatch.setattr(mercado_tokens.requests, "Session", lambda: http)

    token_data = mercado_tokens._request_token({"grant_type": "test"})

    assert token_data["access_token"] == "access-secret"
    assert http.trust_env is False


def test_oauth_proxy_failure_returns_actionable_error():
    class ProxyFailureSession:
        def post(self, *_args, **_kwargs):
            raise mercado_tokens.requests.exceptions.ProxyError("proxy refused")

    with pytest.raises(
        mercado_tokens.MercadoTokenError,
        match="无法连接 Mercado Libre Token 接口",
    ):
        mercado_tokens._request_token(
            {"grant_type": "test"},
            http=ProxyFailureSession(),
        )


@pytest.mark.parametrize(
    "value",
    [
        "TG-fresh-123",
        "https://console.test/zs?code=TG-fresh-123",
    ],
)
def test_extract_authorization_code_accepts_tg_or_callback(value):
    assert mercado_tokens.extract_authorization_code(value) == "TG-fresh-123"


def test_exchange_identifies_store_and_passes_tokens_only_to_database(oauth_env):
    http = OAuthSession()
    captured = {}

    def upsert(record):
        captured.update(record)
        return {
            "id": 7,
            "display_name": record["display_name"],
            "meli_user_id": record["meli_user_id"],
            "status": "active",
        }

    result = mercado_tokens.exchange_and_save(
        "  自定义店铺  ",
        "TG-once-123",
        upsert=upsert,
        http=http,
    )

    assert captured["display_name"] == "自定义店铺"
    assert captured["meli_user_id"] == "123456"
    assert captured["nickname"] == "SELLER_TEST"
    assert captured["site_id"] == "CBT"
    assert captured["email"] == "seller@example.com"
    assert captured["access_token"] == "access-secret"
    assert captured["refresh_token"] == "refresh-secret"
    assert result["id"] == 7
    assert "access_token" not in result
    assert "refresh_token" not in result
    assert http.posts[0][1]["data"]["code"] == "TG-once-123"


def test_refresh_rotates_and_saves_replacement_refresh_token(oauth_env):
    http = OAuthSession(
        token_payload={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 21600,
            "user_id": 123456,
        }
    )
    updated = {}
    errors = []

    result = mercado_tokens.refresh_and_save(
        9,
        get_token=lambda token_id: {
            "id": token_id,
            "display_name": "店铺九",
            "refresh_token": "old-refresh",
        },
        update_token=lambda token_id, record: (
            updated.update(token_id=token_id, record=record)
            or {"id": token_id, "status": "active"}
        ),
        record_error=lambda token_id, message: errors.append((token_id, message)),
        http=http,
    )

    assert result == {"id": 9, "status": "active", "warning": ""}
    assert http.posts[0][1]["data"]["refresh_token"] == "old-refresh"
    assert updated["record"]["access_token"] == "new-access"
    assert updated["record"]["refresh_token"] == "new-refresh"
    assert errors == []


def test_refresh_still_saves_rotated_token_when_profile_lookup_fails(oauth_env):
    class ProfileFailureSession(OAuthSession):
        def get(self, url, **kwargs):
            self.gets.append((url, kwargs))
            return FakeResponse({"message": "temporary unavailable"}, status_code=503)

    http = ProfileFailureSession(
        token_payload={
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 21600,
            "user_id": 123456,
        }
    )
    updated = {}

    result = mercado_tokens.refresh_and_save(
        10,
        get_token=lambda _token_id: {
            "id": 10,
            "display_name": "店铺十",
            "meli_user_id": "123456",
            "nickname": "KNOWN_NAME",
            "site_id": "CBT",
            "email": "known@example.com",
            "refresh_token": "old-refresh",
        },
        update_token=lambda token_id, record: (
            updated.update(token_id=token_id, record=record) or {"id": token_id}
        ),
        http=http,
    )

    assert updated["record"]["refresh_token"] == "rotated-refresh"
    assert updated["record"]["nickname"] == "KNOWN_NAME"
    assert updated["record"]["email"] == "known@example.com"
    assert "读取授权店铺失败" in updated["record"]["last_error"]
    assert result["warning"] == updated["record"]["last_error"]


def test_sync_missing_store_emails_reads_profile_and_persists_email():
    http = OAuthSession()
    updates = []

    result = mercado_tokens.sync_missing_store_emails(
        list_tokens=lambda: {
            "rows": [{"id": 12, "display_name": "旧店铺", "email": ""}]
        },
        get_token=lambda token_id: {
            "id": token_id,
            "access_token": "existing-access",
            "refresh_token": "existing-refresh",
            "expires_at": "2026-09-03 18:00:00",
        },
        update_email=lambda token_id, email: updates.append((token_id, email)),
        refresh_token=lambda _token_id: pytest.fail("有效 Token 不应被刷新"),
        http=http,
        now=mercado_tokens.datetime(2026, 9, 3, 12, 0, 0),
    )

    assert updates == [(12, "seller@example.com")]
    assert result == {
        "checked": 1,
        "missing": 1,
        "synced": 1,
        "unavailable": 0,
        "failed": 0,
        "failures": [],
    }


def test_sync_missing_store_emails_skips_disabled_stores():
    result = mercado_tokens.sync_missing_store_emails(
        list_tokens=lambda: {
            "rows": [{"id": 13, "display_name": "关闭店铺", "email": "", "enabled": False}]
        },
        get_token=lambda _token_id: pytest.fail("关闭店铺不应读取 Token"),
        update_email=lambda *_args: pytest.fail("关闭店铺不应写入邮箱"),
        refresh_token=lambda _token_id: pytest.fail("关闭店铺不应刷新 Token"),
    )

    assert result["checked"] == 1
    assert result["missing"] == 0
    assert result["synced"] == 0


def test_auto_refresh_renews_only_due_tokens_and_defers_recent_failures():
    now = mercado_tokens.datetime(2026, 8, 30, 12, 0, 0)
    refreshed = []

    result = mercado_tokens.auto_refresh_due_store_tokens(
        list_tokens=lambda: {
            "rows": [
                {
                    "id": 1,
                    "display_name": "即将过期",
                    "expires_at": "2026-08-30 12:30:00",
                    "has_refresh_token": True,
                },
                {
                    "id": 2,
                    "display_name": "仍然有效",
                    "expires_at": "2026-08-30 14:00:00",
                    "has_refresh_token": True,
                },
                {
                    "id": 3,
                    "display_name": "无法续期",
                    "expires_at": "2026-08-30 11:00:00",
                    "has_refresh_token": False,
                },
                {
                    "id": 4,
                    "display_name": "等待重试",
                    "expires_at": "2026-08-30 11:00:00",
                    "has_refresh_token": True,
                    "last_error": "temporary failure",
                    "updated_at": "2026-08-30 11:55:00",
                },
                {
                    "id": 5,
                    "display_name": "已关闭",
                    "enabled": False,
                    "expires_at": "2026-08-30 11:00:00",
                    "has_refresh_token": True,
                },
            ]
        },
        refresh_token=lambda token_id: refreshed.append(token_id),
        now=now,
        refresh_before_minutes=60,
        retry_minutes=15,
    )

    assert refreshed == [1]
    assert result == {
        "checked": 5,
        "due": 2,
        "refreshed": 1,
        "failed": 0,
        "retry_deferred": 1,
        "failures": [],
    }


def _logged_in_client():
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {
            "id": 1,
            "username": "tester",
            "display_name": "测试员",
        }
    return client


def test_browser_routes_return_metadata_and_accept_management_actions(monkeypatch):
    summary = {
        "id": 2,
        "display_name": "店铺二",
        "meli_user_id": "9988",
        "status": "active",
        "enabled": True,
        "has_refresh_token": True,
        "site_settings": [],
    }
    calls = []
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "list_mercado_store_tokens",
        lambda: {"total": 1, "rows": [summary]},
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "exchange_mercado_store_token",
        lambda name, code: calls.append(("exchange", name, code)) or summary,
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "start_store_link_sync",
        lambda token_ids: calls.append(("sync-links", token_ids))
        or {"started": True, "state": {"task_id": "task-new-store"}},
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "refresh_mercado_store_token",
        lambda token_id: calls.append(("refresh", token_id)) or summary,
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "rename_mercado_store_token",
        lambda token_id, name: calls.append(("rename", token_id, name)) or summary,
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "set_mercado_store_token_enabled",
        lambda token_id, enabled: calls.append(("enabled", token_id, enabled)) or {
            **summary,
            "enabled": enabled,
        },
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "delete_mercado_store_token",
        lambda token_id: calls.append(("delete", token_id)) or 1,
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "list_mercado_store_site_settings",
        lambda token_id: calls.append(("list-sites", token_id))
        or {"token_id": token_id, "rows": []},
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "update_mercado_store_site_settings",
        lambda token_id, settings: calls.append(("save-sites", token_id, settings))
        or {"token_id": token_id, "rows": settings},
    )
    client = _logged_in_client()

    list_response = client.get("/api/mercado-tokens")
    exchange_response = client.post(
        "/api/mercado-tokens/exchange",
        json={"display_name": "店铺二", "code": "TG-code"},
    )
    refresh_response = client.post("/api/mercado-tokens/2/refresh", json={})
    rename_response = client.patch(
        "/api/mercado-tokens/2", json={"display_name": "新名字"}
    )
    disable_response = client.patch(
        "/api/mercado-tokens/2", json={"enabled": False}
    )
    site_list_response = client.get("/api/mercado-tokens/2/site-settings")
    site_save_response = client.put(
        "/api/mercado-tokens/2/site-settings",
        json={
            "settings": [{
                "site_id": "MLM",
                "discount_rate": 95,
                "salesperson": "张三",
                "group_name": "精品组",
            }]
        },
    )
    delete_response = client.delete("/api/mercado-tokens/2")

    assert list_response.status_code == 200
    assert "access_token" not in list_response.get_data(as_text=True)
    assert exchange_response.status_code == 200
    assert exchange_response.get_json()["data"]["auto_link_sync"]["queued"] is True
    assert "自动拉取全部链接" in exchange_response.get_json()["message"]
    assert refresh_response.status_code == 200
    assert rename_response.status_code == 200
    assert disable_response.status_code == 200
    assert disable_response.get_json()["data"]["enabled"] is False
    assert site_list_response.status_code == 200
    assert site_save_response.status_code == 200
    assert delete_response.status_code == 200
    assert calls == [
        ("exchange", "店铺二", "TG-code"),
        ("sync-links", [2]),
        ("refresh", 2),
        ("rename", 2, "新名字"),
        ("enabled", 2, False),
        ("list-sites", 2),
        ("save-sites", 2, [{
            "site_id": "MLM",
            "discount_rate": 95,
            "salesperson": "张三",
            "group_name": "精品组",
        }]),
        ("delete", 2),
    ]


def test_database_api_client_uses_remote_token_endpoints(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {},
    )

    bit_db_api.list_mercado_store_tokens()
    bit_db_api.list_mercado_store_site_settings(3)
    bit_db_api.update_mercado_store_site_settings(3, [{"site_id": "MLB"}])
    bit_db_api.exchange_mercado_store_token("店铺", "TG-code")
    bit_db_api.refresh_mercado_store_token(3)
    bit_db_api.rename_mercado_store_token(3, "改名")
    bit_db_api.set_mercado_store_token_enabled(3, False)
    bit_db_api.delete_mercado_store_token(3)

    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/api/db/mercado-tokens"),
        ("GET", "/api/db/mercado-tokens/3/site-settings"),
        ("PUT", "/api/db/mercado-tokens/3/site-settings"),
        ("POST", "/api/db/mercado-tokens/exchange"),
        ("POST", "/api/db/mercado-tokens/3/refresh"),
        ("PATCH", "/api/db/mercado-tokens/3"),
        ("PATCH", "/api/db/mercado-tokens/3"),
        ("DELETE", "/api/db/mercado-tokens/3"),
    ]


def test_token_list_falls_back_to_direct_mysql_when_cloud_route_is_old(monkeypatch):
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "数据库接口返回非 JSON：http://host/api/db/mercado-tokens，状态码：404"
            )
        ),
    )
    monkeypatch.setattr(
        bit_db_api,
        "_local_call",
        lambda function_name, *args, **kwargs: {
            "source": function_name,
            "total": 0,
            "rows": [],
        },
    )

    result = bit_db_api.list_mercado_store_tokens()

    assert result == {
        "source": "list_mercado_store_tokens",
        "total": 0,
        "rows": [],
    }


def test_oauth_callback_displays_one_time_code_without_login():
    client = bit_interface.app.test_client()
    response = client.get("/zs?code=TG-visible-123")

    assert response.status_code == 200
    assert "TG-visible-123" in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


def test_console_template_contains_store_token_module():
    client = _logged_in_client()
    response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="tab-store-tokens"' in body
    assert 'id="mercado-token-code"' in body
    assert 'id="mercado-authorization-url"' in body
    assert 'id="mercado-site-settings-dialog"' in body
    assert 'id="mercado-site-settings-body"' in body
    assert 'id="mercado-token-select-all"' in body
    assert 'id="mercado-token-bulk-settings"' in body
    assert 'id="mercado-token-salesperson-filter"' in body
    assert 'id="mercado-token-group-filter"' in body
    assert "filteredMercadoStoreTokenRows" in body
    assert "当前筛选条件下暂无授权店铺" in body
    assert "Token 临近到期时后台自动刷新" in body
    assert "<th>邮箱</th>" in body
    assert "<th>店铺开关</th>" in body
    assert 'row.email || "待读取"' in body
    assert "setMercadoStoreEnabled" in body
    assert "自动登录、同步、采集、刊登等业务操作均不执行" in body
    assert "startMercadoTokenStatusPolling" in body
    assert "loadMercadoStoreTokens(true, true)" in body
    assert 'id="mercado-bulk-settings-dialog"' in body
    assert 'id="mercado-bulk-salesperson"' in body
    assert 'id="mercado-bulk-group-name"' in body
    assert 'id="mercado-bulk-group-save"' in body
    assert 'id="mercado-bulk-settings-body"' in body
    assert "折扣比例（%）" in body
    assert 'id="mercado-site-salesperson"' in body
    assert 'id="mercado-site-group-name"' in body
    assert ">店铺授权</button>" in body
    assert 'data-field="salesperson"' not in body
    assert 'data-field="group_name"' not in body
    assert 'requestAccessApi("/api/access/users")' in body
    assert "勾选站点是否参与自动申诉和访问数据统计" in body
    assert 'data-field="appeal_enabled"' in body
    assert 'data-field="visit_stats_enabled"' in body
    assert 'data-field="bulk_appeal_enabled"' in body
    assert 'data-field="bulk_visit_stats_enabled"' in body
    assert "留空则保留每家店铺原分组" in body
    assert "toggleMercadoStoreToken" in body
    assert "saveMercadoBulkSiteSettings" in body
    assert "saveMercadoSiteSettingsRequest" in body
    assert "保存响应中断，正在自动重试" in body
    assert "bulkGroupName || mercadoAccountGroup(account)" in body
    assert "saveMercadoBulkSiteSettings(true)" in body
    assert "discount_rate: existing.discount_rate ?? null" in body
    assert "group_name: groupName" in body
    assert "global-selling.mercadolibre.com/authorization" in body
    assert "Access Token 和 Refresh Token 仅保存在数据库服务端" in body


def test_site_settings_reject_unsupported_site_before_database_call():
    with pytest.raises(ValueError, match="不支持的美客多站点"):
        bit_mysql.upsert_mercado_store_site_settings(
            2,
            [{"site_id": "CBT", "discount_rate": 90}],
        )


def test_site_settings_reject_discount_outside_percentage_range():
    with pytest.raises(ValueError, match="必须在 0 到 100 之间"):
        bit_mysql.upsert_mercado_store_site_settings(
            2,
            [{"site_id": "MLM", "discount_rate": 101}],
        )
