import copy
import json
from unittest.mock import MagicMock

import pytest

from bit import bit_db_api, bit_mysql


class KnowledgeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.connection.queries.append((sql, params))
        if sql.lstrip().startswith("UPDATE"):
            assert "`source_type` = 'wipo'" in sql
            assert "`deleted_at` IS NULL" in sql
            assert "COALESCE(`list_type`, '') <> 'blacklist'" in sql
            updated_at, organization = params
            self.rowcount = 0
            for row in self.connection.working.values():
                if (row.get("organization_key", "wuhan-zeshun") == organization
                        and row.get("source_type") == "wipo"
                        and row.get("deleted_at") is None
                        and row.get("list_type") != "blacklist"):
                    row.update(list_type="blacklist", updated_at=updated_at)
                    self.rowcount += 1
            return
        assert "FOR UPDATE" in sql
        self.rows = []
        assert "organization_key" in sql
        if " AS incoming" in sql:
            names, organization = params[:-1], params[-1]
        else:
            organization, names = params[0], params[1:]
        for index, name in enumerate(names):
            for row in self.connection.working.values():
                if row["brand_name"].casefold() == name.casefold() and row.get("organization_key", "wuhan-zeshun") == organization:
                    self.rows.append({**row, "input_index": index})

    def fetchall(self):
        return self.rows

    def executemany(self, sql, values):
        values = list(values)
        self.connection.writes.append((sql, values))
        if self.connection.fail_write == len(self.connection.writes):
            raise self.connection.failure
        if "infringement_knowledge_evidence" in sql:
            assert "INSERT IGNORE" in sql
            for row in values:
                self.connection.working_evidence.setdefault((row[0], row[4]), row)
            return
        assert "ON DUPLICATE KEY UPDATE" in sql
        # Execute the important SQL semantics against the transaction's copy.
        for name, notes, evidence, detail, created_at, updated_at, organization in values:
            existing = next((row for row in self.connection.working.values()
                             if row["brand_name"].casefold() == name.casefold() and row.get("organization_key", "wuhan-zeshun") == organization), None)
            if existing:
                if existing.get("deleted_at") is not None or (existing.get("source_type") or "manual") == "manual":
                    continue
                existing.update(list_type="blacklist", source_type="wipo",
                                source_detail=detail, deleted_at=None, updated_at=updated_at)
            else:
                record_id = max(self.connection.working, default=0) + 1
                self.connection.working[record_id] = {
                    "id": record_id, "brand_name": name, "list_type": "blacklist",
                    "source_type": "wipo", "source_detail": detail,
                    "notes": notes, "evidence_count": evidence, "organization_key": organization,
                    "created_at": created_at, "updated_at": updated_at, "deleted_at": None,
                }


class KnowledgeConnection:
    def __init__(self, stored, *, fail_write=None, failure=None, evidence_store=None):
        self.stored = stored
        self.working = copy.deepcopy(stored)
        self.evidence_store = evidence_store if evidence_store is not None else {}
        self.working_evidence = copy.deepcopy(self.evidence_store)
        self.queries = []
        self.writes = []
        self.fail_write = fail_write
        self.failure = failure or RuntimeError("write failed")
        self.commits = self.rollbacks = self.closed = self.begins = 0

    def cursor(self):
        return KnowledgeCursor(self)

    def begin(self):
        self.begins += 1

    def commit(self):
        self.commits += 1
        self.stored.clear()
        self.stored.update(copy.deepcopy(self.working))
        self.evidence_store.clear()
        self.evidence_store.update(copy.deepcopy(self.working_evidence))

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def _existing(record_id, brand_name, **changes):
    return {
        "id": record_id, "brand_name": brand_name,
        "list_type": "blacklist", "source_type": "wipo", "source_detail": "",
        "notes": "", "evidence_count": 0, "deleted_at": None,
        "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
        **changes,
    }


def _feed(name, registration="123", **changes):
    return {
        "brand_name": name, "list_type": "blacklist", "notes": "WIPO 同步",
        "evidence_count": 2,
        "source_detail": json.dumps({"source": "WIPO Global Brand Database",
                                     "registration": registration, "status": "Active",
                                     "country": "CN", "nice_classes": "25"}),
        **changes,
    }


def _connect(monkeypatch, stored, **connection_options):
    connections = []
    evidence_store = connection_options.pop("evidence_store", {})

    def factory(**_kwargs):
        connection = KnowledgeConnection(stored, evidence_store=evidence_store, **connection_options)
        connections.append(connection)
        return connection

    monkeypatch.setattr(bit_mysql.pymysql, "connect", factory)
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_knowledge_table", lambda _cursor: None)
    return connections


