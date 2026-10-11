"""Bill queries never accept a caller-supplied account or enterprise scope."""
from pathlib import Path
import ast
from types import SimpleNamespace

import pytest
from flask import Flask, g, request, jsonify

from bit.ai_billing import actor_context, create_blueprint, install_usage_context, account_usage_context


ROOT = Path(__file__).resolve().parents[1]


def application(user, storage=None):
    app = Flask(__name__, template_folder=str(ROOT / "bit/templates"), static_folder=str(ROOT / "bit/static"))
    app.config["TESTING"] = True
    calls = []

    def query(payload):
        calls.append(payload)
        return {"items": [], "total": 0, "page": 1, "page_size": 20,
                "summary": {}, "options": {}}

    app.register_blueprint(create_blueprint(
        login_required=lambda function: function, current_user=lambda: user,
        selected_organization=lambda user: "company-selected" if user.get("role_key") == "super_admin" else "",
        storage=storage or query,
    ))
    return app, calls


@pytest.mark.parametrize("role,scope,organization", [
    ("super_admin", None, "company-selected"),
])
def test_bill_scopes_are_resolved_server_side(role, scope, organization):
    app, calls = application({"id": 7, "role_key": role, "organization_key": "company-a"})
    response = app.test_client().get(
        "/api/ai-billing?scope_user_ids=8&organization_key=other&actor_id=8&"
        "start_date=2026-10-01&end_date=2026-10-07&salesperson=小张&task_type=appeal&model=qwen&keyword=任务&page=2&page_size=50"
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = calls[0]
    assert payload["scope_user_ids"] == scope
    assert payload["organization_key"] == organization
    assert "actor_id" not in payload
    assert payload["salesperson"] == "小张" and payload["task_type"] == "appeal"
    assert payload["start_date"] == "2026-10-01" and payload["end_date"] == "2026-10-07"
    assert payload["page"] == "2" and payload["page_size"] == "50"


def test_bill_query_requires_an_identity():
    app, calls = application({})
    assert app.test_client().get("/api/ai-billing").status_code == 401
    assert calls == []


def test_invalid_filters_and_unavailable_storage_are_reported():
    def invalid(payload):
        raise ValueError("日期范围无效")
    app, _ = application({"id": 7, "role_key": "super_admin"}, invalid)
    response = app.test_client().get("/api/ai-billing")
    assert response.status_code == 400 and response.json["message"] == "日期范围无效"

    def failed(payload):
        raise RuntimeError("private database detail")
    app, _ = application({"id": 7, "role_key": "super_admin"}, failed)
    response = app.test_client().get("/api/ai-billing")
    assert response.status_code == 503 and "private" not in response.json["message"]


def test_page_renders_without_querying_or_writing_bills():
    app, calls = application({"id": 7, "role_key": "super_admin"})
    response = app.test_client().get("/ai-billing")
    assert response.status_code == 200
    assert "AI 账单" in response.get_data(as_text=True)
    assert calls == []


def test_context_captures_decorated_user_and_survives_background_thread():
    from erp import ai_usage_billing as billing
    from erp.ai_usage_billing import bind_usage, current_usage_context, set_usage_context_resolver
    import threading
    app, _ = application({"id": 7})
    previous = billing._context_resolver
    try:
        install_usage_context(current_user=lambda: {"id": 7}, selected_organization=lambda user: "")
        with app.test_request_context():
            g.browser_extension_user = {"id": 19, "display_name": "业务员乙", "organization_key": "company-b"}
            values = []
            worker = bind_usage(lambda: values.append(current_usage_context()), task_id="product-22")
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(3)
        assert values[0]["actor_id"] == 19 and values[0]["organization_key"] == "company-b"
        assert values[0]["salesperson"] == "业务员乙" and values[0]["task_id"] == "product-22"
    finally:
        set_usage_context_resolver(previous)


def test_actor_context_normalizes_legacy_enterprise():
    assert actor_context({"id": 7, "username": "sales", "organization_key": "default"}) == {
        "actor_id": 7, "actor_name": "sales", "salesperson": "sales", "organization_key": "wuhan-zeshun",
    }


def isolated_function(name, **dependencies):
    node = next(node for node in ast.parse((ROOT / "bit/bit_interface.py").read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    namespace = dict(request=request, g=g, jsonify=jsonify, **dependencies)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<ai-billing-route>", "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("container", ["record", "data", "top"])
def test_agent_cannot_forge_record_owner_even_with_nested_payload(container):
    writes = []
    owner = {"id": 8, "display_name": "Agent业务员", "organization_key": "company-a", "is_active": True}
    creator = {"id": 9, "display_name": "发起人", "organization_key": "company-a", "is_active": True}
    route = isolated_function("api_db_ai_usage_billing",
        reject_db_api_client_mode=lambda: None,
        get_workbench_user=lambda user_id: {8: owner, 9: creator}.get(user_id),
        ai_billing_actor_context=actor_context,
        ai_usage_billing_local=lambda payload: writes.append(payload) or {},
    )
    record = {"actor_id": 99, "actor_name": "伪造", "salesperson": "别人", "organization_key": "other"}
    payload = {"action": "record", **record} if container == "top" else {"action": "record", container: record}
    with Flask(__name__).test_request_context(json=payload):
        g.internal_agent_claims = {"agent_id": "agent-a", "user_id": 8}
        g.internal_agent_jobs = [{"created_by_id": 9}]
        assert route().status_code == 200
    assert writes[0]["actor_id"] == 9 and writes[0]["actor_name"] == "发起人"
    assert writes[0]["salesperson"] == "发起人" and writes[0]["organization_key"] == "company-a"


def test_agent_only_writes_usage_for_its_own_job_and_can_retry_completed_job():
    job = {"agent_id": "agent-a", "job_type": "ai_weight_price", "status": "success", "created_by_id": 9}
    allowed = isolated_function("_agent_internal_api_allowed",
        get_local_agent_store=lambda: SimpleNamespace(get_job=lambda job_id: job),
        DAILY_AGENT_DB_POST_PATHS=set(), AI_WEIGHT_PRICE_AGENT_DB_POST_PATHS=set(),
        DAILY_AGENT_DB_GET_PATHS=set(),
    )
    app = Flask(__name__)
    with app.test_request_context("/api/db/ai-usage-billing", method="POST",
                                  json={"action": "record", "record": {"agent_job_id": "old-job"}}):
        assert allowed({"agent_id": "agent-a", "user_id": 8})
        assert not allowed({"agent_id": "agent-b", "user_id": 8})
    with app.test_request_context("/api/db/ai-usage-billing", method="POST", json={"action": "list"}):
        assert not allowed({"agent_id": "agent-a", "user_id": 8})


def test_scheduled_ai_restores_creator_and_keeps_manual_initiator():
    from erp.ai_usage_billing import usage_context, current_usage_context
    lookups = []

    @account_usage_context("user_id", get_user=lambda user_id: lookups.append(user_id) or {
        "id": user_id, "display_name": "定时任务创建人", "organization_key": "company-b", "is_active": True,
    })
    def run(user_id):
        return current_usage_context()

    with usage_context(actor_id=0):
        restored = run(19)
        assert restored["actor_id"] == 19 and restored["organization_key"] == "company-b"
    with usage_context(actor_id=7, actor_name="操作人", organization_key="company-a"):
        manual = run(19)
        assert manual["actor_id"] == 7 and manual["organization_key"] == "company-a"
    assert lookups == [19]


@pytest.mark.parametrize("role", ["member", "warehouse", "enterprise_admin", "admin", "", None])
def test_non_super_admin_cannot_read_billing_even_with_legacy_admin_flags(role):
    app, calls = application({"id": 7, "role_key": role, "is_admin": True,
                              "is_platform_admin": True, "permissions": ["*"]})
    for path in ("/ai-billing", "/api/ai-billing?role_key=super_admin"):
        assert app.test_client().get(path).status_code == 403
    assert calls == []
