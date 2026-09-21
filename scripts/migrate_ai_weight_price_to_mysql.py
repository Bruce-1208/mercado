"""Migrate the legacy AI核重核价 SQLite store into the server MySQL store.

The source file is read only. Existing MySQL rows with the same primary key are
updated so this command is safe to rerun after an interrupted migration.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from erp.ai_weight_price.config import data_dir
from erp.ai_weight_price.store import Store


def _rows(connection, table):
    connection.row_factory = sqlite3.Row
    return connection.execute(f"SELECT * FROM {table}").fetchall()


def _copy_tasks(source, target):
    rows = _rows(source, "tasks")
    with target.connect() as db:
        for row in rows:
            db.execute(
                """INSERT INTO tasks(erp_goods_id,status,stage,payload,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON DUPLICATE KEY UPDATE status=VALUES(status),stage=VALUES(stage),
                   payload=VALUES(payload),created_at=VALUES(created_at),updated_at=VALUES(updated_at)""",
                tuple(row),
            )
    return len(rows)


def _copy_collection_items(source, target):
    rows = _rows(source, "collection_items")
    with target.connect() as db:
        for row in rows:
            db.execute(
                """INSERT INTO collection_items(scope,erp_goods_id,page) VALUES(?,?,?)
                   ON DUPLICATE KEY UPDATE page=VALUES(page)""",
                tuple(row),
            )
    return len(rows)


def _copy_merchants(source, target):
    rows = _rows(source, "merchants")
    with target.connect() as db:
        for row in rows:
            db.execute(
                """INSERT INTO merchants(merchant_id,task_id,day,reserved_at,sent_at,conversation_url,message)
                   VALUES(?,?,?,?,?,?,?)
                   ON DUPLICATE KEY UPDATE task_id=VALUES(task_id),day=VALUES(day),
                   reserved_at=VALUES(reserved_at),sent_at=VALUES(sent_at),
                   conversation_url=VALUES(conversation_url),message=VALUES(message)""",
                tuple(row),
            )
    return len(rows)


def _copy_events(source, target):
    rows = _rows(source, "events")
    with target.connect() as db:
        for row in rows:
            db.execute(
                """INSERT INTO events(id,at,level,task_id,message) VALUES(?,?,?,?,?)
                   ON DUPLICATE KEY UPDATE at=VALUES(at),level=VALUES(level),
                   task_id=VALUES(task_id),message=VALUES(message)""",
                tuple(row),
            )
    return len(rows)


def _copy_state(source, target):
    rows = _rows(source, "state")
    with target.connect() as db:
        for row in rows:
            db.execute(
                "INSERT INTO state(`key`,value) VALUES(?,?) ON DUPLICATE KEY UPDATE value=VALUES(value)",
                tuple(row),
            )
    return len(rows)


def _copy_runs(source, target):
    rows = _rows(source, "runs")
    with target.connect() as db:
        for row in rows:
            db.execute(
                "INSERT INTO runs(run_id,payload) VALUES(?,?) ON DUPLICATE KEY UPDATE payload=VALUES(payload)",
                tuple(row),
            )
    return len(rows)


def _copy_run_items(source, target):
    rows = _rows(source, "run_items")
    with target.connect() as db:
        for row in rows:
            db.execute(
                """INSERT INTO run_items(run_id,erp_goods_id,sequence,payload) VALUES(?,?,?,?)
                   ON DUPLICATE KEY UPDATE sequence=VALUES(sequence),payload=VALUES(payload)""",
                tuple(row),
            )
    return len(rows)


def migrate(source_path: Path):
    if not source_path.is_file():
        raise FileNotFoundError(f"找不到 SQLite 文件：{source_path}")
    source = sqlite3.connect(source_path)
    target = Store(source_path.parent, backend="mysql")
    try:
        counts = {
            "tasks": _copy_tasks(source, target),
            "collection_items": _copy_collection_items(source, target),
            "merchants": _copy_merchants(source, target),
            "events": _copy_events(source, target),
            "state": _copy_state(source, target),
            "runs": _copy_runs(source, target),
            "run_items": _copy_run_items(source, target),
        }
        config_path = source_path.with_name("config.json")
        if config_path.is_file():
            target.set_state("config", json.loads(config_path.read_text(encoding="utf-8")))
            counts["config"] = 1
    finally:
        source.close()
    return counts


def main():
    parser = argparse.ArgumentParser(description="迁移 AI核重核价 SQLite 数据到服务器 MySQL")
    parser.add_argument("--sqlite-path", type=Path, default=data_dir() / "tasks.sqlite3")
    args = parser.parse_args()
    counts = migrate(args.sqlite_path)
    print(json.dumps({"source": str(args.sqlite_path.resolve()), "copied": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
