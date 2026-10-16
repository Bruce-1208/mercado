"""Public serialization must not expose AI costs or mutate persisted state."""
import ast
import copy
import json
from pathlib import Path

import pytest
from flask import Flask, jsonify

from bit.ai_billing import can_view_ai_costs, install_cost_visibility


@pytest.mark.parametrize("role", ["super_admin", "enterprise_admin", "member", "warehouse", None])
def test_public_responses_filter_nested_and_serialized_costs(role):
    app = Flask(__name__)
    user = {"id": 1, "role_key": role, "is_platform_admin": True}
    install_cost_visibility(app, lambda: user)
    prepared = {"title": "商品", "generation_budget": {"spent_yuan": 0.012, "calls": [{"cost_nano": 12000000}]}}
    payload = {"data": {"rows": [{"ai_original": prepared,
        "source_snapshot_json": json.dumps({"ai_original": prepared})}],
        "results": [{"generation_budget": prepared["generation_budget"], "status": "completed"}],
        "generation_cost_summary": {"spent_yuan": 0.012}, "cost_cap_yuan": 0.03,
        "purchase_cost_cny": 12, "net_proceeds_usd": 5,
        "message": "模型调用中断，本次费用待确认；可重新生成继续，历史预留费用已保留。"}}
    original = copy.deepcopy(payload)
    app.add_url_rule("/api/products", view_func=lambda: jsonify(payload))
    app.add_url_rule("/api/db/products", endpoint="internal", view_func=lambda: jsonify(payload))
    response = app.test_client().get("/api/products")
    data = response.json["data"]
    assert payload == original
    assert data["purchase_cost_cny"] == 12 and data["net_proceeds_usd"] == 5
    assert data["results"][0]["status"] == "completed"
    if role == "super_admin":
        assert response.json == original
    else:
        assert "generation_budget" not in json.dumps(response.json)
        assert "spent_yuan" not in json.dumps(response.json)
        assert "cost_cap_yuan" not in data
        assert "费用" not in data["message"]
        assert json.loads(data["rows"][0]["source_snapshot_json"])["ai_original"] == {"title": "商品"}
    assert app.test_client().get("/api/db/products").json == original


@pytest.mark.parametrize("role", ["enterprise_admin", "member", "warehouse", None])
def test_cost_summary_denied_before_storage_access(role):
    source = Path("bit/bit_interface.py").read_text()
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == "api_ai_original_cost_summary")
    node.decorator_list = []
    namespace = {"can_view_ai_costs": can_view_ai_costs, "jsonify": jsonify,
                 "get_current_workbench_user": lambda: {"role_key": role}}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<cost-summary>", "exec"), namespace)
    with Flask(__name__).test_request_context():
        response, status = namespace[node.name]()
        assert status == 403


def test_billing_navigation_is_rendered_only_for_super_admin():
    from jinja2 import Template
    source = Path("bit/templates/index.html").read_text()
    link = source.index('href="/ai-billing"')
    fragment = source[source.rfind("{% if", 0, link):source.index("{% endif %}", link) + len("{% endif %}")]
    for role in ["member", "warehouse", "enterprise_admin", "admin", "super_admin"]:
        rendered = Template(fragment).render(current_user={"role_key": role})
        assert ('href="/ai-billing"' in rendered) == (role == "super_admin")


@pytest.mark.parametrize('role', ['super_admin', 'enterprise_admin', 'operator', 'member', 'warehouse', 'viewer', None])
def test_legacy_sku_failure_keeps_reason_but_hides_appended_usage(role):
    app = Flask(__name__)
    install_cost_visibility(app, lambda: {'role_key': role})
    reason = '产品 16943431：24 个 SKU 缺少采集原图，请重新采集或补充图片链接'
    message = reason + ' · 模型计费估算 ¥0.013195（含已记录尝试） · 累计 60,318 Token · 金额不限额 · 复用结果 1 次'
    payload = {'data': {'results': [{'message': message}], 'ai_error': message,
                        'logs': [message], 'total_tokens': 60318, 'prompt_tokens': 60300,
                        'net_proceeds_usd': 12}}
    app.add_url_rule('/api/ai-original-products/process/status', view_func=lambda: jsonify(payload))
    result = app.test_client().get('/api/ai-original-products/process/status').json
    if role == 'super_admin':
        assert result == payload
    else:
        assert result['data']['results'][0]['message'] == reason
        assert result['data']['ai_error'] == reason
        assert result['data']['logs'] == [reason]
        assert result['data']['net_proceeds_usd'] == 12
        assert not any(value in json.dumps(result, ensure_ascii=False) for value in ['Token', '¥', '计费', '金额不限额', 'total_tokens', 'prompt_tokens'])
