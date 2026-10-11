"""Legacy risk reads must not prevent new product snapshots from being saved."""
import os
import uuid
from unittest.mock import MagicMock

import pytest

from bit import bit_mysql


def test_candidate_stream_releases_backfill_writes_before_first_yield(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{"source_row_id": 7, "title": "product", "product_id": "p7"}]
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kw: connection)
    monkeypatch.setattr(bit_mysql, "_ensure_zying_product_table", lambda c: None)
    monkeypatch.setattr(bit_mysql, "_ensure_infringement_risk_checks_table", lambda c: None)
    def backfill(c):
        connection.commit.assert_not_called()
    monkeypatch.setattr(bit_mysql, "_backfill_legacy_zying_risk_checks", backfill)
    stream = bit_mysql.get_infringement_risk_candidates(sources=["zying"], include_checked=True, _stream=True)
    try:
        assert next(stream)["product_id"] == "p7"
        connection.commit.assert_called_once()
    finally:
        stream.close()
    connection.close.assert_called_once()


@pytest.mark.skipif(os.environ.get("ZYING_MYSQL_LOCK_TEST") != "1", reason="opt-in MySQL concurrency test")
def test_legacy_backfill_allows_concurrent_snapshot_insert_and_preserves_reviews():
    # Disposable tables: never read or modify real product/review records.
    source, target = ["test_zying_lock_" + uuid.uuid4().hex for _ in range(2)]
    reader = bit_mysql._pymysql_driver.connect(**bit_mysql.config)
    writer = bit_mysql._pymysql_driver.connect(**bit_mysql.config)
    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def execute(self, sql, params=None):
            sql = sql.replace('`zying_product`', f'`{source}`').replace('`infringement_risk_checks`', f'`{target}`')
            return self.cursor.execute(sql, params)
        def executemany(self, sql, rows):
            return self.cursor.executemany(sql.replace('`infringement_risk_checks`', f'`{target}`'), rows)
        def fetchall(self):
            return self.cursor.fetchall()
    try:
        with reader.cursor() as c:
            c.execute(f'CREATE TABLE `{source}` LIKE `zying_product`')
            c.execute(f'CREATE TABLE `{target}` LIKE `infringement_risk_checks`')
            c.executemany(f'INSERT INTO `{source}` (`产品编号`, `疑似侵权`, `采集时间`, `提交时间`) VALUES (%s, %s, NOW(), NOW())',
                          [(str(i), str(i % 3)) for i in range(202)])
            c.execute(f"INSERT INTO `{target}` (source_type,source_row_id,risk_level,reason,checked_at) VALUES ('zying',1,2,'manual review',NOW())")
        reader.commit()
        with reader.cursor() as c, writer.cursor() as w:
            w.execute('SET SESSION innodb_lock_wait_timeout = 1')
            bit_mysql._backfill_legacy_zying_risk_checks(Cursor(c))
            # Keep reader's inserts uncommitted: source still must be writable.
            w.execute(f'INSERT INTO `{source}` (`产品编号`, `采集时间`, `提交时间`) VALUES (\'new-product\', NOW(), NOW())')
            writer.commit()
            c.execute(f'SELECT COUNT(*) n FROM `{target}`')
            assert c.fetchone()['n'] == 202
            c.execute(f'SELECT risk_level,reason FROM `{target}` WHERE source_row_id=1')
            assert c.fetchone() == {'risk_level': 2, 'reason': 'manual review'}
            reader.commit()
            bit_mysql._backfill_legacy_zying_risk_checks(Cursor(c))
            w.execute(f'INSERT INTO `{source}` (`产品编号`, `采集时间`, `提交时间`) VALUES (\'another-product\', NOW(), NOW())')
            writer.commit()
            c.execute(f'SELECT COUNT(*) n FROM `{target}`')
            assert c.fetchone()['n'] == 202
    finally:
        reader.rollback()
        writer.rollback()
        with reader.cursor() as c:
            c.execute(f'DROP TABLE IF EXISTS `{target}`')
            c.execute(f'DROP TABLE IF EXISTS `{source}`')
        reader.close()
        writer.close()
