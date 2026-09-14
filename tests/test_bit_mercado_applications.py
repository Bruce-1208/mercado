from urllib.parse import parse_qs, urlparse

from bit import bit_db_api, bit_interface, bit_mysql, mercado_tokens
from erp import mercadolibre_batch_publish as batch_publish


class _Response:
    ok = True
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return dict(self.payload)


class _OAuthSession:
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response({
            "access_token": "access",
            "refresh_token": "refresh-new",
            "expires_in": 21600,
            "user_id": 99,
        })

    def get(self, _url, **_kwargs):
        return _Response({"id": 99, "nickname": "SHOP", "site_id": "CBT"})


def _application():
    return {
        "id": 7,
        "name": "刊登应用",
        "client_id": "selected-app-id",
        "client_secret": "selected-secret",
        "redirect_uri": "https://console.test/zs",
        "authorization_base_url": "https://global-selling.mercadolibre.com/authorization",
        "enabled": True,
    }


def test_application_authorization_link_uses_selected_app_without_leaking_secret():
    info = mercado_tokens.authorization_info(_application())
    query = parse_qs(urlparse(info["authorization_url"]).query)

    assert query["client_id"] == ["selected-app-id"]
    assert query["redirect_uri"] == ["https://console.test/zs"]
    assert info["application_id"] == 7
    assert "selected-secret" not in str(info)


def test_exchange_and_refresh_use_store_application_credentials():
    exchange_http = _OAuthSession()
    saved = {}
    mercado_tokens.exchange_and_save(
        "店铺",
        "TG-code",
        application_id=7,
        get_application=lambda _application_id: _application(),
        upsert=lambda record: saved.update(record) or {"id": 3},
        http=exchange_http,
    )

    assert exchange_http.posts[0][1]["data"]["client_id"] == "selected-app-id"
    assert exchange_http.posts[0][1]["data"]["client_secret"] == "selected-secret"
    assert saved["application_id"] == 7

    refresh_http = _OAuthSession()
    refreshed = {}
    mercado_tokens.refresh_and_save(
        3,
        get_token=lambda _token_id: {
            "id": 3,
            "display_name": "店铺",
            "application_id": 7,
            "refresh_token": "refresh-old",
        },
        get_application=lambda _application_id: _application(),
        update_token=lambda _token_id, record: refreshed.update(record) or {"id": 3},
        http=refresh_http,
    )

    form = refresh_http.posts[0][1]["data"]
    assert form["client_id"] == "selected-app-id"
    assert form["client_secret"] == "selected-secret"
    assert form["refresh_token"] == "refresh-old"
    assert refreshed["application_id"] == 7


def test_reauthorize_application_rejects_a_different_store():
    http = _OAuthSession()

    try:
        mercado_tokens.reauthorize_with_application(
            3,
            "TG-code",
            7,
            get_token=lambda _token_id, include_disabled=False: {
                "id": 3,
                "display_name": "原店铺",
                "meli_user_id": "100",
            },
            get_application=lambda _application_id: _application(),
            update_token=lambda *_args: None,
            http=http,
        )
    except mercado_tokens.MercadoTokenError as exc:
        assert "不是当前店铺" in str(exc)
    else:
        raise AssertionError("不同店铺的授权不应覆盖原店铺")


def test_application_summary_never_returns_client_secret():
    summary = bit_mysql._mercado_application_summary(_application())

    assert summary["has_client_secret"] is True
    assert "client_secret" not in summary


def test_publish_client_loads_credentials_from_store_application(monkeypatch):
    monkeypatch.setattr(
        batch_publish,
        "_token_record",
        lambda _token_id: {
            "access_token": "access",
            "client_id": "old-app-id",
            "application_id": 7,
        },
    )
    monkeypatch.setattr(bit_mysql, "get_mercado_application", lambda _app_id: _application())

    client = batch_publish.DatabaseMercadoLibreClient(3)

    assert client.client_id == "selected-app-id"
    assert client.client_secret == "selected-secret"


def test_application_api_client_uses_central_routes(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {},
    )

    bit_db_api.list_mercado_applications()
    bit_db_api.create_mercado_application({"name": "应用"})
    bit_db_api.update_mercado_application(7, {"name": "新应用"})
    bit_db_api.reauthorize_mercado_store_application(3, 7, "TG-code")
    bit_db_api.delete_mercado_application(7)

    assert [(method, path) for method, path, _kwargs in calls] == [
        ("GET", "/api/db/mercado-applications"),
        ("POST", "/api/db/mercado-applications"),
        ("PATCH", "/api/db/mercado-applications/7"),
        ("POST", "/api/db/mercado-tokens/3/application"),
        ("DELETE", "/api/db/mercado-applications/7"),
    ]
    assert calls[3][2]["json"] == {"application_id": 7, "code": "TG-code"}


def test_browser_routes_manage_apps_and_change_store_app(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "list_mercado_applications",
        lambda: {"total": 1, "rows": [{"id": 7, "name": "刊登应用"}]},
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "create_mercado_application",
        lambda record: calls.append(("create", record)) or {"id": 7},
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "update_mercado_application",
        lambda app_id, changes: calls.append(("update", app_id, changes)) or {"id": app_id},
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "delete_mercado_application",
        lambda app_id: calls.append(("delete", app_id)) or 1,
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "reauthorize_mercado_store_application",
        lambda token_id, app_id, code: calls.append(("store-app", token_id, app_id, code))
        or {"id": token_id, "application_id": app_id},
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = {"id": 1, "username": "tester"}

    assert client.get("/api/mercado-applications").status_code == 200
    assert client.post("/api/mercado-applications", json={"name": "刊登应用"}).status_code == 200
    assert client.patch("/api/mercado-applications/7", json={"name": "新应用"}).status_code == 200
    changed = client.post(
        "/api/mercado-tokens/3/application",
        json={"application_id": 7, "code": "TG-code"},
    )
    assert changed.status_code == 200
    assert "后续刊登将使用新应用" in changed.get_json()["message"]
    assert client.delete("/api/mercado-applications/7").status_code == 200
    assert calls == [
        ("create", {"name": "刊登应用"}),
        ("update", 7, {"name": "新应用"}),
        ("store-app", 3, 7, "TG-code"),
        ("delete", 7),
    ]


def test_store_authorization_template_contains_application_management():
    client = bit_interface.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = {"id": 1, "username": "tester"}

    body = client.get("/").get_data(as_text=True)

    assert 'id="mercado-token-application"' in body
    assert 'id="mercado-application-dialog"' in body
    assert "saveMercadoApplication" in body
    assert "changeMercadoStoreApplication" in body
    assert "后续刊登的官方开发者应用" in body