def test_wipo_import_blacklist_protects_human_and_deleted_records_and_is_idempotent(monkeypatch):
    stable_detail = bit_mysql._wipo_knowledge_source_detail(json.loads(_feed("Stable")["source_detail"]))
    stored = {
        1: _existing(1, "Brand Name", list_type="whitelist", source_type="manual",
                     notes="人工审核备注", evidence_count=7, source_detail="人工证据来源"),
        2: _existing(2, "Deleted", deleted_at="2026-01-02 00:00:00"),
        3: _existing(3, "Stable", list_type="pending", source_detail=stable_detail),
        4: _existing(4, "Absent", list_type="whitelist", source_type="manual"),
        5: _existing(5, "Legacy automatic", source_type="analysis", notes="保留原始证据", evidence_count=4),
    }
    before = copy.deepcopy(stored)
    connections = _connect(monkeypatch, stored)
    records = [_feed("  Brand   Name "), _feed("Deleted"), _feed("Stable"),
               _feed("Ａｌｐｈａ"), _feed("alpha"), _feed("Legacy automatic")]
    result = bit_mysql.upsert_wipo_infringement_knowledge(records)
    assert result == {"total": 5, "inserted": 1, "updated": 2, "unchanged": 0,
                      "restored": 0, "skipped_manual": 1, "skipped_deleted": 1}
    for key in (1, 2, 4):
        assert stored[key] == before[key]
    assert stored[3]["list_type"] == "blacklist" and stored[3]["source_detail"] == stable_detail
    assert stored[5]["list_type"] == "blacklist"
    assert stored[5]["notes"] == "保留原始证据" and stored[5]["evidence_count"] == 4
    assert stored[6]["brand_name"] == "Alpha" and stored[6]["list_type"] == "blacklist"
    assert stored[6]["notes"] == "WIPO 同步" and stored[6]["evidence_count"] == 2
    assert len(connections[0].queries) == 2
    sql = connections[0].writes[0][0].split("ON DUPLICATE KEY UPDATE", 1)[1]
    assert "`notes` =" not in sql and "`evidence_count` =" not in sql
    assert "`deleted_at` IS NULL" in sql and "<> 'manual'" in sql
    assert "'blacklist'" in sql and "'pending'" not in sql
    assert connections[0].commits == connections[0].closed == connections[0].begins == 1
    saved = copy.deepcopy(stored)
    assert bit_mysql.upsert_wipo_infringement_knowledge(records) == {
        "total": 5, "inserted": 0, "updated": 0, "unchanged": 3, "restored": 0,
        "skipped_manual": 1, "skipped_deleted": 1,
    }
    assert stored == saved
    assert not [sql for sql, _ in connections[1].writes if "ON DUPLICATE KEY UPDATE" in sql]
    assert connections[1].working_evidence == connections[0].working_evidence
    assert any(key[0] == 1 for key in connections[0].working_evidence)
    assert not any(key[0] == 2 for key in connections[0].working_evidence)


@pytest.mark.parametrize("requested_type", ["pending", "whitelist", "borderline", "blacklist"])
def test_wipo_writer_always_imports_blacklist(monkeypatch, requested_type):
    stored = {}
    connections = _connect(monkeypatch, stored)
    bit_mysql.upsert_wipo_infringement_knowledge([_feed("Automatic Brand", list_type=requested_type)])
    assert stored[1]["list_type"] == "blacklist"
    payload = json.loads(next(iter(connections[0].working_evidence.values()))[3])
    assert payload["list_type"] == "blacklist"


def test_wipo_migration_promotes_only_live_wipo_rows_in_current_enterprise_and_replays_safely(monkeypatch):
    from bit.knowledge_organization import scope

    stored = {
        1: _existing(1, "Pending", list_type="pending", organization_key="enterprise-a",
                     notes="原同步备注", source_detail="原注册证据", evidence_count=4),
        2: _existing(2, "Legacy white", list_type="whitelist", organization_key="enterprise-a"),
        3: _existing(3, "Black", organization_key="enterprise-a"),
        4: _existing(4, "Reviewed", list_type="whitelist", source_type="manual",
                     original_source_type="wipo", organization_key="enterprise-a"),
        5: _existing(5, "Deleted", list_type="pending", deleted_at="2026-01-02 00:00:00",
                     organization_key="enterprise-a"),
        6: _existing(6, "Analysis", list_type="pending", source_type="analysis",
                     organization_key="enterprise-a"),
        7: _existing(7, "Other enterprise", list_type="pending", organization_key="enterprise-b"),
    }
    before = copy.deepcopy(stored)
    connections = _connect(monkeypatch, stored)
    with scope("enterprise-a"):
        assert bit_mysql.migrate_wipo_blacklist() == 2
        after = copy.deepcopy(stored)
        assert bit_mysql.migrate_wipo_blacklist() == 0
    assert stored == after
    for key in (1, 2):
        assert stored[key]["list_type"] == "blacklist"
        assert {name: value for name, value in stored[key].items() if name not in ("list_type", "updated_at")} == {
            name: value for name, value in before[key].items() if name not in ("list_type", "updated_at")
        }
    for key in (3, 4, 5, 6, 7):
        assert stored[key] == before[key]
    assert all(connection.commits == connection.begins == connection.closed == 1 for connection in connections)


