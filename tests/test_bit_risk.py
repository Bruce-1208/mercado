import json

from bit import bit_risk


def test_extract_json_payload_accepts_markdown_fence():
    payload = bit_risk._extract_json_payload(
        '```json\n[{"row_id": 7, "suspected": true, "reason": "品牌"}]\n```'
    )

    assert payload[0]["row_id"] == 7
    assert payload[0]["suspected"] is True


def test_normalize_ai_results_preserves_database_order_and_missing_rows():
    records = [{"row_id": 10}, {"row_id": 11}]
    payload = [{"row_id": "11", "suspected": "是", "reason": "图片有 Logo"}]

    results = bit_risk._normalize_ai_results(payload, records)

    assert results == [
        {
            "row_id": 10,
            "suspected": False,
            "reason": "AI 未返回该行，未标记",
        },
        {"row_id": 11, "suspected": True, "reason": "图片有 Logo"},
    ]


def test_scan_recent_products_only_marks_suspected_rows(monkeypatch):
    candidates = [
        {
            "row_id": 101,
            "product_id": "A",
            "title": "Generic backpack",
            "main_image_url": "https://example.test/a.jpg",
        },
        {
            "row_id": 102,
            "product_id": "B",
            "title": "Michael Jackson plush",
            "main_image_url": "https://example.test/b.jpg",
        },
    ]
    marked = []

    monkeypatch.setattr(
        bit_risk,
        "get_zying_risk_candidates",
        lambda hours, limit: candidates,
    )
    monkeypatch.setattr(
        bit_risk,
        "enrich_records_with_ocr",
        lambda records, workers, min_confidence: [
            dict(record, image_ocr_text="MICHAEL JACKSON" if record["row_id"] == 102 else "")
            for record in records
        ],
    )
    monkeypatch.setattr(
        bit_risk,
        "classify_risk_records",
        lambda records, model: [
            {
                "row_id": record["row_id"],
                "suspected": record["row_id"] == 102,
                "reason": "名人 IP" if record["row_id"] == 102 else "通用商品",
            }
            for record in records
        ],
    )

    def mark(row_ids):
        marked.extend(row_ids)
        return len(row_ids)

    monkeypatch.setattr(bit_risk, "mark_zying_products_suspected", mark)

    summary = bit_risk.scan_recent_products(hours=24, batch_size=20)

    assert marked == [102]
    assert summary["checked"] == 2
    assert summary["suspected"] == 1
    assert summary["updated"] == 1


def test_classify_risk_records_builds_title_and_ocr_payload(monkeypatch):
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return json.dumps(
            [{"row_id": 5, "suspected": True, "reason": "OCR 命中 BOHNANZA"}],
            ensure_ascii=False,
        )

    monkeypatch.setattr(bit_risk, "chat_deepseek", fake_chat)
    results = bit_risk.classify_risk_records(
        [
            {
                "row_id": 5,
                "product_id": "793693876",
                "title": "Clásico juego de cartas",
                "image_ocr_text": "BOHNANZA",
                "product_category": "Trading Card Games",
            }
        ]
    )

    assert results[0]["suspected"] is True
    assert "BOHNANZA" in captured["messages"][1]["content"]
    assert captured["kwargs"]["temperature"] == 0
