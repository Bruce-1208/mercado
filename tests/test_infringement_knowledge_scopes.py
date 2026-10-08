import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from bit import bit_mysql
from bit.bit_infringement_knowledge import (
    knowledge_rule_matches, knowledge_rule_names, normalize_knowledge_record,
    normalize_rule_scope, normalize_valid_until,
)
from bit.infringement_knowledge_store import (
    append_evidence, get_detail, get_products, prepare_manual_record,
    save_manual_metadata, serialize_metadata, submit_candidate,
)


def rule(**fields):
    return {"id": 7, "brand_name": "Sample Brand", "source_type": "manual",
            "list_type": "whitelist", "notes": "已核实授权", "rule_version": 3, **fields}


def offline_database(monkeypatch, existing=None):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = existing
    cursor.lastrowid = 8
    cursor.rowcount = 1
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_knowledge_table", lambda cursor: None)
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_risk_checks_table", lambda cursor: None)
    return connection, cursor


def test_aliases_and_explicit_scopes_normalize_without_expanding_scope():
    record = normalize_knowledge_record({"brand_name": "Sample", "list_type": "pending",
        "aliases": " Alias A\nalias a\nAlias B ",
        "scope": {"mode": "scoped", "sites": "mlm, MLB", "categories": ["MLM100"], "shops": [18]},
        "valid_until": "2026-10-06", "reviewed_by": "reviewer", "rule_version": 2})
    assert record["aliases"] == ["Alias A", "Alias B"]
    assert record["scope"] == {"mode": "scoped", "sites": ["MLM", "MLB"],
                               "categories": ["MLM100"], "shops": ["18"]}
    assert record["valid_until"] == "2026-10-06"
    assert knowledge_rule_names({**record, "aliases": ["Sample", "Alias A"]}) == ["Sample", "Alias A"]


@pytest.mark.parametrize("scope", [None, {}, {"mode": "scoped"},
    {"mode": "global", "shops": [18]}, {"mode": "scoped", "sites": {"MLM": True}}])
def test_new_scope_is_explicit_and_unambiguous(scope):
    with pytest.raises(ValueError):
        normalize_rule_scope(scope)


def test_legacy_global_remains_effective_but_auto_and_invalid_records_do_not():
    assert knowledge_rule_matches(rule())
    assert serialize_metadata(rule())["scope_label"] == "企业内通用规则（历史记录）"
    assert not knowledge_rule_matches(rule(source_type="analysis"))
    assert not knowledge_rule_matches(rule(list_type="pending"))
    assert not knowledge_rule_matches(rule(scope_json="{broken"))
    assert not knowledge_rule_matches(rule(deleted_at="2026-10-01"))


def test_scoped_matching_uses_or_within_and_between_dimensions():
    scoped = rule(scope={"mode": "scoped", "sites": ["MLM", "MLB"],
        "categories": ["MLM100"], "shops": ["18", "25"]})
    assert knowledge_rule_matches(scoped, site="mlm", category="MLM100", shop=18)
    assert not knowledge_rule_matches(scoped, site="MLM", category="MLM100", shop=40)
    assert not knowledge_rule_matches(scoped, site="MLM", category="other", shop=18)
    assert not knowledge_rule_matches(scoped, category="MLM100", shop=18)
    assert not knowledge_rule_matches(scoped, site="MLM", shop=18)


def test_expiry_is_inclusive_and_malformed_dates_fail_closed():
    expired = rule(valid_until="2026-10-06")
    assert knowledge_rule_matches(expired, now=date(2026, 10, 6))
    assert not knowledge_rule_matches(expired, now=date(2026, 10, 7))
    assert not knowledge_rule_matches(rule(valid_until="bad"))
    with pytest.raises(ValueError):
        normalize_valid_until("20261006")


def test_manual_decision_requires_basis_and_scope_and_detects_version_conflicts():
    with pytest.raises(ValueError, match="处理依据"):
        prepare_manual_record(rule(notes=""))
    with pytest.raises(ValueError, match="明确适用范围"):
        prepare_manual_record(rule())
    pending = rule(source_type="analysis", list_type="pending")
    with pytest.raises(ValueError, match="明确适用范围"):
        prepare_manual_record(rule(), pending)
    with pytest.raises(ValueError, match="其他用户更新"):
        prepare_manual_record(rule(rule_version=2), rule())
    # Editing a legacy manual rule without scope preserves its current scope.
    assert prepare_manual_record(rule(), rule())["rule_version"] == 3