def test_wipo_migration_rolls_back_and_retries_deadlock(monkeypatch):
    connections = [MagicMock(), MagicMock()]
    cursors = [connection.cursor.return_value.__enter__.return_value for connection in connections]
    cursors[0].execute.side_effect = bit_mysql.pymysql.err.OperationalError(1213, "deadlock")
    cursors[1].rowcount = 9
    attempts = iter(connections)
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: next(attempts))
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_knowledge_table", lambda _cursor: None)
    assert bit_mysql.migrate_wipo_blacklist() == 9
    connections[0].rollback.assert_called_once()
    connections[0].commit.assert_not_called()
    connections[1].commit.assert_called_once()
    assert all(connection.close.call_count == 1 for connection in connections)


def test_wipo_migration_propagates_nonretryable_failure_after_rollback(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = RuntimeError("migration failed")
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: connection)
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_knowledge_table", lambda _cursor: None)
    with pytest.raises(RuntimeError, match="migration failed"):
        bit_mysql.migrate_wipo_blacklist()
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
    connection.close.assert_called_once()


def test_wipo_duplicate_brands_retain_distinct_registration_metadata(monkeypatch):
    stored = {}
    _connect(monkeypatch, stored)
    result = bit_mysql.upsert_wipo_infringement_knowledge(
        [_feed("BRAND", "123"), _feed("brand", "456", country="US")]
    )
    assert result["total"] == result["inserted"] == 1
    detail = json.loads(stored[1]["source_detail"])
    assert detail["registration"] == ["123", "456"]
    assert stored[1]["evidence_count"] == 4


@pytest.mark.parametrize("source,expected", [
    ("WIPO Madrid Monitor", "WIPO Madrid Monitor"),
    ("arbitrary source", "WIPO Global Brand Database"),
])
def test_wipo_import_preserves_only_supported_source_labels(monkeypatch, source, expected):
    stored = {}
    _connect(monkeypatch, stored)
    bit_mysql.upsert_wipo_infringement_knowledge([_feed("Madrid", source_detail=json.dumps({
        "source": source, "source_mode": "madrid", "registration": "AB-123",
        "source_url": "https://www.wipo.int/en/web/madrid-system/monitor/download",
    }))])
    detail = json.loads(stored[1]["source_detail"])
    assert detail["source"] == expected
    assert detail["registration"] == "AB-123" and detail["source_mode"] == "madrid"


def test_wipo_source_detail_keeps_valid_bounded_json_and_provenance():
    source = {"source": "WIPO Global Brand Database", "registration": "AB-123",
              "country": "CN", "status": "Active", "nice_classes": "1," * 2000,
              "detail": "内容" * 3000}
    detail = bit_mysql._wipo_knowledge_source_detail(source, _existing(
        1, "Name", source_type="manual", source_detail="原来源" * 1000,
    ))
    assert len(detail) <= 1000
    decoded = json.loads(detail)
    assert decoded["source"] == "WIPO Global Brand Database"
    assert decoded["registration"] == "AB-123"
    assert decoded["country"] == "CN" and decoded["status"] == "Active"
    assert decoded["detail_truncated"] is True


def test_wipo_batches_share_one_transaction_and_roll_back_partial_writes(monkeypatch):
    stored = {1: _existing(1, "Existing")}
    before = copy.deepcopy(stored)
    connections = _connect(monkeypatch, stored, fail_write=2)
    with pytest.raises(RuntimeError, match="write failed"):
        bit_mysql.upsert_wipo_infringement_knowledge(
            [_feed(f"Brand {index:04}") for index in range(501)]
        )
    assert len(connections[0].queries) == len(connections[0].writes) == 2
    assert stored == before
    assert connections[0].commits == 0
    assert connections[0].rollbacks == connections[0].closed == 1


