from pathlib import Path

import pytest

from bit import bit_db_api, bit_interface
from bit.bit_infringement_knowledge import (
    normalize_knowledge_record,
    normalize_list_type,
    parse_bulk_brand_lines,
)
from bit.bit_infringement_knowledge_analysis import (
    analyze_knowledge_sources,
    extract_brand_names,
    normalize_brand_name,
)


def _user(*permissions):
    return {
        "id": 1,
        "username": "knowledge-tester",
        "permissions": list(permissions),
        "access_version": 1,
    }


def test_knowledge_record_normalizes_brand_and_list_semantics():
    assert normalize_knowledge_record(
        {"brand_name": "  Brand   Name  ", "list_type": "白名单", "notes": "  可用  "}
    ) == {
        "brand_name": "Brand Name",
        "list_type": "whitelist",
        "notes": "可用",
    }
    assert normalize_list_type("黑名单") == "blacklist"
    with pytest.raises(ValueError, match="品牌名称不能为空"):
        normalize_knowledge_record({"brand_name": "", "list_type": "whitelist"})
    with pytest.raises(ValueError, match="白名单或黑名单"):
        normalize_list_type("unknown")


def test_bulk_brand_lines_use_one_nonempty_unique_line_per_record():
    rows = parse_bulk_brand_lines(
        " Nike\n\nAdidas\n nike \nLEGO ",
        "黑名单",
        "批量导入",
    )

    assert [row["brand_name"] for row in rows] == ["Nike", "Adidas", "LEGO"]
    assert all(row["list_type"] == "blacklist" for row in rows)
    assert all(row["notes"] == "批量导入" for row in rows)


def test_infringement_knowledge_crud_api(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _user("infringement_knowledge.view", "infringement_knowledge.manage"),
    )
    monkeypatch.setattr(
        bit_interface,
        "db_list_infringement_knowledge",
        lambda **filters: calls.append(("list", filters))
        or {"summary": {"total": 0, "whitelist": 0, "blacklist": 0}, "rows": []},
    )
    monkeypatch.setattr(
        bit_interface,
        "db_create_infringement_knowledge",
        lambda record: calls.append(("create", record)) or {"id": 9},
    )
    monkeypatch.setattr(
        bit_interface,
        "db_update_infringement_knowledge",
        lambda record_id, record: calls.append(("update", record_id, record))
        or {"id": record_id},
    )
    monkeypatch.setattr(
        bit_interface,
        "db_delete_infringement_knowledge",
        lambda record_id: calls.append(("delete", record_id)) or {"id": record_id},
    )
    client = bit_interface.app.test_client()

    response = client.get(
        "/api/infringement-knowledge?list_type=whitelist&search=Brand"
    )
    assert response.status_code == 200
    assert calls[0][1]["list_type"] == "whitelist"
    assert calls[0][1]["search"] == "Brand"
    assert client.post(
        "/api/infringement-knowledge",
        json={"brand_name": "Brand", "list_type": "blacklist", "notes": "侵权"},
    ).get_json()["data"]["id"] == 9
    assert client.put(
        "/api/infringement-knowledge/9",
        json={"brand_name": "Brand", "list_type": "whitelist", "notes": "不侵权"},
    ).status_code == 200
    assert client.delete("/api/infringement-knowledge/9").status_code == 200
    assert [call[0] for call in calls] == ["list", "create", "update", "delete"]


def test_infringement_knowledge_write_requires_manage_permission(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _user("infringement_knowledge.view"),
    )
    response = bit_interface.app.test_client().post(
        "/api/infringement-knowledge",
        json={"brand_name": "Brand", "list_type": "blacklist"},
    )

    assert response.status_code == 403
    assert response.get_json()["required_permissions"] == [
        "infringement_knowledge.manage"
    ]


def test_infringement_knowledge_bulk_api(monkeypatch):
    captured = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _user("infringement_knowledge.view", "infringement_knowledge.manage"),
    )
    monkeypatch.setattr(
        bit_interface,
        "db_bulk_create_infringement_knowledge",
        lambda records: captured.extend(records)
        or {"total": len(records), "inserted": len(records), "restored": 0, "skipped": 0},
    )

    response = bit_interface.app.test_client().post(
        "/api/infringement-knowledge/bulk",
        json={
            "brands_text": "Nike\nAdidas\nNike",
            "list_type": "whitelist",
            "notes": "可用",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["inserted"] == 2
    assert [row["brand_name"] for row in captured] == ["Nike", "Adidas"]


def test_auto_analysis_blacklist_wins_and_writes_evidence():
    written = []

    def fake_extractor(titles, *, source_label, **_kwargs):
        return ["LEGO", "Generic"] if source_label == "侵权记录" else ["Nike", "LEGO"]

    result = analyze_knowledge_sources(
        {
            "infraction_rows": [
                {"title": "LEGO building blocks"},
                {"title": "LEGO hero set"},
            ],
            "active_rows": [
                {"title": "Nike sports cap"},
                {"title": "LEGO toy"},
            ],
        },
        writer=lambda records: written.extend(records) or {"inserted": len(records)},
        brand_extractor=fake_extractor,
    )

    assert normalize_brand_name("Generic") == ""
    assert result["blacklist_candidates"] == 1
    assert result["whitelist_candidates"] == 1
    assert [(row["brand_name"], row["list_type"]) for row in written] == [
        ("LEGO", "blacklist"),
        ("Nike", "whitelist"),
    ]
    assert written[0]["evidence_count"] == 2


def test_brand_extraction_retries_empty_ai_response():
    responses = iter(["", '[{"name":"Dixit"}]'])

    result = extract_brand_names(
        ["Juego de cartas Dixit"],
        source_label="测试",
        batch_size=10,
        chat_client=lambda *_args, **_kwargs: next(responses),
    )

    assert result == ["Dixit"]


def test_db_api_client_exposes_infringement_knowledge_routes(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {"id": 3},
    )

    bit_db_api.list_infringement_knowledge("blacklist", "Nike")
    bit_db_api.create_infringement_knowledge({"brand_name": "Nike"})
    bit_db_api.update_infringement_knowledge(3, {"brand_name": "Nike"})
    bit_db_api.delete_infringement_knowledge(3)
    bit_db_api.bulk_create_infringement_knowledge([{"brand_name": "Adidas"}])
    bit_db_api.get_infringement_knowledge_analysis_sources(100, 50)
    bit_db_api.upsert_analyzed_infringement_knowledge([{"brand_name": "LEGO"}])

    assert [(call[0], call[1]) for call in calls] == [
        ("GET", "/api/db/infringement-knowledge"),
        ("POST", "/api/db/infringement-knowledge"),
        ("PUT", "/api/db/infringement-knowledge/3"),
        ("DELETE", "/api/db/infringement-knowledge/3"),
        ("POST", "/api/db/infringement-knowledge/bulk"),
        ("GET", "/api/db/infringement-knowledge/analysis-sources"),
        ("POST", "/api/db/infringement-knowledge/analyzed"),
    ]


def test_workbench_contains_infringement_knowledge_management_page():
    source = (Path(__file__).parents[1] / "bit" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'data-tab="infringement-knowledge"' in source
    assert 'id="tab-infringement-knowledge"' in source
    assert "白名单 · 不侵权" in source
    assert "黑名单 · 侵权" in source
    assert 'id="infringement-knowledge-form"' in source
    assert "每一行会作为一条独立记录" in source
    assert 'id="start-infringement-analysis-btn"' in source
    assert "/api/infringement-knowledge" in source
