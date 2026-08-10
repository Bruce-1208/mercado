from pathlib import Path

from bit import bit_appeal_ai, bit_interface
from bit.bit_appeal_phrases import (
    default_phrase_rows,
    render_appeal_phrase,
    use_appeal_phrase,
)


def _user(*permissions):
    return {
        "id": 1,
        "username": "appeal-tester",
        "permissions": list(permissions),
        "access_version": 1,
    }


def test_existing_phrases_are_seeded_and_grouped_by_appeal_type():
    rows = default_phrase_rows()
    counts = {
        appeal_type: sum(1 for row in rows if row["appeal_type"] == appeal_type)
        for appeal_type in ("延误", "侵权", "取消率", "投诉")
    }

    assert counts == {"延误": 3, "侵权": 4, "取消率": 3, "投诉": 2}
    assert len({row["source_key"] for row in rows}) == len(rows)


def test_phrase_renderer_replaces_placeholders_without_duplicating_order_ids():
    assert render_appeal_phrase(
        "你好，我叫{nickname}，订单为 {order_ids}",
        nickname="Lucy",
        order_ids="1001、1002",
        appeal_type="取消率",
    ) == "你好，我叫Lucy，订单为 1001、1002"
    assert render_appeal_phrase(
        "请复核",
        order_ids="1001、1002",
        appeal_type="投诉",
    ) == "销售单号：1001、1002\n请复核"


def test_ai_delay_uses_phrase_selected_from_library(monkeypatch):
    sent_messages = []
    monkeypatch.setattr(
        bit_appeal_ai,
        "get_delay_orders_download_list",
        lambda *args: ["1001", "1002"],
    )
    monkeypatch.setattr(bit_appeal_ai, "open_ai_contact_window", lambda *args: None)
    monkeypatch.setattr(bit_appeal_ai, "safe_get_agent_messages", lambda driver: [])
    monkeypatch.setattr(
        bit_appeal_ai,
        "send_ai_chat_message",
        lambda driver, message: sent_messages.append(message),
    )
    monkeypatch.setattr(
        bit_appeal_ai,
        "wait_for_ai_agent_reply",
        lambda *args, **kwargs: ("完成", ["完成"]),
    )
    monkeypatch.setattr(bit_appeal_ai, "append_chat_log", lambda *args, **kwargs: None)

    with use_appeal_phrase("我是{nickname}，请处理这些延误订单"):
        bit_appeal_ai.handle_delay(
            "window-id",
            object(),
            "测试店铺",
            "墨西哥",
            "",
            "Bruce",
        )

    assert sent_messages == ["1001、1002我是Bruce，请处理这些延误订单"]


def test_appeal_phrase_crud_api(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _user("appeal.view", "appeal.execute"),
    )
    monkeypatch.setattr(
        bit_interface,
        "db_list_appeal_phrases",
        lambda: {"summary": [], "rows": [], "total": 0},
    )
    monkeypatch.setattr(
        bit_interface,
        "db_create_appeal_phrase",
        lambda record: calls.append(("create", record)) or {"id": 7},
    )
    monkeypatch.setattr(
        bit_interface,
        "db_update_appeal_phrase",
        lambda phrase_id, record: calls.append(("update", phrase_id, record)) or {"id": phrase_id},
    )
    monkeypatch.setattr(
        bit_interface,
        "db_delete_appeal_phrase",
        lambda phrase_id: calls.append(("delete", phrase_id)) or {"id": phrase_id},
    )
    client = bit_interface.app.test_client()

    assert client.get("/api/appeal-phrases").status_code == 200
    assert client.post(
        "/api/appeal-phrases",
        json={"appeal_type": "延误", "content": "新增话术", "is_active": True},
    ).get_json()["data"]["id"] == 7
    assert client.put(
        "/api/appeal-phrases/7",
        json={"appeal_type": "侵权", "content": "修改话术", "is_active": False},
    ).status_code == 200
    assert client.delete("/api/appeal-phrases/7").status_code == 200
    assert [call[0] for call in calls] == ["create", "update", "delete"]


def test_appeal_page_contains_phrase_library_management():
    source = (Path(__file__).parents[1] / "bit" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "申诉话术库" in source
    assert "add-appeal-phrase-btn" in source
    assert 'fetch("/api/appeal-phrases"' in source
    assert "留空时按申诉类型从话术库随机选择" in source