def test_wipo_deadlock_retries_the_entire_transaction(monkeypatch):
    stored = {}
    first = KnowledgeConnection(stored, fail_write=1,
                                failure=bit_mysql.pymysql.err.OperationalError(1213, "deadlock"))
    second = KnowledgeConnection(stored)
    attempts = iter([first, second])
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: next(attempts))
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_knowledge_table", lambda _cursor: None)
    assert bit_mysql.upsert_wipo_infringement_knowledge([_feed("Brand")])["inserted"] == 1
    assert first.commits == 0 and first.rollbacks == first.closed == 1
    assert second.commits == second.closed == 1
    assert len(stored) == 1


def test_wipo_empty_or_invalid_batch_never_opens_database(monkeypatch):
    connect = MagicMock(side_effect=AssertionError("database must not open"))
    monkeypatch.setattr(bit_mysql.pymysql, "connect", connect)
    assert bit_mysql.upsert_wipo_infringement_knowledge([])["total"] == 0
    with pytest.raises(ValueError, match="品牌名称不能为空"):
        bit_mysql.upsert_wipo_infringement_knowledge([_feed(" ")])
    connect.assert_not_called()


def test_knowledge_cursor_pages_are_stable_and_query_one_extra_row(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [_existing(6, "One"), _existing(7, "Two"), _existing(8, "Three")]
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: connection)
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_knowledge_table", lambda _cursor: None)
    result = bit_mysql.list_infringement_knowledge("blacklist", "Brand", limit=2, after_id=5)
    assert [row["id"] for row in result["rows"]] == [6, 7]
    assert result["has_more"] is True and result["next_after_id"] == 7
    assert result["filtered_total"] == 2
    cursor.execute.assert_called_once()
    sql, params = cursor.execute.call_args.args
    assert "ORDER BY `id` ASC" in sql and "`id` > %s" in sql
    assert params == ("wuhan-zeshun", "blacklist", "%Brand%", "%Brand%", "%Brand%", "%Brand%", 5, 3)
    assert "COUNT(*)" not in sql

    cursor.fetchall.return_value = []
    result = bit_mysql.list_infringement_knowledge(limit=2, after_id=7)
    assert result["rows"] == [] and result["has_more"] is False
    assert result["next_after_id"] == 7


def test_knowledge_default_listing_preserves_ui_summary_and_sort(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {"total": 10, "whitelist": 3, "blacklist": 7}
    cursor.fetchall.return_value = [_existing(3, "Brand")]
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: connection)
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_knowledge_table", lambda _cursor: None)
    result = bit_mysql.list_infringement_knowledge(limit=2)
    assert result["summary"] == {"total": 10, "whitelist": 3, "blacklist": 7, "pending": 0, "borderline": 0}
    assert result["filtered_total"] == 1 and "has_more" not in result
    assert "ORDER BY `updated_at` DESC, `id` DESC" in cursor.execute.call_args.args[0]
    assert cursor.execute.call_args.args[1] == ("wuhan-zeshun", 2)


@pytest.mark.parametrize("after_id", [-1, "bad"])
def test_knowledge_invalid_cursor_is_rejected_before_database(monkeypatch, after_id):
    connect = MagicMock()
    monkeypatch.setattr(bit_mysql.pymysql, "connect", connect)
    with pytest.raises(ValueError, match="分页游标无效"):
        bit_mysql.list_infringement_knowledge(after_id=after_id)
    connect.assert_not_called()


def test_wipo_db_api_and_pagination_forwarding_preserve_legacy_arguments(monkeypatch):
    request = MagicMock(return_value={})
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(bit_db_api, "_request", request)
    bit_db_api.list_infringement_knowledge("blacklist", "Brand", 100)
    assert request.call_args.kwargs["params"] == {"list_type": "blacklist", "search": "Brand", "limit": 100}
    bit_db_api.list_infringement_knowledge(limit=5000, after_id=0)
    assert request.call_args.kwargs["params"]["after_id"] == 0
    records = [_feed("Brand")]
    bit_db_api.upsert_wipo_infringement_knowledge(records)
    request.assert_called_with("POST", "/api/db/infringement-knowledge/wipo/records",
                               json={"records": records}, timeout=120)

    local = MagicMock(return_value={})
    monkeypatch.setattr(bit_db_api, "DB_MODE", "mysql")
    monkeypatch.setattr(bit_db_api, "_local_call", local)
    bit_db_api.list_infringement_knowledge()
    local.assert_called_with("list_infringement_knowledge", list_type="", search="", limit=2000)
    bit_db_api.list_infringement_knowledge(after_id=9)
    assert local.call_args.kwargs["after_id"] == 9
    bit_db_api.upsert_wipo_infringement_knowledge(records)
    local.assert_called_with("upsert_wipo_infringement_knowledge", records)


