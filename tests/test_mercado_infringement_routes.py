"""Exercise public permissions and the real service without business connections."""
import json
import time

import pytest

from bit import bit_interface as console
from bit.infringement_state import canonical_record, content_hash, knowledge_version
from bit.infringement_tasks import InfringementTaskStore
from bit.mercado_infringement import MercadoInfringementService


@pytest.fixture
def api(tmp_path, monkeypatch, subscribed_workbench_account):
    user = {"id": 7, "username": "reviewer", "role_key": "custom", "access_version": 1, "organization_key": "enterprise-a",
            "permissions": ["risk_check.view", "risk_check.execute", "risk_check.review", "infringement_knowledge.manage"]}
    monkeypatch.setattr(console, "get_current_workbench_user", lambda: user)
    monkeypatch.setattr(console, "_authorized_token_ids_for_user", lambda *args: None)
    rows = {1: {"id": 1, "title": "桌面收纳盒", "source_item_id": "MLM1", "category_name": "Storage",
                "infringement_check_json": None, "infringement_risk_level": None, "added_to_products": False}}
    captured = {}

    def candidates(**kwargs):
        return [canonical_record(rows[item["row_id"]], item["source_type"]) for item in kwargs["items"]]

    def scan(**kwargs):
        records = kwargs["candidate_reader"](items=kwargs["items"])
        output = []
        for record in records:
            event = {**record, "status": "completed", "check_status": "completed", "risk_level": 0,
                     "content_hash": content_hash(record, kwargs["detection_scope"]),
                     "rule_version": kwargs["rule_version"], "detection_scope": kwargs["detection_scope"]}
            kwargs["item_callback"](event)
            output.append(event)
        kwargs["risk_writer"](output)
        return {"checked": len(output), "results": output}

    def writer(output):
        for result in output:
            rows[result["row_id"]]["infringement_check_json"] = json.dumps(result)
            rows[result["row_id"]]["infringement_risk_level"] = result["risk_level"]

    def review(*args, **kwargs):
        captured["review"] = (args, kwargs)
        return {"updated": 1}

    deps = {"knowledge": lambda **kwargs: {"rows": []}, "credential": lambda uid: "offline-account-token",
            "candidates": candidates, "writer": writer, "scan": scan, "review": review}
    for mode in ("collection", "products"):
        deps["get_" + mode] = lambda ids: [rows[value] for value in ids if value in rows]
        deps["list_" + mode] = lambda **kwargs: {"total": len(rows), "rows": list(rows.values())[kwargs.get("offset", 0):kwargs.get("offset", 0) + kwargs.get("limit", 1000)]}
    service = MercadoInfringementService(InfringementTaskStore(tmp_path / "tasks.sqlite3"), deps)
    monkeypatch.setattr(console, "get_mercado_infringement_service", lambda: service)
    return console.app.test_client(), service, user, captured


def test_public_start_detail_and_task_ownership(api):
    client, service, user, _ = api
    response = client.post("/api/mercado/infringement/tasks", json={
        "list_mode": "products", "selection_mode": "selected", "item_ids": [1], "detection_scope": "title"})
    assert response.status_code == 202
    task_id = response.get_json()["data"]["task_id"]
    for _ in range(100):
        task = service.store.get(task_id, 7)
        if not task["running"]:
            break
        time.sleep(.01)
    assert task["status"] == "completed" and task["total"] == task["completed"] == 1
    assert "offline-account-token" not in client.get("/api/mercado/infringement/tasks").get_data(as_text=True)
    detail = client.get("/api/mercado/infringement/items/products/1")
    assert detail.status_code == 200
    assert detail.get_json()["data"]["infringement"]["status"] == "low"
    user["id"] = 8
    assert client.get(f"/api/mercado/infringement/tasks/{task_id}").status_code == 404
    assert client.post(f"/api/mercado/infringement/tasks/{task_id}/cancel").status_code == 404


def test_execute_permission_cannot_grant_product_release(api):
    client, _, user, _ = api
    user["permissions"] = ["risk_check.view", "risk_check.execute"]
    response = client.post("/api/mercado/infringement/items/products/1/review", json={
        "decision": "released", "reason": "授权已核实", "site_ids": ["MLM"],
        "evidence_ref": "AUTH-1", "expires_at": "2099-01-01T00:00:00Z"})
    assert response.status_code == 403
    assert response.get_json()["required_permissions"] == ["risk_check.review"]


def test_review_requires_evidence_scope_and_uses_authenticated_actor(api):
    client, _, _, captured = api
    path = "/api/mercado/infringement/items/products/1/review"
    assert client.post(path, json={"decision": "released", "reason": "yes"}).status_code == 400
    response = client.post(path, json={"decision": "released", "reason": "授权已核实",
        "site_ids": ["MLM"], "evidence_ref": "AUTH-1", "expires_at": "2099-01-01T00:00:00Z",
        "reviewer": "spoofed-user"})
    assert response.status_code == 200
    args, kwargs = captured["review"]
    assert args[0] == [{"source_type": "product_list", "row_id": 1}]
    assert args[1] == "allow" and args[3] == "reviewer"
    assert kwargs["site_ids"] == ["MLM"]


def test_new_stateful_endpoints_have_one_worker_owner():
    from bit.service_split import is_worker_path
    assert is_worker_path("/api/mercado/infringement/tasks")
    assert is_worker_path("/api/mercado/infringement/items/products/1/review")
    assert is_worker_path("/api/risk-check/start")
    assert is_worker_path("/api/mercado-products")
