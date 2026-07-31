import json

import pytest

from bit import bit_check_risk
from bit import bit_mysql


def test_normalize_ai_results_supports_three_levels_and_keywords():
    records = [{"row_id": 10}, {"row_id": 11}, {"row_id": 12}]
    payload = [
        {"row_id": 10, "risk_level": 0, "keywords": [], "reason": "通用商品"},
        {
            "row_id": 11,
            "risk_level": 1,
            "keywords": ["Apple", "apple"],
            "reason": "兼容性表述",
        },
        {
            "row_id": 12,
            "risk_level": 2,
            "brands": "Pokemon, Pikachu",
            "reason": "角色周边",
        },
    ]

    results = bit_check_risk._normalize_ai_results(payload, records)

    assert [item["risk_level"] for item in results] == [0, 1, 2]
    assert results[0]["keywords"] == []
    assert results[1]["keywords"] == ["Apple"]
    assert results[2]["keywords"] == ["Pokemon", "Pikachu"]


def test_normalize_ai_results_rejects_missing_rows():
    with pytest.raises(ValueError, match="AI 未返回"):
        bit_check_risk._normalize_ai_results(
            [{"row_id": 1, "risk_level": 0, "keywords": []}],
            [{"row_id": 1}, {"row_id": 2}],
        )


def test_classify_risk_records_sends_only_title_to_ai(monkeypatch):
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return json.dumps(
            [
                {
                    "row_id": 5,
                    "risk_level": 2,
                    "keywords": ["Pokemon"],
                    "reason": "标题命中作品名",
                }
            ],
            ensure_ascii=False,
        )

    monkeypatch.setattr(bit_check_risk, "chat_deepseek", fake_chat)
    results = bit_check_risk.classify_risk_records(
        [
            {
                "row_id": 5,
                "product_id": "793693876",
                "title": "Pokemon juego de cartas",
                "image_ocr_text": "BOHNANZA",
                "main_image_url": "https://example.test/logo.jpg",
                "product_category": "Trading Card Games",
                "zying_category": "玩具/卡牌",
            }
        ],
        retries=0,
    )

    assert results[0]["risk_level"] == 2
    assert "Pokemon juego de cartas" in captured["messages"][1]["content"]
    assert "BOHNANZA" not in captured["messages"][1]["content"]
    assert "logo.jpg" not in captured["messages"][1]["content"]
    assert captured["kwargs"]["temperature"] == 0


def test_scan_products_filters_category_and_writes_all_risk_levels(monkeypatch):
    candidates = [
        {"row_id": 101, "product_id": "A", "title": "Generic backpack"},
        {"row_id": 102, "product_id": "B", "title": "for iPhone case"},
        {"row_id": 103, "product_id": "C", "title": "Pokemon Pikachu plush"},
    ]
    captured = {}

    def get_candidates(**kwargs):
        captured["query"] = kwargs
        return candidates

    monkeypatch.setattr(bit_check_risk, "get_zying_risk_candidates", get_candidates)
    monkeypatch.setattr(
        bit_check_risk,
        "classify_risk_records",
        lambda records, model, retries: [
            {
                "row_id": record["row_id"],
                "risk_level": index,
                "keywords": [] if index == 0 else ["Apple" if index == 1 else "Pokemon"],
                "reason": "test",
            }
            for index, record in enumerate(records)
        ],
    )

    def update(results):
        captured["results"] = results
        return len(results)

    monkeypatch.setattr(bit_check_risk, "update_zying_product_risks", update)
    logs = []

    summary = bit_check_risk.scan_products(
        zying_category="玩具类",
        hours=0,
        recheck=True,
        batch_size=20,
        log_callback=logs.append,
    )

    assert captured["query"]["zying_category"] == "玩具类"
    assert captured["query"]["include_checked"] is True
    assert [item["risk_level"] for item in captured["results"]] == [0, 1, 2]
    assert summary == {
        "checked": 3,
        "risk_0": 1,
        "risk_1": 1,
        "risk_2": 1,
        "updated": 3,
        "results": captured["results"],
    }
    assert any("开始标题审核批次 1/1" in line for line in logs)
    assert any("风险审核完成" in line for line in logs)


def test_update_zying_product_risks_clears_keywords_for_level_zero(monkeypatch):
    calls = []

    class Cursor:
        rowcount = 2

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def executemany(self, sql, params):
            calls.append((sql, params))

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: Connection())
    monkeypatch.setattr(bit_mysql, "_ensure_zying_product_table", lambda cursor: None)

    updated = bit_mysql.update_zying_product_risks(
        [
            {"row_id": 7, "risk_level": 0, "keywords": ["old"]},
            {"row_id": 8, "risk_level": 2, "keywords": ["Nike", "Swoosh"]},
        ]
    )

    assert updated == 2
    assert calls[0][1] == [("0", None, 7), ("2", "Nike, Swoosh", 8)]
    assert "commit" in calls


def test_get_zying_risk_candidates_filters_zying_category(monkeypatch):
    captured = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self):
            return [{"row_id": 9}]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: Connection())
    monkeypatch.setattr(bit_mysql, "_ensure_zying_product_table", lambda cursor: None)

    rows = bit_mysql.get_zying_risk_candidates(
        hours=0,
        limit=5,
        zying_category="玩具类",
    )

    assert rows == [{"row_id": 9}]
    assert "`智赢分类编号` = %s" in captured["sql"]
    assert "`智赢产品分类` LIKE %s" in captured["sql"]
    assert "NOT IN ('0', '1', '2')" in captured["sql"]
    assert captured["params"] == ("玩具类", "玩具类", "%/玩具类", 5)
    assert captured["closed"] is True


def test_get_zying_risk_results_filters_and_uses_whitelisted_sort(monkeypatch):
    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql, params):
            calls.append((sql, params))

        def fetchone(self):
            return {"total": 2, "risk_0": 0, "risk_1": 1, "risk_2": 1, "unchecked": 0}

        def fetchall(self):
            return [{"row_id": 9, "risk_level": "2"}]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: Connection())
    monkeypatch.setattr(bit_mysql, "_ensure_zying_product_table", lambda cursor: None)

    result = bit_mysql.get_zying_risk_results(
        zying_category="玩具类",
        risk_level="2",
        search="Pokemon",
        sort_by="submitted_at; DROP TABLE zying_product",
        sort_dir="asc",
        limit=200,
    )

    assert result["total"] == 2
    assert result["rows"] == [{"row_id": 9, "risk_level": "2"}]
    assert "DROP TABLE" not in calls[1][0]
    assert "CAST(COALESCE" in calls[1][0]
    assert calls[1][1][-1] == 200