def test_manual_review_preserves_original_source_and_records_actor_and_version():
    cursor = MagicMock()
    before = rule(source_type="wipo", list_type="pending", source_detail="registration 123", evidence_count=2)
    update = {"brand_name": "Sample Brand", "list_type": "blacklist", "notes": "核实投诉",
        "scope": {"mode": "scoped", "sites": ["MLM"], "categories": [], "shops": []},
        "aliases": ["Sample"], "reviewed_by": "operator", "rule_version": 3}
    assert save_manual_metadata(cursor, 7, update, before, "2026-10-06 10:00:00") == 4
    calls = [item.args for item in cursor.execute.call_args_list]
    evidence = next(params for sql, params in calls if "knowledge_evidence" in sql)
    assert evidence[0:3] == (7, "wipo", "registration 123")
    history = next(params for sql, params in calls if "knowledge_history" in sql)
    assert history[0:4] == (7, 4, "review", "operator")
    after = json.loads(history[5])
    assert after["source_type"] == "manual" and after["original_source_type"] == "wipo"
    assert after["source_detail"] == "registration 123"
    assert json.loads(after["scope_json"])["sites"] == ["MLM"]


def test_evidence_fingerprint_is_stable_and_keeps_complete_source_payload():
    cursor = MagicMock()
    payload = {"source_type": "detection", "source_detail": "product title hit",
               "product_id": "MLM123", "title": "Sample Brand product", "matched_rule_id": 7}
    append_evidence(cursor, 7, payload, "2026-10-06 10:00:00")
    append_evidence(cursor, 7, payload, "2026-10-06 11:00:00")
    first = cursor.execute.call_args_list[0].args[1]
    second = cursor.execute.call_args_list[1].args[1]
    assert first[4] == second[4] and first[5] != second[5]
    assert json.loads(first[3])["product_id"] == "MLM123"
    assert all("INSERT IGNORE" in call.args[0] for call in cursor.execute.call_args_list)


def test_candidate_never_overwrites_existing_manual_decision(monkeypatch):
    connection, cursor = offline_database(monkeypatch, rule())
    result = submit_candidate({"brand_name": "Sample Brand", "list_type": "blacklist",
        "notes": "新增检测线索", "source_detail": "标题命中"}, actor="reviewer")
    assert result == {"id": 7, "list_type": "whitelist", "candidate_status": "pending", "preserved_manual": True}
    mutations = [sql for sql, *_ in (call.args for call in cursor.execute.call_args_list)
                 if not sql.lstrip().startswith("SELECT")]
    assert len(mutations) == 1 and "knowledge_evidence" in mutations[0]
    connection.commit.assert_called_once()


def test_new_candidate_is_forced_pending_and_deleted_brand_is_not_restored(monkeypatch):
    connection, cursor = offline_database(monkeypatch)
    result = submit_candidate({"brand_name": "New Brand", "list_type": "whitelist"}, actor="reviewer")
    assert result["list_type"] == result["candidate_status"] == "pending"
    insert = next(call.args for call in cursor.execute.call_args_list
                  if "INSERT INTO `infringement_knowledge`" in call.args[0])
    assert "'pending'" in insert[0]
    connection, cursor = offline_database(monkeypatch, rule(deleted_at="2026-10-01"))
    with pytest.raises(ValueError, match="已删除"):
        submit_candidate({"brand_name": "Sample Brand"})
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_detail_decodes_independent_evidence_and_version_history(monkeypatch):
    connection, cursor = offline_database(monkeypatch, rule(source_detail="original source"))
    cursor.fetchall.side_effect = [
        [{"id": 1, "payload_json": '{"product_id":"MLM123"}', "created_at": "2026-10-06"}],
        [{"id": 2, "before_json": '{"list_type":"pending"}', "after_json": '{"list_type":"whitelist"}', "created_at": "2026-10-06"}],
    ]
    result = get_detail(7)
    assert result["record"]["scope"]["mode"] == "global"
    assert result["evidence"][0]["payload"]["product_id"] == "MLM123"
    assert result["history"][0]["before"]["list_type"] == "pending"
    assert result["history"][0]["after"]["list_type"] == "whitelist"


def test_related_query_uses_rule_ids_and_authorized_accounts(monkeypatch):
    connection, cursor = offline_database(monkeypatch, rule(aliases_json='["Sample Alias"]'))
    cursor.fetchall.return_value = [{"source_type": "collection_list", "source_row_id": 20,
        "details_json": '{"matched_rule_ids":[7]}', "association": "matched_rule"}]
    result = get_products(7, limit=1, token_ids=[18])
    sql, params = cursor.execute.call_args.args
    assert "matched_rule_ids" in sql and "JSON_VALID" in sql and "`token_id` IN (%s)" in sql
    assert sql.count("%s") == len(params)
    assert params[:4] == ("7", '"7"', "7", '"7"') and params[-2:] == (18, 2)
    assert result["rows"][0]["record_key"] == "collection_list:20"
    assert result["rows"][0]["details"]["matched_rule_ids"] == [7]
