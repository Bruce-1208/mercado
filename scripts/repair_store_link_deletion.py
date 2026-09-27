"""Restore one audited local deletion and optionally delete its exact remote items.

Run with ``python3 -m scripts.repair_store_link_deletion AUDIT_ID`` to inspect.
Add ``--apply`` only for an explicitly authorized deletion batch.
"""
import argparse
import json
from pathlib import Path
import sqlite3

from bit import bit_mysql
from bit.bit_runtime_lock import InterProcessLock
from bit.bit_store_link_sync import STORE_LINK_SYNC_LOCK_KEY, _client_and_token
from bit.bit_store_link_remote_update import _delete_one_link
from erp.mercadolibre_store_link_store import _connect, STORE_LINK_TABLE
from erp.store_link_audit import record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_id", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    audit_path = Path(__file__).resolve().parents[1] / "runtime_logs/store_link_operations.sqlite3"
    audit = sqlite3.connect(f"file:{audit_path}?mode=ro&immutable=1", uri=True)
    try:
        event = audit.execute(
            "SELECT details FROM operations WHERE id=? AND action='delete_store_links' AND phase='before'",
            (args.audit_id,),
        ).fetchone()
    finally:
        audit.close()
    if not event:
        raise ValueError("找不到指定的历史删除快照")
    snapshot = json.loads(event[0])
    rows = snapshot["rows"]
    if set(snapshot["link_ids"]) != {row["id"] for row in rows}:
        raise ValueError("快照编号与删除范围不一致")

    lease = InterProcessLock(STORE_LINK_SYNC_LOCK_KEY, owner="repair_store_link_deletion")
    if not lease.acquire(timeout=0):
        raise RuntimeError("链接同步或修改正在执行，请等待任务完成")
    try:
        tokens = {}
        for row in rows:
            token_id = int(row["token_id"])
            if token_id not in tokens:
                client, token = _client_and_token(dict(bit_mysql.get_mercado_store_token(token_id) or {}))
                tokens[token_id] = (client, token)
            client, token = tokens[token_id]
            item = client.get_marketplace_item(row["item_id"])
            print(json.dumps({"item_id": row["item_id"], "store": row["store_name"],
                              "remote_status": item.get("status"), "sub_status": item.get("sub_status"),
                              "deleted": item.get("deleted")}, ensure_ascii=False), flush=True)
            if not args.apply:
                continue
            row = {**row, "status": item.get("status") or row.get("status")}
            # Restore the original full snapshot, including price, links and timestamps.
            # Refuse a conflicting reused primary key; reuse a re-synced matching item.
            connection = _connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT id,token_id,item_id FROM `{STORE_LINK_TABLE}` "
                                   "WHERE id=%s OR (token_id=%s AND item_id=%s) FOR UPDATE",
                                   (row["id"], token_id, row["item_id"]))
                    existing = cursor.fetchall()
                    if any(int(e["id"]) == int(row["id"]) and
                           (int(e["token_id"]), e["item_id"]) != (token_id, row["item_id"]) for e in existing):
                        raise ValueError("原记录编号已被其他链接占用")
                    if existing:
                        restored_id = int(existing[0]["id"])
                    else:
                        cursor.execute(f"SHOW COLUMNS FROM `{STORE_LINK_TABLE}`")
                        allowed = {column["Field"] for column in cursor.fetchall()}
                        columns = [key for key in row if key in allowed]
                        cursor.execute(f"INSERT INTO `{STORE_LINK_TABLE}` ("
                                       + ",".join(f"`{key}`" for key in columns)
                                       + ") VALUES (" + ",".join(["%s"] * len(columns)) + ")",
                                       tuple(json.dumps(row[key], ensure_ascii=False)
                                             if isinstance(row[key], (dict, list)) else row[key]
                                             for key in columns))
                        restored_id = int(row["id"])
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
            record("restore_deleted_link", "completed", {"audit_id": args.audit_id,
                   "link_id": restored_id, "item_id": row["item_id"]})
            result = _delete_one_link({**row, "id": restored_id}, token)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        lease.release()


if __name__ == "__main__":
    main()