def test_review_pagination_counts_filtered_rows_and_maps_legacy_automatic_status(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [
        {"total": 90, "pending": 80, "blacklist": 6, "whitelist": 4}, {"total": 51},
    ]
    cursor.fetchall.return_value = [_existing(3, "Legacy", source_type="analysis",
                                             source_detail="global_ip:wikidata:Q123")]
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_knowledge_table", lambda cursor: None)
    result = bit_mysql.list_infringement_knowledge(
        list_type="pending", source_type="wikidata", search="Legacy", page=99, page_size=50)
    assert result["page"] == result["pages"] == 2
    assert result["filtered_total"] == 51 and not result["has_more"]
    assert result["summary"]["pending"] == 80
    assert result["rows"][0]["list_type"] == "pending"
    assert result["rows"][0]["source_type"] == "wikidata"
    count_sql, count_params = cursor.execute.call_args_list[1].args
    rows_sql, rows_params = cursor.execute.call_args_list[2].args
    assert "ELSE 'pending'" in count_sql and "THEN 'wikidata'" in count_sql
    assert count_params == ("wuhan-zeshun", "pending", "wikidata", "%Legacy%", "%Legacy%", "%Legacy%", "%Legacy%")
    assert rows_params == (*count_params, 50, 50)
    assert "LIMIT %s OFFSET %s" in rows_sql


@pytest.mark.parametrize("options", [
    {"page": 0}, {"page": "bad"}, {"page": 1, "page_size": 0},
    {"page": 1, "after_id": 0}, {"source_type": "unknown"},
])
def test_bad_review_filters_do_not_open_database(monkeypatch, options):
    connect = MagicMock()
    monkeypatch.setattr(bit_mysql.pymysql, "connect", connect)
    with pytest.raises(ValueError):
        bit_mysql.list_infringement_knowledge(**options)
    connect.assert_not_called()


def test_analysis_writer_forces_pending_and_protects_manual_deleted_and_other_sources(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [
        _existing(1, "Manual", source_type="manual", list_type="whitelist"),
        _existing(2, "Deleted", source_type="analysis", deleted_at="2026-01-01"),
        _existing(3, "Wipo", source_type="wipo"),
        _existing(4, "Update", source_type="analysis"), None,
    ]
    cursor.rowcount = 1
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_knowledge_table", lambda cursor: None)
    result = bit_mysql.upsert_analyzed_infringement_knowledge([
        {"brand_name": name, "list_type": "whitelist"} for name in ["Manual", "Deleted", "Wipo", "Update", "New"]
    ])
    assert result["inserted"] == result["updated"] == 1
    assert result["skipped_manual"] == result["skipped_deleted"] == result["skipped_source"] == 1
    writes = [call.args for call in cursor.execute.call_args_list if not call.args[0].lstrip().startswith("SELECT")]
    assert len(writes) == 2
    assert writes[0][1][0] == "pending" and writes[1][1][1] == "pending"
    assert "ON DUPLICATE KEY UPDATE `id` = `id`" in writes[1][0]
    connection.commit.assert_called_once()


def test_manual_review_retains_source_evidence(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {"id": 1}
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_knowledge_table", lambda cursor: None)
    bit_mysql.update_infringement_knowledge(1, {
        "brand_name": "Reviewed Brand", "list_type": "blacklist", "notes": "核实后的处理依据",
    })
    sql, params = next(call.args for call in cursor.execute.call_args_list
                       if "SET `brand_name`" in call.args[0])
    assert "`source_type` = 'manual'" in sql
    assert "source_detail" not in sql and "evidence_count" not in sql
    assert params[:3] == ("Reviewed Brand", "blacklist", "核实后的处理依据")


def test_same_feed_and_evidence_are_independent_per_enterprise(monkeypatch):
    from bit.knowledge_organization import scope
    stored = {}
    evidence = {}
    _connect(monkeypatch, stored, evidence_store=evidence)
    with scope("enterprise-a"):
        assert bit_mysql.upsert_wipo_infringement_knowledge([_feed("Shared Name")])["inserted"] == 1
    with scope("enterprise-b"):
        assert bit_mysql.upsert_wipo_infringement_knowledge([_feed("Shared Name")])["inserted"] == 1
        assert bit_mysql.upsert_wipo_infringement_knowledge([_feed("Shared Name")])["unchanged"] == 1
    assert len(stored) == 2
    assert {row["organization_key"] for row in stored.values()} == {"enterprise-a", "enterprise-b"}
    assert {key[0] for key in evidence} == set(stored)
