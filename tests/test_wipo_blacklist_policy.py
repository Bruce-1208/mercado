"""WIPO import policy must agree in matching, presentation and SQL filters."""
import sqlite3

import pytest

from bit import bit_check_risk, bit_mysql
from bit.bit_infringement_knowledge import effective_list_type, knowledge_rule_matches
from bit.infringement_state import knowledge_version


@pytest.mark.parametrize("source", [None, "", "manual", "wipo", "analysis", "wikidata", "detection"])
@pytest.mark.parametrize("list_type", ["blacklist", "whitelist", "pending", "borderline"])
def test_effective_policy_matches_sql_and_serialized_status(source, list_type):
    row = {"id": 1, "brand_name": "Registered Mark", "source_type": source, "list_type": list_type}
    expected = list_type if not source or source == "manual" else (
        "blacklist" if source == "wipo" and list_type == "blacklist" else "pending")
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE knowledge (source_type TEXT, list_type TEXT)")
        db.execute("INSERT INTO knowledge VALUES (?, ?)", (source, list_type))
        sql_type = db.execute(f"SELECT {bit_mysql._KNOWLEDGE_EFFECTIVE_TYPE_SQL} FROM knowledge").fetchone()[0]
    assert sql_type == effective_list_type(row) == expected
    assert bit_mysql._serialize_infringement_knowledge_row(row)["list_type"] == expected


def test_blacklist_counts_and_filter_pages_include_wipo_but_other_sources_remain_pending():
    rows = [("wipo", "blacklist"), ("wipo", "blacklist"), ("manual", "blacklist"),
            ("analysis", "blacklist"), ("wikidata", "blacklist"), ("manual", "whitelist")]
    expression = bit_mysql._KNOWLEDGE_EFFECTIVE_TYPE_SQL
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE knowledge (source_type TEXT, list_type TEXT)")
        db.executemany("INSERT INTO knowledge VALUES (?, ?)", rows)
        counts = dict(db.execute(f"SELECT {expression}, COUNT(*) FROM knowledge GROUP BY {expression}"))
        page = db.execute(f"SELECT source_type FROM knowledge WHERE ({expression}) = ? ORDER BY rowid LIMIT 1 OFFSET 1",
                          ("blacklist",)).fetchall()
    assert counts == {"blacklist": 3, "pending": 2, "whitelist": 1}
    assert page == [("wipo",)]


def test_wipo_blacklist_obeys_scope_expiry_and_deletion():
    rule = {"id": 8, "brand_name": "Registered Mark", "source_type": "wipo", "list_type": "blacklist"}
    assert knowledge_rule_matches(rule)
    assert not knowledge_rule_matches({**rule, "deleted_at": "2026-10-01"})
    assert not knowledge_rule_matches({**rule, "valid_until": "2000-01-01"})
    scoped = {**rule, "scope": {"mode": "scoped", "sites": ["MLM"], "categories": [], "shops": []}}
    assert knowledge_rule_matches(scoped, site="MLM")
    assert not knowledge_rule_matches(scoped, site="MLB")
    assert not knowledge_rule_matches({**rule, "list_type": "whitelist"})


def test_wipo_blacklist_title_and_alias_hits_block_without_ai_and_change_rule_version(monkeypatch):
    rule = {"id": 8, "brand_name": "Registered Mark", "aliases": ["Registered Alias"],
            "source_type": "wipo", "list_type": "blacklist"}
    candidate = {"row_id": 9, "source_type": "product_list", "title": "Registered Alias storage box"}
    monkeypatch.setattr(bit_check_risk, "classify_risk_records",
                        lambda *args, **kwargs: pytest.fail("WIPO title match should already be blocked"))
    result = bit_check_risk.scan_products(candidate_reader=lambda **kwargs: [candidate],
        knowledge_records=[rule], dry_run=True, sources=["product_list"])
    assert result["risk_2"] == 1 and result["failed"] == 0
    assert result["results"][0]["keywords"] == ["Registered Alias"]
    assert result["results"][0]["matched_rule_ids"] == [8]
    assert knowledge_version([rule]) != knowledge_version([{**rule, "list_type": "pending"}])
