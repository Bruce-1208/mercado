from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook

from bit import bit_db_api, bit_interface


def _logged_in_client():
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}
    return client


def test_risk_check_console_exposes_start_sort_and_export_controls():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'data-tab="risk-check"' in template
    assert 'id="tab-risk-check"' in template
    assert 'id="start-risk-check-btn"' in template
    assert 'id="risk-check-log"' in template
    assert "主图链接和 Logo 暂不参与判断" in template
    assert 'data-risk-sort="risk_level"' in template
    assert "function sortRiskResults(column)" in template
    assert "function exportRiskResults()" in template
    assert "/api/risk-check/results/export" in template


def test_build_risk_check_params_clamps_console_values():
    params = bit_interface.build_risk_check_params(
        {
            "category": "202170568",
            "hours": -1,
            "limit": 999999,
            "batch_size": 0,
            "ai_retries": 99,
            "recheck": "true",
        }
    )

    assert params["zying_category"] == "202170568"
    assert params["hours"] == 0
    assert params["limit"] == 50000
    assert params["batch_size"] == 1
    assert "workers" not in params
    assert "min_ocr_confidence" not in params
    assert params["retries"] == 5
    assert params["recheck"] is True


def test_risk_check_results_api_passes_filters_and_sort(monkeypatch):
    captured = {}

    def get_results(**kwargs):
        captured.update(kwargs)
        return {
            "total": 1,
            "risk_0": 0,
            "risk_1": 0,
            "risk_2": 1,
            "unchecked": 0,
            "rows": [{"row_id": 7, "risk_level": "2"}],
        }

    monkeypatch.setattr(bit_interface, "db_get_zying_risk_results", get_results)
    response = _logged_in_client().get(
        "/api/risk-check/results",
        query_string={
            "category": "玩具类",
            "risk_level": "2",
            "search": "Pokemon",
            "sort_by": "submitted_at",
            "sort_dir": "asc",
            "limit": "500",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["rows"][0]["row_id"] == 7
    assert captured == {
        "zying_category": "玩具类",
        "risk_level": "2",
        "search": "Pokemon",
        "sort_by": "submitted_at",
        "sort_dir": "asc",
        "limit": 500,
    }


def test_risk_check_export_keeps_filters_and_creates_workbook(monkeypatch):
    captured = {}

    def get_results(**kwargs):
        captured.update(kwargs)
        return {
            "rows": [
                {
                    "row_id": 9,
                    "product_id": "P-9",
                    "title": "Pokemon Pikachu plush",
                    "zying_category_id": "88",
                    "zying_category": "玩具/毛绒",
                    "product_category": "Plush",
                    "risk_level": "2",
                    "keywords": "Pokemon, Pikachu",
                    "main_image_url": "https://example.test/p.jpg",
                    "collected_at": "2026-07-30 10:00:00",
                    "submitted_at": "2026-07-30 10:01:00",
                }
            ]
        }

    monkeypatch.setattr(bit_interface, "db_get_zying_risk_results", get_results)
    response = _logged_in_client().get(
        "/api/risk-check/results/export",
        query_string={"risk_level": "2", "sort_by": "title", "sort_dir": "asc"},
    )

    assert response.status_code == 200
    assert captured["risk_level"] == "2"
    assert captured["sort_by"] == "title"
    assert captured["sort_dir"] == "asc"
    assert captured["limit"] == 0
    workbook = load_workbook(BytesIO(response.data))
    sheet = workbook["侵权检测结果"]
    assert sheet["A2"].value == 9
    assert sheet["G2"].value == "2 - 侵权"
    assert sheet["H2"].value == "Pokemon, Pikachu"
    assert sheet.auto_filter.ref


def test_risk_check_start_runs_in_background_and_updates_status(monkeypatch):
    captured = {}

    class ImmediateThread:
        def __init__(self, target, args=(), **kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    def scan_products(**kwargs):
        captured.update(kwargs)
        kwargs["log_callback"]("开始标题审核批次 1/1，3 条")
        return {
            "checked": 3,
            "risk_0": 1,
            "risk_1": 1,
            "risk_2": 1,
            "updated": 3,
            "results": [],
        }

    monkeypatch.setattr(bit_interface.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(bit_interface.bit_check_risk, "scan_products", scan_products)
    with bit_interface._risk_check_state_lock:
        previous = dict(bit_interface._risk_check_state)
        previous_logs = list(bit_interface._risk_check_logs)
    try:
        response = _logged_in_client().post(
            "/api/risk-check/start",
            json={"category": "202170568", "limit": 3},
        )
        status_response = _logged_in_client().get("/api/risk-check/status")
    finally:
        with bit_interface._risk_check_state_lock:
            bit_interface._risk_check_state.clear()
            bit_interface._risk_check_state.update(previous)
            bit_interface._risk_check_logs.clear()
            bit_interface._risk_check_logs.extend(previous_logs)

    assert response.status_code == 200
    assert captured["zying_category"] == "202170568"
    assert captured["candidate_reader"] is bit_interface.db_get_zying_risk_candidates
    assert captured["risk_writer"] is bit_interface.db_update_zying_product_risks
    assert callable(captured["log_callback"])
    status = status_response.get_json()["data"]
    assert status["running"] is False
    assert status["status"] == "success"
    assert status["summary"]["risk_2"] == 1
    assert any("开始标题审核批次 1/1" in line for line in status["logs"])


def test_bit_db_api_forwards_risk_result_sorting(monkeypatch):
    captured = {}
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")

    def request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"rows": []}

    monkeypatch.setattr(bit_db_api, "_request", request)
    result = bit_db_api.get_zying_risk_results(
        zying_category="玩具类",
        risk_level="1",
        sort_by="keywords",
        sort_dir="asc",
        limit=200,
    )

    assert result == {"rows": []}
    assert captured["path"] == "/api/db/zying-risk/results"
    assert captured["params"]["category"] == "玩具类"
    assert captured["params"]["sort_by"] == "keywords"
