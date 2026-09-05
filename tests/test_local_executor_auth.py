"""Regression coverage for delegated local execution without a shared DB token."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests
from itsdangerous import TimestampSigner

from bit import bit_db_api, bit_interface


@pytest.fixture
def executor_user(monkeypatch):
    user = {
        "id": 7,
        "username": "local-operator",
        "permissions": ["appeal.execute", "tasks.view", "tasks.execute"],
        "access_version": 1,
        "is_active": True,
    }
    monkeypatch.delenv("BIT_DB_API_TOKEN", raising=False)
    monkeypatch.setattr(bit_interface.app, "testing", True)
    monkeypatch.setattr(bit_interface.app, "secret_key", "local-executor-test-server-key")
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.setattr(
        bit_interface,
        "RUNTIME_SETTINGS",
        replace(bit_interface.RUNTIME_SETTINGS, role="server"),
    )
    monkeypatch.setattr(bit_interface, "get_current_workbench_user", lambda: user)
    monkeypatch.setattr(bit_interface, "get_workbench_user", lambda **_kwargs: user)
    monkeypatch.setattr(bit_interface, "build_workbench_session_user", lambda row: dict(row))
    monkeypatch.setattr(
        bit_db_api,
        "get_database_api_health",
        lambda: {"role": "server", "database_host": "test-db"},
    )
    return user


def _claims(user, permission="tasks.execute"):
    return {"id": user["id"], "username": user["username"], "permission": permission}


def _response(payload, status_code=200):
    return SimpleNamespace(
        ok=200 <= status_code < 300,
        status_code=status_code,
        json=lambda: payload,
    )


def test_logged_in_user_can_get_and_verify_token_without_db_secret(executor_user):
    client = bit_interface.app.test_client()
    response = client.post(
        "/api/execution-targets/local-token", json={"permission": "tasks.execute"}
    )
    assert response.status_code == 200
    token = response.get_json()["data"]["token"]
    assert token.startswith("session:")

    verified = client.post(
        "/api/execution-targets/local-token/verify",
        headers={"Authorization": f"Bearer {token}"},
        environ_overrides={"REMOTE_ADDR": "10.0.0.20"},
    )
    assert verified.status_code == 200
    assert verified.get_json()["data"] == _claims(executor_user)


def test_token_issuance_still_requires_the_requested_permission(monkeypatch, executor_user):
    executor_user["permissions"] = ["tasks.view"]
    response = bit_interface.app.test_client().post(
        "/api/execution-targets/local-token", json={"permission": "tasks.execute"}
    )
    assert response.status_code == 403


def test_tampered_session_token_is_rejected(executor_user):
    token = bit_interface.create_local_executor_token(executor_user, "tasks.execute")
    assert bit_interface._local_executor_user_from_token(token + "tampered") is None


def test_session_token_expires_after_short_validity(monkeypatch, executor_user):
    issued_at = 1_700_000_000
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _self: issued_at)
    token = bit_interface.create_local_executor_token(executor_user, "tasks.execute")
    monkeypatch.setattr(
        TimestampSigner,
        "get_timestamp",
        lambda _self: issued_at + bit_interface.LOCAL_EXECUTOR_TOKEN_MAX_AGE_SECONDS + 1,
    )
    assert bit_interface._local_executor_user_from_token(token) is None


@pytest.mark.parametrize("change", ["disabled", "deleted", "permission_removed", "renamed"])
def test_session_token_rechecks_current_account(monkeypatch, executor_user, change):
    token = bit_interface.create_local_executor_token(executor_user, "tasks.execute")
    if change == "disabled":
        executor_user["is_active"] = False
    elif change == "deleted":
        monkeypatch.setattr(bit_interface, "get_workbench_user", lambda **_kwargs: None)
    elif change == "permission_removed":
        executor_user["permissions"] = ["tasks.view"]
    else:
        executor_user["username"] = "renamed-operator"
    assert bit_interface._local_executor_user_from_token(token) is None


def test_verify_endpoint_requires_bearer_and_does_not_accept_login_session(executor_user):
    token = bit_interface.create_local_executor_token(executor_user, "tasks.execute")
    response = bit_interface.app.test_client().post(
        "/api/execution-targets/local-token/verify",
        query_string={"token": token},
        json={"token": token},
    )
    assert response.status_code == 401


def test_view_token_cannot_start_or_stop_local_tasks(monkeypatch, executor_user):
    token = bit_interface.create_local_executor_token(executor_user, "tasks.view")
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    monkeypatch.setattr(
        bit_interface,
        "_verify_local_executor_token_with_server",
        lambda _token: _claims(executor_user, "tasks.view"),
    )
    monkeypatch.setattr(
        bit_db_api,
        "get_database_api_health",
        lambda: pytest.fail("Denied permissions must not reach database preflight"),
    )
    client = bit_interface.app.test_client()
    headers = {"Authorization": f"Bearer {token}", "Origin": "https://zeshun.nat100.top"}
    for action in ("start", "stop"):
        response = client.post(
            f"/api/local-executor/tasks/daily/{action}",
            headers=headers,
            json={},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    ("server_url", "expected_base"),
    [
        ("https://workbench.example", "https://workbench.example"),
        ("http://zeshun.nat100.top", "https://zeshun.nat100.top"),
        ("http://127.0.0.1:5001", "http://127.0.0.1:5001"),
    ],
)
def test_client_verifies_against_only_configured_server(
    monkeypatch, executor_user, server_url, expected_base
):
    token = bit_interface.create_local_executor_token(executor_user, "tasks.execute")
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    monkeypatch.setattr(
        bit_interface,
        "RUNTIME_SETTINGS",
        replace(bit_interface.RUNTIME_SETTINGS, role="client", api_base_url=server_url),
    )
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _response({"status": "success", "data": _claims(executor_user)})

    monkeypatch.setattr(bit_db_api.DB_API_SESSION, "post", post)
    result = bit_interface._local_executor_user_from_token(token)
    assert result == _claims(executor_user)
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == f"{expected_base}/api/execution-targets/local-token/verify"
    assert kwargs["headers"]["Authorization"] == f"Bearer {token}"
    assert "X-Internal-Token" not in kwargs["headers"]
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == 10


def test_server_token_never_authenticates_internal_database_api(executor_user):
    token = bit_interface.create_local_executor_token(executor_user, "tasks.execute")
    response = bit_interface.app.test_client().get(
        "/api/db/health",
        headers={"Authorization": f"Bearer {token}", "X-Internal-Token": token},
        environ_overrides={"REMOTE_ADDR": "10.0.0.20"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("status_code", [302, 401, 403])
def test_remote_verification_rejects_redirects_and_denials(
    monkeypatch, executor_user, status_code
):
    token = bit_interface.create_local_executor_token(executor_user, "tasks.execute")
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    monkeypatch.setattr(
        bit_interface,
        "RUNTIME_SETTINGS",
        replace(bit_interface.RUNTIME_SETTINGS, role="client", api_base_url="https://workbench.example"),
    )
    monkeypatch.setattr(
        bit_db_api.DB_API_SESSION,
        "post",
        lambda *_args, **_kwargs: _response(
            {"status": "success", "data": _claims(executor_user)}, status_code=status_code
        ),
    )
    if status_code == 302:
        with pytest.raises(RuntimeError, match="服务端"):
            bit_interface._local_executor_user_from_token(token)
    else:
        assert bit_interface._local_executor_user_from_token(token) is None


@pytest.mark.parametrize(
    "server_url",
    [
        "http://workbench.example",
        "https://username:password@workbench.example",
        "https://workbench.example?forward=other",
        "https://workbench.example#fragment",
    ],
)
def test_unsafe_verification_address_rejected_before_request(
    monkeypatch, executor_user, server_url
):
    token = bit_interface.create_local_executor_token(executor_user, "tasks.execute")
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    monkeypatch.setattr(
        bit_interface,
        "RUNTIME_SETTINGS",
        replace(bit_interface.RUNTIME_SETTINGS, role="client", api_base_url=server_url),
    )

    def unexpected_request(*_args, **_kwargs):
        pytest.fail("Unsafe verification URL must never receive a bearer credential")

    monkeypatch.setattr(bit_db_api.DB_API_SESSION, "post", unexpected_request)
    with pytest.raises(RuntimeError, match="HTTPS"):
        bit_interface._local_executor_user_from_token(token)


def test_verification_network_failure_has_actionable_error(monkeypatch, executor_user):
    token = bit_interface.create_local_executor_token(executor_user, "tasks.execute")
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    monkeypatch.setattr(
        bit_interface,
        "RUNTIME_SETTINGS",
        replace(bit_interface.RUNTIME_SETTINGS, role="client", api_base_url="https://workbench.example"),
    )

    def unavailable_server(*_args, **_kwargs):
        raise requests.ConnectionError("test connection failed")

    monkeypatch.setattr(bit_db_api.DB_API_SESSION, "post", unavailable_server)
    with pytest.raises(RuntimeError, match="客户端的服务端地址和连接"):
        bit_interface._local_executor_user_from_token(token)


def test_existing_shared_token_mode_remains_supported(monkeypatch, executor_user):
    monkeypatch.setenv("BIT_DB_API_TOKEN", "test-shared-secret")
    token = bit_interface.create_local_executor_token(executor_user, "tasks.execute")
    assert not token.startswith("session:")
    assert bit_interface._local_executor_user_from_token(token) == _claims(executor_user)


def test_verification_route_is_not_served_by_client(monkeypatch, executor_user):
    token = bit_interface.create_local_executor_token(executor_user, "tasks.execute")
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    response = bit_interface.app.test_client().post(
        "/api/execution-targets/local-token/verify",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 503


@pytest.mark.parametrize(
    ("method", "path", "permission", "handler_name"),
    [
        ("POST", "/api/local-executor/tasks/daily/start", "tasks.execute", "api_start_daily_task"),
        ("GET", "/api/local-executor/run_shensu", "appeal.execute", "api_run_shensu"),
    ],
)
@pytest.mark.parametrize("failure", ["forbidden", "wrong_role"])
def test_database_preflight_blocks_launch_before_creating_job(
    monkeypatch, executor_user, method, path, permission, handler_name, failure
):
    token = bit_interface.create_local_executor_token(executor_user, permission)
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    monkeypatch.setattr(
        bit_interface,
        "_verify_local_executor_token_with_server",
        lambda _token: _claims(executor_user, permission),
    )

    def database_health():
        if failure == "forbidden":
            raise RuntimeError("Forbidden")
        return {"role": "client"}

    monkeypatch.setattr(bit_db_api, "get_database_api_health", database_health)
    monkeypatch.setattr(
        bit_interface,
        handler_name,
        SimpleNamespace(__wrapped__=lambda: pytest.fail("Job must not start before data access is ready")),
    )
    response = bit_interface.app.test_client().open(
        path,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Origin": "https://zeshun.nat100.top"},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 503
    message = response.get_json()["message"]
    assert "数据接口" in message
    assert "BIT_DB_API_TOKEN" in message


def test_distinct_server_and_client_keys_integrate_through_real_bridge(
    monkeypatch, executor_user
):
    server_secret = bit_interface.app.secret_key
    issued = bit_interface.app.test_client().post(
        "/api/execution-targets/local-token", json={"permission": "tasks.execute"}
    )
    assert issued.status_code == 200
    token = issued.get_json()["data"]["token"]

    monkeypatch.setattr(bit_interface.app, "secret_key", "different-local-client-secret")
    monkeypatch.setattr(bit_interface, "USE_DB_API", True)
    monkeypatch.setattr(
        bit_interface,
        "RUNTIME_SETTINGS",
        replace(bit_interface.RUNTIME_SETTINGS, role="client", api_base_url="https://workbench.example"),
    )
    verification_statuses = []

    def invoke_actual_server_verify(url, **kwargs):
        assert url == "https://workbench.example/api/execution-targets/local-token/verify"
        assert kwargs["allow_redirects"] is False
        with monkeypatch.context() as server_context:
            server_context.setattr(bit_interface, "USE_DB_API", False)
            server_context.setattr(bit_interface.app, "secret_key", server_secret)
            server_response = bit_interface.app.test_client().post(
                "/api/execution-targets/local-token/verify",
                headers=kwargs["headers"],
                environ_overrides={"REMOTE_ADDR": "10.0.0.20"},
            )
            verification_statuses.append(server_response.status_code)
            return _response(server_response.get_json(), server_response.status_code)

    monkeypatch.setattr(bit_db_api.DB_API_SESSION, "post", invoke_actual_server_verify)
    launches = []

    def create_job():
        launches.append(dict(bit_interface.g.local_executor_user))
        return {"status": "success", "data": {"task_id": "test-task"}}

    monkeypatch.setattr(
        bit_interface,
        "api_start_daily_task",
        SimpleNamespace(__wrapped__=create_job),
    )
    client = bit_interface.app.test_client()
    for submitted_token, expected_status in ((token, 200), (token + "tampered", 401)):
        response = client.post(
            "/api/local-executor/tasks/daily/start",
            json={},
            headers={
                "Authorization": f"Bearer {submitted_token}",
                "Origin": "https://zeshun.nat100.top",
            },
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert response.status_code == expected_status

    assert verification_statuses == [200, 401]
    assert launches == [_claims(executor_user)]
    assert bit_interface.app.secret_key == "different-local-client-secret"
    assert bit_interface.USE_DB_API is True
